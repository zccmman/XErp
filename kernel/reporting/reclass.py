"""往来重分类列报（阶段1）：按往来单位余额方向重分类应收/预付与应付/预收。

口径（企业会计准则列报惯例——按往来单位余额方向重分类，不是简单对冲抵消）：

    应收账款 1122 按客户：借方余额 → 应收账款（资产）；贷方余额 → 预收款项（负债 2203）
    预收账款 2203 按客户：贷方余额 → 预收款项；借方余额 → 应收账款
    预付账款 1123 按供应商：借方余额 → 预付款项；贷方余额 → 应付账款
    应付账款 2202 按供应商：贷方余额 → 应付账款；借方余额 → 预付款项

真源与同源约束：

- 科目对来自本体 `relations.csv` 的 reclass_pairs（本体第一次进报表，
  新增对冲对改文件即可，不改代码）；
- 取数与资产负债表同源（Balance 投影 × dims_key 的 canonical 键），
  因此重分类后「资产 = 负债 + 所有者权益」仍成立——它只是在两边之间搬金额，
  总额不变。不加这一约束，重分类会变成又一个对不上的数。
- 未挂往来维度的余额**保守留在原报表项目**并单独暴露：宁可暴露脏数据，
  也不靠猜测把金额搬走（同 partner_balances 的纪律）。
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, Period
from kernel.ontology import OntologyError
from kernel.reporting.statements import _period, ending_balance

ZERO = Decimal("0.00")
Q = Decimal("0.01")


def reclass_pairs(standard: str = "small_business") -> list[dict]:
    """本体声明的重分类科目对：[{asset_code, liability_code, note_zh}]。

    哪一侧是资产按编码前缀判定（科目表约束：1*=资产，2*=负债，ADR-001 精神），
    因此 relations.csv 里 2203→1122 与 1123→2202 两种写法都能正确归一。
    """
    from kernel.ontology import load_relations

    out: list[dict] = []
    for r in load_relations(standard):
        if r["rel_type"] != "reclass_pairs":
            continue
        a, b = r["subj_code"], r["obj_code"]
        if a.startswith("1") and b.startswith("2"):
            asset, liab = a, b
        elif a.startswith("2") and b.startswith("1"):
            asset, liab = b, a
        else:
            raise OntologyError(
                f"reclass_pairs {a}->{b} 不构成资产/负债对冲对（须 1* 与 2* 各一）"
            )
        out.append(
            {"asset_code": asset, "liability_code": liab, "note_zh": r["note_zh"]}
        )
    return out


def _partner_of(dims_key: str) -> str | None:
    """从 Balance.dims_key（canonical_json）取往来单位；无维度返回 None。

    与 partner_balances 同一惯例：维度可能含多键，取第一个值作往来单位标识。
    """
    if not dims_key:
        return None
    try:
        dims = json.loads(dims_key)
    except (TypeError, ValueError):
        return None
    if not isinstance(dims, dict) or not dims:
        return None
    return str(next(iter(dims.values())))


def _side_of(code: str, pairs: list[dict]) -> tuple[str, str] | None:
    """科目在某对冲对中的 (side, other_code)；不在任何对冲对则返回 None。"""
    for p in pairs:
        if code.startswith(p["asset_code"]):
            return "asset", p["liability_code"]
        if code.startswith(p["liability_code"]):
            return "liability", p["asset_code"]
    return None


def reclassify(
    session: Session,
    ledger_set_id: str,
    year: int,
    month: int,
    standard: str = "small_business",
) -> dict:
    """计算期末往来重分类明细。

    返回 {period, pairs, items, to_asset, to_liability, untracked, note}：
      items      逐 (科目, 往来单位) 的反向余额重分类建议
      to_asset   负债侧借方余额 → 资产（预付性质）
      to_liability 资产侧贷方余额 → 负债（预收性质）
      untracked  未挂往来维度、保守留在原处的金额（不参与重分类）
    """
    pairs = reclass_pairs(standard)
    period = _period(session, ledger_set_id, year, month)
    if not pairs:
        return {
            "period": {"year": year, "month": month},
            "pairs": [],
            "items": [],
            "to_asset": ZERO,
            "to_liability": ZERO,
            "untracked": ZERO,
            "note": "本体未声明 reclass_pairs 关系，无需重分类",
        }

    prefixes = [p["asset_code"] for p in pairs] + [p["liability_code"] for p in pairs]
    accounts = {
        a.id: a
        for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id,
                or_(*[Account.code.like(pfx + "%") for pfx in prefixes]),
            )
        ).all()
    }
    items: list[dict] = []
    untracked = ZERO
    to_asset = ZERO
    to_liability = ZERO
    if accounts:
        rows = session.scalars(
            select(Balance).where(
                Balance.period_id == period.id,
                Balance.account_id.in_(list(accounts)),
            )
        ).all()
        agg: dict[tuple[str, str], list[Decimal]] = {}
        for b in rows:
            acc = accounts[b.account_id]
            dr, cr = agg.get((acc.code, b.dims_key), [ZERO, ZERO])
            agg[(acc.code, b.dims_key)] = [
                dr + Decimal(str(b.debit_total)),
                cr + Decimal(str(b.credit_total)),
            ]
        for (code, dims_key), (dr, cr) in sorted(agg.items()):
            net = ending_balance(code, dr, cr)
            if net == ZERO:
                continue
            located = _side_of(code, pairs)
            if located is None:
                continue
            side, other = located
            partner = _partner_of(dims_key)
            if partner is None:
                # 未挂维度：宁可留在原报表项目，也不按猜测搬动
                if net < ZERO:
                    untracked += -net
                continue
            if net > ZERO:
                continue  # 方向与科目正常方向一致，保持原列报
            amount = -net  # 反向余额，正数金额
            if side == "asset":
                to_liability += amount
                direction = "to_liability"
            else:
                to_asset += amount
                direction = "to_asset"
            items.append(
                {
                    "account_code": code,
                    "partner": partner,
                    "balance": net.quantize(Q),
                    "to_account_code": other,
                    "amount": amount.quantize(Q),
                    "direction": direction,
                }
            )

    return {
        "period": {"year": year, "month": month},
        "pairs": pairs,
        "items": items,
        "to_asset": to_asset.quantize(Q),
        "to_liability": to_liability.quantize(Q),
        "untracked": untracked.quantize(Q),
        "note": (
            f"按往来单位余额方向重分类：{len(items)} 项，"
            f"资产→负债 {to_liability.quantize(Q)}、负债→资产 {to_asset.quantize(Q)}"
        ),
    }


def apply_reclass(
    session: Session,
    ledger_set_id: str,
    period: Period,
    amounts: dict[str, tuple[Decimal, Decimal]],
    standard: str = "small_business",
) -> tuple[dict[str, tuple[Decimal, Decimal]], dict]:
    """把反向往来余额搬到对方科目，返回 (调整后 amounts, 重分类明细)。

    「搬」的规则统一为：两侧都按各自**正常余额方向**加回该金额——
    来源方净额为负（反向）加回后归零，目标方净额同向增加。
    这样重分类只在资产/负债两边之间搬金额，总额不变、表内仍平衡。
    """
    detail = reclassify(session, ledger_set_id, period.year, period.month, standard)
    if not detail["items"]:
        return amounts, detail

    directions = {
        a.code: a.direction
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        )
    }
    new = dict(amounts)

    def _add(code: str, amount: Decimal) -> None:
        dr, cr = new.get(code, (ZERO, ZERO))
        if directions.get(code, "debit") == "debit":
            new[code] = (dr + amount, cr)
        else:
            new[code] = (dr, cr + amount)

    for it in detail["items"]:
        _add(it["account_code"], it["amount"])       # 来源方归零
        _add(it["to_account_code"], it["amount"])    # 目标方同向增加
    return new, detail
