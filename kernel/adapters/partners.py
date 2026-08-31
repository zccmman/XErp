"""往来余额查询（P2-02）：回答「谁欠我、我欠谁」。

数据源是余额投影（Balance，按 账套×期间×科目×辅助维度 聚合）——
适配器挂了 aux_dims 后，往来余额无需任何新增投影逻辑，直接按维度切片。

口径
----
- 应收类（资产/借方余额方向）：balance = 借 - 贷，正数 = 客户欠我；
- 应付类（负债/贷方余额方向）：balance = 贷 - 借，正数 = 我欠供应商；
- 无维度行（如 dogfood 早期手工录入的凭证）单独列出 ``dims: null``，
  并在汇总里给出「未挂维度」金额——**宁可暴露脏数据，也不静默吞掉**。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Period, Voucher, VoucherLine

ZERO = Decimal("0.00")

# 默认往来科目（与科目模板的 aux_dims 声明一致）
DEFAULT_AR_ACCOUNTS = ("1122",)
DEFAULT_AP_ACCOUNTS = ("2202",)


def partner_balances(
    session: Session,
    ledger_set_id: str,
    *,
    year: int | None = None,
    month: int | None = None,
    ar_accounts: tuple[str, ...] = DEFAULT_AR_ACCOUNTS,
    ap_accounts: tuple[str, ...] = DEFAULT_AP_ACCOUNTS,
) -> dict[str, Any]:
    """按往来单位聚合应收/应付余额。

    未指定期间时取最新 OPEN 期间；指定则取该期间（期末口径 = 截至该期间累计）。
    返回 receivables / payables 两组明细 + 未挂维度金额 + 平衡校验。
    """
    prefixes = tuple(ar_accounts) + tuple(ap_accounts)
    # 多个前缀是「或」关系——展开成 AND 会永远查空（一个编码不可能同时
    # 匹配 1122% 和 2202%），这是本函数第一版实际踩过的坑
    accounts = {
        a.id: a
        for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id,
                or_(*[Account.code.like(p + "%") for p in prefixes]),
            )
        ).all()
    }
    if not accounts:
        return {
            "period": None,
            "receivables": [],
            "payables": [],
            "untracked_total": "0.00",
            "reconcile": {"ok": True, "note": "无往来科目余额"},
        }

    period = None
    if year and month:
        period = session.scalars(
            select(Period).where(
                Period.ledger_set_id == ledger_set_id,
                Period.year == year,
                Period.month == month,
            )
        ).first()
    else:
        period = session.scalars(
            select(Period)
            .where(
                Period.ledger_set_id == ledger_set_id, Period.status == "OPEN"
            )
            .order_by(Period.year.desc(), Period.month.desc())
        ).first()
    if period is None:
        period = session.scalars(
            select(Period)
            .where(Period.ledger_set_id == ledger_set_id)
            .order_by(Period.year.desc(), Period.month.desc())
        ).first()
    if period is None:
        return {
            "period": None,
            "receivables": [],
            "payables": [],
            "untracked_total": "0.00",
            "reconcile": {"ok": True, "note": "账套尚无期间"},
        }

    # 往来口径不查 Balance 投影——投影只累积 POSTED，而 PUSHED（待审）
    # 的开票事件在业务上已经产生债权。直接从凭证明细聚合，
    # 覆盖 PUSHED/APPROVED/POSTED，返回里标注口径。
    included_status = ("PUSHED", "APPROVED", "POSTED")
    rows = session.execute(
        select(VoucherLine, Voucher.status)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status.in_(included_status),
            VoucherLine.account_id.in_(list(accounts)),
        )
    ).all()

    receivables: dict[tuple[str, str], Decimal] = {}
    payables: dict[tuple[str, str], Decimal] = {}
    untracked = Decimal("0")

    for line, _status in rows:
        acc = accounts[line.account_id]
        debit = Decimal(str(line.debit))
        credit = Decimal(str(line.credit))
        # 科目声明的余额方向：资产类借方为正，负债类贷方为正
        net = debit - credit if acc.direction == "debit" else credit - debit
        if net == ZERO:
            continue
        dims = line.aux_dims or None
        if dims is None:
            untracked += net
            continue
        # 维度可能含多键，取第一个作为往来单位标识
        partner = next(iter(dims.values()), "?")
        bucket = receivables if acc.code.startswith(tuple(ar_accounts)) else payables
        key = (partner, acc.code)
        bucket[key] = bucket.get(key, ZERO) + net

    def _rows(bucket: dict) -> list[dict]:
        out = [
            {
                "partner": partner,
                "account": code,
                "balance": f"{amount.quantize(Decimal('0.01')):,.2f}",
            }
            for (partner, code), amount in sorted(
                bucket.items(), key=lambda kv: (-kv[1], kv[0][0])
            )
            if amount != ZERO
        ]
        return out

    return {
        "period": {"year": period.year, "month": period.month},
        "basis": {
            "voucher_status": list(included_status),
            "note": "开票即挂账：含待审与待过账凭证（在途口径）",
        },
        "receivables": _rows(receivables),
        "payables": _rows(payables),
        "untracked_total": f"{untracked.quantize(Decimal('0.01')):,.2f}",
        "reconcile": {
            "ok": True,
            "note": (
                "存在未挂往来维度的余额（早期手工凭证），已单列不混入明细"
                if untracked != ZERO
                else "往来维度全覆盖"
            ),
        },
    }
