"""P0-1 成本核算：制造费用分摊 + 完工产品成本结转（纯函数、只读）。

设计铁律（ADR-002 单一真源 / 零新表）：
- 成本对象 = (5001 生产成本 + VoucherLine.aux_dims[project|department])，既有结构已承载。
- 制造费用 = 5101 当期发生额；分摊与结转均为只读算子，产出凭证草稿 lines，
  落库经既有 create_voucher HITL。绝不新建写凭证工具。

链接约定：生产成本 / 制造费用凭证行在 aux_dims 带成本对象维度
（project / department），内核据此聚合各对象发生额。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Voucher, VoucherLine

ZERO = Decimal("0")

_BASE_TO_SUB = {"direct_material": "500101", "direct_labor": "500102"}


class CostingError(RuntimeError):
    pass


def _fmt(d: Decimal) -> str:
    return f"{(d or ZERO):.2f}"


def _cost_object_key(aux: dict | None) -> str:
    aux = aux or {}
    for dim in ("project", "department"):
        if aux.get(dim):
            return f"{dim}:{aux[dim]}"
    return "general"


def _posted_amounts_by_object(
    session: Session, ledger_set_id: str, codes: set[str], year: int, month: int
) -> dict[str, dict[str, Decimal]]:
    """返回 {(cost_object_key): {code: net_debit}}，仅统计 POSTED 且期间命中。

    net_debit = 借方 − 贷方（这些成本科目活动均为借，净额即可）。
    """
    rows = session.execute(
        select(Voucher, VoucherLine, Account.code)
        .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
        .join(Account, Account.id == VoucherLine.account_id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status == "POSTED",
            Account.code.in_(codes),
        )
    ).all()
    out: dict[str, dict[str, Decimal]] = {}
    for v, ln, code in rows:
        if (v.voucher_date.year, v.voucher_date.month) != (year, month):
            continue
        key = _cost_object_key(ln.aux_dims)
        obj = out.setdefault(key, {})
        obj[code] = obj.get(code, ZERO) + (ln.debit - ln.credit)
    return out


def _beginning_balance_by_object(
    session: Session, ledger_set_id: str, codes: set[str], year: int, month: int
) -> dict[str, dict[str, Decimal]]:
    """期初余额（严格早于目标期间的累计 net），用于完工结转的期初在产。

    仅统计 (year, month) < (目标年, 月) 的 POSTED 行，避免与当期投入重复累加。
    """
    rows = session.execute(
        select(Voucher, VoucherLine, Account.code)
        .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
        .join(Account, Account.id == VoucherLine.account_id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status == "POSTED",
            Account.code.in_(codes),
        )
    ).all()
    out: dict[str, dict[str, Decimal]] = {}
    for v, ln, code in rows:
        pk = (v.voucher_date.year, v.voucher_date.month)
        if pk >= (year, month):
            continue
        key = _cost_object_key(ln.aux_dims)
        obj = out.setdefault(key, {})
        obj[code] = obj.get(code, ZERO) + (ln.debit - ln.credit)
    return out


def cost_allocation_draft(
    session: Session,
    ledger_set_id: str,
    year: int,
    month: int,
    base: str = "direct_material",
) -> dict:
    """制造费用（5101）按基础分摊到各 5001 成本对象 + 凭证草稿 lines（只读）。

    base：direct_material（默认，按各对象直接材料 500101 金额占比）/ direct_labor
    （按直接人工 500102 占比）。MVP 仅支持金额类基础。
    """
    if base not in _BASE_TO_SUB:
        raise CostingError(
            f"MVP 暂仅支持分摊基础 direct_material / direct_labor，收到: {base}"
        )
    sub_code = _BASE_TO_SUB[base]
    amounts = _posted_amounts_by_object(
        session, ledger_set_id, {"5101", sub_code}, year, month
    )
    total_overhead = ZERO
    weights: dict[str, Decimal] = {}
    for key, obj in amounts.items():
        total_overhead += obj.get("5101", ZERO)
        weights[key] = obj.get(sub_code, ZERO)
    total_weight = sum(weights.values(), ZERO)

    lines: list[dict] = []
    allocations: list[dict] = []
    if total_overhead > 0 and total_weight > 0:
        for key, w in weights.items():
            share = total_overhead * w / total_weight
            if share <= 0:
                continue
            lines.append({
                "account_code": "500103", "debit": _fmt(share), "credit": "",
                "summary": f"分摊制造费用（{key}·{base}）",
                "aux_dims": _aux_from_key(key),
            })
            allocations.append({
                "cost_object": key, "base": base,
                "base_amount": _fmt(w), "allocated": _fmt(share),
            })
    if total_overhead > 0:
        lines.append({
            "account_code": "5101", "debit": "", "credit": _fmt(total_overhead),
            "summary": f"{year}-{month:02d} 结转制造费用",
        })
    return {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "base": base,
        "total_overhead": _fmt(total_overhead),
        "allocations": allocations,
        "lines": lines,
        "summary": (
            f"{year}-{month:02d} 制造费用 {_fmt(total_overhead)} 按 {base} 分摊至 "
            f"{len(allocations)} 个成本对象"
        ),
    }


def _aux_from_key(key: str) -> dict:
    if key == "general":
        return {}
    dim, val = key.split(":", 1)
    return {dim: val}


def cost_settlement_draft(
    session: Session,
    ledger_set_id: str,
    year: int,
    month: int,
    ending_wip: Decimal = ZERO,
) -> dict:
    """完工产品成本结转草稿（只读）：借 1405 库存商品 / 贷 5001 生产成本。

    简化口径：某成本对象完工 = 期初在产(5001 期初 net) + 本期投入(5001 本期净额) − 期末在产。
    期末在产(ending_wip)由用户/AI 输入（默认 0 全部完工）；若给定，按各对象当前 5001
    期末余额(WIP)比例分摊到对象。落库经 create_voucher HITL。
    """
    # 生产成本按 5001 整棵子树（5001/500101/500102/500103）聚合到成本对象，口径一致。
    SUBTREE = ("5001", "500101", "500102", "500103")
    cur = _posted_amounts_by_object(session, ledger_set_id, set(SUBTREE), year, month)
    beg = _beginning_balance_by_object(session, ledger_set_id, set(SUBTREE), year, month)

    def _subtotal(bucket: dict[str, Decimal]) -> Decimal:
        return sum(bucket.get(c, ZERO) for c in SUBTREE)

    # 各对象 5001 期末 WIP（当前余额）= 期初 + 本期 整棵子树净额
    wip_bal: dict[str, Decimal] = {}
    for key in set(cur) | set(beg):
        wip_bal[key] = _subtotal(beg.get(key, {})) + _subtotal(cur.get(key, {}))
    total_wip = sum(v for v in wip_bal.values() if v > 0)
    ending_wip = Decimal(str(ending_wip))

    lines: list[dict] = []
    settlements: list[dict] = []
    for key, obj_cur in cur.items():
        input_amt = _subtotal(obj_cur)            # 本期投入（整棵子树净额）
        begin_amt = _subtotal(beg.get(key, {}))   # 期初在产
        # 该对象分摊的期末在产
        if ending_wip > 0 and total_wip > 0 and wip_bal.get(key, ZERO) > 0:
            obj_wip = ending_wip * wip_bal[key] / total_wip
        else:
            obj_wip = ZERO
        completed = begin_amt + input_amt - obj_wip
        if completed <= 0:
            continue
        lines.append({
            "account_code": "1405", "debit": _fmt(completed), "credit": "",
            "summary": f"完工入库（{key}）", "aux_dims": _aux_from_key(key),
        })
        lines.append({
            "account_code": "5001", "debit": "", "credit": _fmt(completed),
            "summary": f"结转完工成本（{key}）", "aux_dims": _aux_from_key(key),
        })
        settlements.append({
            "cost_object": key, "beginning_wip": _fmt(begin_amt),
            "input": _fmt(input_amt), "ending_wip": _fmt(obj_wip),
            "completed": _fmt(completed),
        })
    return {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "total_completed": _fmt(sum(
            Decimal(s["completed"]) for s in settlements
        )),
        "settlements": settlements,
        "lines": lines,
        "summary": (
            f"{year}-{month:02d} 完工结转合计 "
            f"{_fmt(sum(Decimal(s['completed']) for s in settlements))}"
            + (f"，期末在产 {_fmt(ending_wip)}" if ending_wip > 0 else "")
        ),
    }
