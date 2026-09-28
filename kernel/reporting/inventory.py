"""P0-1 存货：收发存台账 + 期末计价（纯函数、只读）。

设计铁律（ADR-002 单一真源）：
- 收发存台账**不建投影表**，全部由 POSTED 凭证明细（VoucherLine.quantity +
  aux_dims["inventory_item"] == 货品编码）重建。
- 计价（加权平均 / 移动加权 / 先进先出）是只读算子，产出价值与**结转凭证草稿
  lines**；落库一律由调用方经既有 create_voucher HITL（本模块绝不写凭证）。

链接约定：一笔存货收发凭证行需在 aux_dims 里带 {"inventory_item": "<货品编码>"}，
并在 quantity 字段填数量；借方=收（库存增加）、贷方=发（库存减少）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, InventoryItem, Voucher, VoucherLine

ZERO = Decimal("0")


class InventoryError(RuntimeError):
    pass


def _fmt(d: Decimal) -> str:
    return f"{(d or ZERO):.2f}"


def _get_item(session: Session, ledger_set_id: str, item_code: str) -> InventoryItem:
    item = session.scalars(
        select(InventoryItem).where(
            InventoryItem.ledger_set_id == ledger_set_id,
            InventoryItem.code == item_code,
        )
    ).first()
    if item is None:
        raise InventoryError(f"存货档案 {item_code} 在账套 {ledger_set_id} 下不存在")
    return item


def _posted_lines_for_item(session: Session, ledger_set_id: str, item_code: str):
    """返回该存货所有 POSTED 凭证明细（含数量），按凭证日期升序。

    每行：(voucher_date, account_code, debit, credit, quantity, unit)
    库存类科目为资产方向：借方收、贷方发。
    """
    rows = session.execute(
        select(Voucher, VoucherLine, Account.code)
        .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
        .join(Account, Account.id == VoucherLine.account_id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status == "POSTED",
        )
    ).all()
    out = []
    for v, ln, acc_code in rows:
        aux = ln.aux_dims or {}
        if aux.get("inventory_item") != item_code:
            continue
        if ln.quantity is None:
            continue
        out.append((v.voucher_date, acc_code, ln.debit, ln.credit, ln.quantity, ln.unit))
    out.sort(key=lambda r: r[0])
    return out


def stockcard(
    session: Session, ledger_set_id: str, item_code: str, year: int, month: int
) -> dict:
    """收发存汇总（单一真源重建）。返回期初/本期收/本期发/期末的数量与金额。"""
    item = _get_item(session, ledger_set_id, item_code)
    lines = _posted_lines_for_item(session, ledger_set_id, item_code)
    target = (year, month)
    beg_qty = beg_amt = ZERO
    recv_qty = recv_amt = ZERO
    issue_qty = issue_amt = ZERO
    for d, _acc, debit, credit, qty, _unit in lines:
        qty_delta = qty if debit > 0 else -qty
        amt_delta = debit if debit > 0 else -credit
        pk = (d.year, d.month)
        if pk < target:
            beg_qty += qty_delta
            beg_amt += amt_delta
        elif pk == target:
            recv_qty += qty_delta if qty_delta > 0 else ZERO
            issue_qty += -qty_delta if qty_delta < 0 else ZERO
            recv_amt += amt_delta if amt_delta > 0 else ZERO
            issue_amt += -amt_delta if amt_delta < 0 else ZERO
    end_qty = beg_qty + recv_qty - issue_qty
    end_amt = beg_amt + recv_amt - issue_amt
    return {
        "item_code": item_code,
        "item_name": item.name,
        "unit": item.unit,
        "account_code": item.default_account_code,
        "method": item.valuation_method,
        "period": {"year": year, "month": month},
        "beginning": {"qty": beg_qty, "amount": beg_amt},
        "receipts": {"qty": recv_qty, "amount": recv_amt},
        "issues": {"qty": issue_qty, "amount": issue_amt},
        "ending": {"qty": end_qty, "amount": end_amt},
        "transactions": len(lines),
    }


def _ordered_events(lines):
    """把收发存行转成有序事件：receipt=(+qty,+amt)，issue=(-qty,-amt)。"""
    evs = []
    for d, _acc, debit, credit, qty, _unit in lines:
        if debit > 0:
            evs.append((d, True, qty, debit))       # 收
        else:
            evs.append((d, False, qty, credit))      # 发
    evs.sort(key=lambda e: e[0])
    return evs


def _fifo_ending_value(events):
    lots: list[list[Decimal]] = []  # [qty, unit_cost]
    cogs = ZERO
    for _d, is_receipt, qty, amt in events:
        if is_receipt:
            unit = (amt / qty) if qty > 0 else ZERO
            lots.append([qty, unit])
        else:
            remain = qty
            while remain > 0 and lots:
                lot = lots[0]
                take = min(remain, lot[0])
                cogs += take * lot[1]
                lot[0] -= take
                remain -= take
                if lot[0] == ZERO:
                    lots.pop(0)
    end_qty = sum(l[0] for l in lots)
    end_amt = sum(l[0] * l[1] for l in lots)
    return end_amt, cogs


def _moving_avg_ending_value(events):
    avg = ZERO
    run_qty = ZERO
    cogs = ZERO
    for _d, is_receipt, qty, amt in events:
        if is_receipt:
            new_qty = run_qty + qty
            avg = ((run_qty * avg) + amt) / new_qty if new_qty > 0 else ZERO
            run_qty = new_qty
        else:
            cogs += qty * avg
            run_qty -= qty
    end_qty = run_qty
    end_amt = run_qty * avg
    return end_amt, cogs


def inventory_valuation_draft(
    session: Session,
    ledger_set_id: str,
    item_code: str,
    year: int,
    month: int,
    method: str | None = None,
    physical_count_qty: Decimal | None = None,
) -> dict:
    """期末计价 + 结转凭证草稿（只读）。

    method：weighted_avg（默认，月末一次加权平均）/ moving_avg（移动加权）/ fifo（先进先出）。
    physical_count_qty：实地盘点数量（可选）；给定则额外产出盘盈/盘亏调整 lines。

    返回：stockcard 摘要 + unit_cost + ending_value + cogs + 草稿 lines
    （periodic_cogs：借 6401 主营业务成本/贷 库存商品；盘盈盘亏：1901 待处理财产损溢）。
    """
    item = _get_item(session, ledger_set_id, item_code)
    sc = stockcard(session, ledger_set_id, item_code, year, month)
    method = (method or item.valuation_method).lower()
    if method not in ("weighted_avg", "moving_avg", "fifo"):
        raise InventoryError(f"不支持的计价方法: {method}")

    beg_qty = sc["beginning"]["qty"]
    beg_amt = sc["beginning"]["amount"]
    recv_qty = sc["receipts"]["qty"]
    recv_amt = sc["receipts"]["amount"]
    issue_qty = sc["issues"]["qty"]
    avail_qty = beg_qty + recv_qty
    avail_amt = beg_amt + recv_amt

    lines = _ordered_events(
        _posted_lines_for_item(session, ledger_set_id, item_code)
    )

    if method == "weighted_avg":
        unit_cost = (avail_amt / avail_qty) if avail_qty > 0 else ZERO
        end_qty = beg_qty + recv_qty - issue_qty
        end_amt = end_qty * unit_cost
        cogs = avail_amt - end_amt
    elif method == "fifo":
        end_amt, cogs = _fifo_ending_value(lines)
        end_qty = beg_qty + recv_qty - issue_qty
        unit_cost = (end_amt / end_qty) if end_qty > 0 else ZERO
    else:  # moving_avg
        end_amt, cogs = _moving_avg_ending_value(lines)
        end_qty = beg_qty + recv_qty - issue_qty
        unit_cost = (end_amt / end_qty) if end_qty > 0 else ZERO

    draft_lines: list[dict] = []
    # ① 分期结转（实地盘存制下结转已销商品成本）
    if cogs > 0:
        draft_lines.append({
            "account_code": "6401",
            "debit": _fmt(cogs), "credit": "",
            "summary": f"结转已销商品成本（{item.name}·{method}）",
            "purpose": "periodic_cogs",
        })
        draft_lines.append({
            "account_code": item.default_account_code,
            "debit": "", "credit": _fmt(cogs),
            "quantity": _fmt(end_qty) if method == "weighted_avg" else "",
            "summary": f"结转已销商品成本（贷：{item.name}）",
            "purpose": "periodic_cogs",
        })

    # ② 实地盘点差异调整（可选）
    variance = None
    if physical_count_qty is not None:
        pc = Decimal(str(physical_count_qty))
        variance = pc - end_qty
        if abs(variance) > Decimal("1e-9"):
            v_amt = abs(variance) * (unit_cost if unit_cost > 0 else ZERO)
            if variance > 0:  # 盘盈
                draft_lines.append({
                    "account_code": item.default_account_code, "debit": _fmt(v_amt),
                    "credit": "", "quantity": _fmt(variance),
                    "summary": f"存货盘盈（{item.name}）", "purpose": "physical_gain",
                })
                draft_lines.append({
                    "account_code": "1901", "debit": "", "credit": _fmt(v_amt),
                    "summary": "待处理财产损溢-盘盈", "purpose": "physical_gain",
                })
            else:  # 盘亏
                draft_lines.append({
                    "account_code": "1901", "debit": _fmt(v_amt), "credit": "",
                    "summary": "待处理财产损溢-盘亏", "purpose": "physical_loss",
                })
                draft_lines.append({
                    "account_code": item.default_account_code, "debit": "",
                    "credit": _fmt(v_amt), "quantity": _fmt(-variance),
                    "summary": f"存货盘亏（{item.name}）", "purpose": "physical_loss",
                })

    return {
        "item_code": item_code,
        "item_name": item.name,
        "period": {"year": year, "month": month},
        "method": method,
        "unit_cost": _fmt(unit_cost),
        "beginning_qty": _fmt(beg_qty),
        "ending_qty": _fmt(end_qty),
        "ending_value": _fmt(end_amt),
        "cogs": _fmt(cogs),
        "physical_count_qty": (None if physical_count_qty is None else _fmt(Decimal(str(physical_count_qty)))),
        "variance": (None if variance is None else _fmt(variance)),
        "lines": draft_lines,
        "summary": (
            f"{item.name}（{method}）：期末 {_fmt(end_qty)}{item.unit or ''}，"
            f"单位成本 {_fmt(unit_cost)}，存货价值 {_fmt(end_amt)}，"
            f"本期结转成本 {_fmt(cogs)}"
            + (f"；盘点差异 {_fmt(variance)}" if variance is not None else "")
        ),
    }
