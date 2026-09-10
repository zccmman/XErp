"""辅助核算报表（②）：按维度（客户/供应商/部门/项目/其他）透视余额。

数据源只有一处：``balances`` 投影（账套×期间×科目×dims_key）。
dims_key 是 aux_dims 的 canonical_json（如 ``{"customer":"甲公司"}`` 或
``{"department":"销售部","customer":"甲公司"}``）；辅助维度值以**名称字符串**
承载，与 ``Party``（party_type + name）对应，因此报表既能聚合已知往来单位，
也能回显一次性手填的维度值。

口径铁律（与三大报表一致）：
- 只取 POSTED 凭证产生的余额（balances 投影本身只来自 POSTED，见 posting.py）；
- 期初/结转凭证的余额同样含在 balances 里（投影已含期初），不单独剔除；
- 不引入任何业务规则，纯按维度切片聚合，结果可被账账核对独立验证。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, LedgerSet, Party, Period

ZERO = Decimal("0.00")

DIMENSIONS = ("customer", "supplier", "department", "project", "other")


class AuxReportError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _dim_key(dim: str) -> str:
    if dim not in DIMENSIONS:
        raise AuxReportError(
            "BAD_DIM",
            f"未知辅助维度：{dim!r}（可选：{', '.join(DIMENSIONS)}）",
            {"dim": dim, "allowed": list(DIMENSIONS)},
        )
    return dim


def _period_id(session: Session, ledger_set_id: str, year: int | None,
               month: int | None) -> str | None:
    """给定年份月份返回期间 id；不传则返回 None（表示全期合计）。"""
    if year is None or month is None:
        return None
    p = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year, Period.month == month,
        )
    ).first()
    if p is None:
        raise AuxReportError(
            "PERIOD_NOT_FOUND",
            f"账套不存在 {year}-{month:02d} 期间",
            {"year": year, "month": month},
        )
    return p.id


def _extract_dim(dims_key: str, dim: str) -> str | None:
    """从 dims_key 取出目标维度的值；不含该维度返回 None。"""
    if not dims_key:
        return None
    try:
        obj = json.loads(dims_key)
    except Exception:
        return None
    val = obj.get(dim)
    return str(val).strip() if val else None


def aux_ledger(
    session: Session,
    *,
    ledger_set_id: str,
    dim: str,
    party_name: str | None = None,
    account_code: str | None = None,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """辅助核算明细：按（维度值 × 科目）聚合借/贷/净额。

    party_name：仅看某个辅助对象（按名称匹配，不强制存在 Party 记录）。
    account_code：仅看某个科目（支持前缀匹配，末级优先）。
    year/month：不传 = 全期合计；传入 = 该期间。
    """
    dim = _dim_key(dim)
    pid = _period_id(session, ledger_set_id, year, month)

    accounts = {
        a.id: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    parties = {
        (p.party_type, p.name): p.id
        for p in session.scalars(
            select(Party).where(Party.ledger_set_id == ledger_set_id)
        ).all()
    }

    stmt = select(Balance).where(Balance.ledger_set_id == ledger_set_id)
    if pid is not None:
        stmt = stmt.where(Balance.period_id == pid)
    balances = session.scalars(stmt).all()

    # (维度值, 科目id) -> 发生额
    agg: dict[tuple[str, str], dict] = {}
    seen_dims: set[str] = set()
    for b in balances:
        value = _extract_dim(b.dims_key, dim)
        if value is None:
            continue
        if party_name is not None and value != party_name:
            continue
        acc = accounts.get(b.account_id)
        if acc is None:
            continue
        if account_code and not acc.code.startswith(str(account_code).strip()):
            continue
        seen_dims.add(value)
        key = (value, b.account_id)
        bucket = agg.setdefault(key, {
            "debit": ZERO, "credit": ZERO,
        })
        bucket["debit"] += Decimal(str(b.debit_total))
        bucket["credit"] += Decimal(str(b.credit_total))

    rows: list[dict] = []
    for (value, acc_id), bucket in sorted(agg.items()):
        acc = accounts[acc_id]
        net = bucket["debit"] - bucket["credit"]
        party_id = parties.get((dim, value))
        rows.append({
            "dim_value": value,
            "party_id": party_id,
            "account_code": acc.code,
            "account_name": acc.name,
            "debit": str(bucket["debit"]),
            "credit": str(bucket["credit"]),
            "net": str(net),
        })

    total_debit = sum((Decimal(r["debit"]) for r in rows), ZERO)
    total_credit = sum((Decimal(r["credit"]) for r in rows), ZERO)
    return {
        "ledger_set_id": ledger_set_id,
        "dim": dim,
        "scope": (
            {"year": year, "month": month} if pid is not None
            else {"year": None, "month": None}
        ),
        "account_filter": account_code,
        "party_filter": party_name,
        "rows": rows,
        "totals": {
            "debit": str(total_debit),
            "credit": str(total_credit),
            "net": str(total_debit - total_credit),
        },
        "basis": "仅 POSTED 凭证产生的余额投影；按辅助维度切片聚合",
    }


def aux_summary(
    session: Session,
    *,
    ledger_set_id: str,
    dim: str,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """辅助核算汇总：按维度值跨科目合计净额（一个辅助对象一张小计）。

    用于「客户往来一览」「部门费用一览」等总览场景；交叉到具体科目用 aux_ledger。
    """
    dim = _dim_key(dim)
    full = aux_ledger(
        session, ledger_set_id=ledger_set_id, dim=dim,
        year=year, month=month,
    )
    by_value: dict[str, Decimal] = {}
    by_party: dict[str, str | None] = {}
    for r in full["rows"]:
        by_value[r["dim_value"]] = (
            by_value.get(r["dim_value"], ZERO) + Decimal(r["net"])
        )
        by_party[r["dim_value"]] = r["party_id"]
    items = [
        {
            "dim_value": v,
            "party_id": by_party.get(v),
            "net": str(net),
        }
        for v, net in sorted(by_value.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    total = sum((Decimal(i["net"]) for i in items), ZERO)
    return {
        "ledger_set_id": ledger_set_id,
        "dim": dim,
        "scope": full["scope"],
        "items": items,
        "total_net": str(total),
        "basis": "按辅助维度值跨科目合计净额",
    }
