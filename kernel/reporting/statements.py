"""三大报表投影（P1-01）：资产负债表 / 利润表 / 现金流量表（直接法）。

数据源只有两处：balances 投影（发生额）与凭证明细（现金流分类）。
报表不从余额表反推——全部按映射配置从发生额聚合，保证可回放。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, LedgerSet, Period, Voucher, VoucherLine
from kernel.opening import is_opening_voucher
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


def ending_balance(code: str, debits: Decimal, credits: Decimal) -> Decimal:
    """期末余额：资产/成本/费用类借方为正，其余贷方为正。

    对外暴露是为了让 Web 科目余额表与三大报表共用同一口径，
    避免「报表一个数、页面另一个数」这种最伤信任的不一致。
    """
    if code.startswith(("1", "6")) and not code.startswith(("6001", "6051", "6301")):
        return debits - credits
    return credits - debits


# 内部沿用旧名，避免牵动既有调用点
_ending = ending_balance


def net_profit(session: Session, ledger_set_id: str, year: int, month: int,
               standard: str = "small_business") -> Decimal:
    """本期净利润（利润表末行），供资产负债表平衡使用。"""
    return income_statement(session, ledger_set_id, year, month, standard)["net_profit"]


def balance_sheet(session: Session, ledger_set_id: str, year: int, month: int,
                  standard: str = "small_business",
                  apply_reclass: bool = False) -> dict:
    """资产负债表。

    apply_reclass=True 时启用往来重分类列报（阶段1）：把应收/预付下
    **贷方余额**（实为预收）、应付/预收下**借方余额**（实为预付）按往来
    单位方向搬到对方科目列报——科目对取自本体 relations.csv。
    默认关闭：列报口径变更必须显式选择，历史口径不因升级而漂移。
    """
    mp = M.get_mapping(standard)
    period = _period(session, ledger_set_id, year, month)
    amounts = _amounts_by_code(session, period)
    reclass = None
    if apply_reclass:
        from kernel.reporting.reclass import apply_reclass as _apply

        amounts, reclass = _apply(session, ledger_set_id, period, amounts, standard)

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
        "reclass": reclass,
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
        # 期初及其红字冲销都不是「本期经营成果」，必须排除：否则建账当期
        # 利润表会被期初数（甚至 force 重导时的冲销额）直接污染。
        if is_opening_voucher(v.voucher_no):
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
            # 净额口径：费用=借-贷，收入=贷-借。否则贷方冲减（销售退回/
            # 红字发票/测试对冲）被忽略，净利润错报并污染资产负债表 np 注入。
            total += (cr - dr) if side == "credit" else (dr - cr)
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


def _cat_of(label: str) -> str:
    """现金流项目名 → 三大类别键。"""
    if "投资" in label:
        return "investing"
    if "筹资" in label:
        return "financing"
    return "operating"


def cash_flow(session: Session, ledger_set_id: str, year: int, month: int,
              standard: str = "small_business") -> dict:
    """现金流量表（直接法）：遍历 POSTED 凭证，按对方科目归类现金收支。

    现金流项目语义（②）：科目可通过 attrs ``cash_flow_item`` 声明式指定所属
    现金流项目名（如 6001 主营业务收入 → 「销售商品、提供劳务收到的现金」），
    优先于按对方科目前缀的默认归类；类别（经营/投资/筹资）仍由映射口径推导，
    保证与准则模板一致。期初凭证单独计入「期初现金」，不参与本期三类流量。
    """
    mp = M.get_mapping(standard)
    period = _period(session, ledger_set_id, year, month)
    accounts = {a.id: a.code for a in session.scalars(select(Account)).all()}
    # 科目 attrs（用于 cash_flow_item 声明式覆盖）
    attrs_by_code = {
        a.code: (a.attrs or {}) for a in session.scalars(select(Account)).all()
    }

    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
        )
    ).all()

    items: dict[str, Decimal] = {}
    categories: dict[str, dict] = {
        "operating": {"in": ZERO, "out": ZERO, "items": {}},
        "investing": {"in": ZERO, "out": ZERO, "items": {}},
        "financing": {"in": ZERO, "out": ZERO, "items": {}},
    }
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
        # 期初及其红字冲销 → 归入期初现金，不计入本期三类流量
        if is_opening_voucher(v.voucher_no):
            opening_cash += delta
            continue
        inflow = delta > ZERO
        target = others[0] if others else None
        target_code = accounts.get(target.account_id, "") if target else ""
        custom = attrs_by_code.get(target_code, {}).get("cash_flow_item")
        if custom:
            label = str(custom)
        elif target:
            label = _flow_label(mp, target_code, inflow)
        else:
            label = "经营活动-流入" if inflow else "经营活动-流出"
        items[label] = items.get(label, ZERO) + delta
        # 类别：优先从项目名识别，否则回退到对方科目的映射类别
        cat = _cat_of(label)
        if cat == "operating" and not custom and target_code:
            mb = M.cash_flow_bucket(mp, target_code)
            if mb:
                cat = _cat_of(mb)
        if inflow:
            categories[cat]["in"] += delta
        else:
            categories[cat]["out"] += -delta
        cat_items = categories[cat]["items"]
        cat_items[label] = cat_items.get(label, ZERO) + delta

    op = categories["operating"]["in"] - categories["operating"]["out"]
    inv = categories["investing"]["in"] - categories["investing"]["out"]
    fin = categories["financing"]["in"] - categories["financing"]["out"]
    net_increase = op + inv + fin
    return {
        "ledger_set": ledger_set_id,
        "period": {"year": year, "month": month},
        "standard": standard,
        # 扁平列表（兼容 a2ui / 旧消费方）
        "items": [{"item": k, "amount": v} for k, v in sorted(items.items())],
        # 结构化：经营/投资/筹资 分流入、流出与项目明细
        "categories": {
            "operating": {
                "in": categories["operating"]["in"],
                "out": categories["operating"]["out"],
                "net": op,
                "items": [
                    {"item": k, "amount": v}
                    for k, v in sorted(categories["operating"]["items"].items())
                ],
            },
            "investing": {
                "in": categories["investing"]["in"],
                "out": categories["investing"]["out"],
                "net": inv,
                "items": [
                    {"item": k, "amount": v}
                    for k, v in sorted(categories["investing"]["items"].items())
                ],
            },
            "financing": {
                "in": categories["financing"]["in"],
                "out": categories["financing"]["out"],
                "net": fin,
                "items": [
                    {"item": k, "amount": v}
                    for k, v in sorted(categories["financing"]["items"].items())
                ],
            },
        },
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
