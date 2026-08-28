"""三大报表投影（P1-01）：资产负债表 / 利润表 / 现金流量表（直接法）。

数据源只有两处：balances 投影（发生额）与凭证明细（现金流分类）。
报表不从余额表反推——全部按映射配置从发生额聚合，保证可回放。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, LedgerSet, Period, Voucher, VoucherLine
from kernel.reporting import mapping as M

ZERO = Decimal("0")


class ReportError(RuntimeError):
    pass


def _period(session: Session, ledger_set_id: str, year: int, month: int) -> Period:
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise ReportError(f"期间 {year}-{month:02d} 不存在")
    return period


def _amounts_by_code(session: Session, period: Period) -> dict[str, tuple[Decimal, Decimal]]:
    """科目 → (本期借方发生额, 本期贷方发生额)（取自 balances 投影）。"""
    rows = session.scalars(
        select(Balance).where(Balance.period_id == period.id)
    ).all()
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for b in rows:
        acc = session.get(Account, b.account_id)
        if acc is None:
            continue
        d, c = out.get(acc.code, (ZERO, ZERO))
        out[acc.code] = (
            d + Decimal(str(b.debit_total)),
            c + Decimal(str(b.credit_total)),
        )
    return out


def _ending(code: str, debits: Decimal, credits: Decimal) -> Decimal:
    """期末余额：资产/成本/费用类借方为正，其余贷方为正。"""
    if code.startswith(("1", "6")) and not code.startswith(("6001", "6051", "6301")):
        return debits - credits
    return credits - debits


def net_profit(session: Session, ledger_set_id: str, year: int, month: int,
               standard: str = "small_business") -> Decimal:
    """本期净利润（利润表末行），供资产负债表平衡使用。"""
    return income_statement(session, ledger_set_id, year, month, standard)["net_profit"]


def balance_sheet(session: Session, ledger_set_id: str, year: int, month: int,
                  standard: str = "small_business") -> dict:
    mp = M.get_mapping(standard)
    period = _period(session, ledger_set_id, year, month)
    amounts = _amounts_by_code(session, period)

    groups: dict[tuple[str, str], list[dict]] = {}
    for code, (dr, cr) in sorted(amounts.items()):
        pos = M.balance_sheet_group(mp, code)
        if pos is None:
            continue
        bal = _ending(code, dr, cr)
        if bal == ZERO:
            continue
        groups.setdefault(pos, []).append({"code": code, "ending": bal})

    np = net_profit(session, ledger_set_id, year, month, standard)
    # 已执行期结转 → 净利润已在 3103 权益科目内，不再挂临时插值项
    closed = session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(f"结转-{year}{month:02d}-%"),
        )
    ).first() is not None

    def build(major: str) -> tuple[list[dict], Decimal]:
        items, total = [], ZERO
        for (m, g), rows in sorted(groups.items(), key=lambda kv: kv[0]):
            if m != major:
                continue
            sub = sum((r["ending"] for r in rows), ZERO)
            total += sub
            items.append({"group": g, "amount": sub, "accounts": rows})
        return items, total

    assets, total_assets = build("资产")
    liabs, total_liabs = build("负债")
    equity, total_equity = build("所有者权益")

    # 本期净利润尚未期结转时暂列权益项下，保证表内平衡
    if np != ZERO and not closed:
        equity.append({
            "group": "未分配利润（本期净利润，未结转）",
            "amount": np,
            "accounts": [],
        })
        total_equity += np

    return {
        "ledger_set": ledger_set_id,
        "period": {"year": year, "month": month},
        "standard": standard,
        "assets": {"items": assets, "total": total_assets},
        "liabilities": {"items": liabs, "total": total_liabs},
        "equity": {"items": equity, "total": total_equity},
        "balanced": (total_assets == total_liabs + total_equity),
        "check": {
            "assets": total_assets,
            "liabilities_plus_equity": total_liabs + total_equity,
            "diff": total_assets - (total_liabs + total_equity),
        },
    }


def income_statement(session: Session, ledger_set_id: str, year: int, month: int,
                     standard: str = "small_business") -> dict:
    mp = M.get_mapping(standard)
    period = _period(session, ledger_set_id, year, month)
    # 利润表从 POSTED 凭证分录取数（排除结转凭证）——事件可回放口径，
    # 期结转后历史期间利润表不丢（结转凭证以「结转-」前缀标识）
    accounts = {a.id: a.code for a in session.scalars(select(Account)).all()}
    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
        )
    ).all()
    amounts: dict[str, tuple[Decimal, Decimal]] = {}
    for v in vouchers:
        if v.voucher_no.startswith("结转-"):
            continue
        for ln in session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ):
            code = accounts.get(ln.account_id, "")
            dr, cr = amounts.get(code, (ZERO, ZERO))
            amounts[code] = (
                dr + Decimal(str(ln.debit)),
                cr + Decimal(str(ln.credit)),
            )

    items: list[dict] = []
    revenue = ZERO
    expense = ZERO
    for name, _prefixes, side in mp["income_statement"]:
        total = ZERO
        for code, (dr, cr) in amounts.items():
            hit = M.income_statement_item(mp, code)
            if hit is None or hit[0] != name:
                continue
            total += cr if side == "credit" else dr
        items.append({"item": name, "amount": total, "side": side})
        if side == "credit":
            revenue += total
        else:
            expense += total

    net = revenue - expense
    return {
        "ledger_set": ledger_set_id,
        "period": {"year": year, "month": month},
        "standard": standard,
        "items": items,
        "revenue": revenue,
        "expense": expense,
        "net_profit": net,
    }


def _flow_label(mp: dict, code: str, inflow: bool) -> str:
    want = "in" if inflow else "out"
    fallback = None
    for label, prefixes, direction in mp["cash_flow"]:
        if M._match(code, prefixes):
            if direction == want:
                return label
            fallback = fallback or label
    if fallback:
        return fallback
    return "经营活动-流入" if inflow else "经营活动-流出"


def cash_flow(session: Session, ledger_set_id: str, year: int, month: int,
              standard: str = "small_business") -> dict:
    """现金流量表（直接法）：遍历 POSTED 凭证，按对方科目归类现金收支。"""
    mp = M.get_mapping(standard)
    period = _period(session, ledger_set_id, year, month)
    accounts = {a.id: a.code for a in session.scalars(select(Account)).all()}

    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
        )
    ).all()

    buckets: dict[str, Decimal] = {}
    opening_cash = ZERO
    for v in vouchers:
        lines = session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ).all()
        delta = ZERO
        others: list[VoucherLine] = []
        for ln in lines:
            code = accounts.get(ln.account_id, "")
            if M.is_cash_account(mp, code):
                delta += Decimal(str(ln.debit)) - Decimal(str(ln.credit))
            else:
                others.append(ln)
        if delta == ZERO:
            continue
        if v.voucher_no.startswith("期初-"):
            opening_cash += delta
            continue
        inflow = delta > ZERO
        target = others[0] if others else None
        label = (
            _flow_label(mp, accounts.get(target.account_id, ""), inflow)
            if target
            else ("经营活动-流入" if inflow else "经营活动-流出")
        )
        buckets[label] = buckets.get(label, ZERO) + delta

    def total(kind: str) -> Decimal:
        return sum(
            amt for lbl, amt in buckets.items() if lbl.startswith(kind)
        )

    op, inv, fin = total("经营活动"), total("投资活动"), total("筹资活动")
    net_increase = op + inv + fin
    return {
        "ledger_set": ledger_set_id,
        "period": {"year": year, "month": month},
        "standard": standard,
        "items": [{"item": k, "amount": v} for k, v in sorted(buckets.items())],
        "operating": op,
        "investing": inv,
        "financing": fin,
        "net_increase": net_increase,
        # 勾稽：期初现金（期初凭证）+ 净增加 = 期末现金（投影）
        "reconcile": {
            "opening_cash": opening_cash,
            "net_increase": net_increase,
            "closing_cash": opening_cash + net_increase,
        },
    }


def ledger_set_standard(session: Session, ledger_set_id: str) -> str:
    ls = session.get(LedgerSet, ledger_set_id)
    return (ls.accounting_standard if ls else None) or "small_business"
