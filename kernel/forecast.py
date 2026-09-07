"""三表前向预测（P1-01 预测）：以历史实际三表为种子 + 可编辑驱动假设外推未来 N 期。

设计原则（与 P1 本体一致）：
- 预测是「物化视图」不是事实源，全部由驱动假设确定性推导，可回放、可审计。
- 三表内部完全勾稽：净利润→留存收益→资产负债表；折旧→现金流加回；
  营运资本变动（应收/应付/存货）连接权责发生制与现金流；capex→固定资产→折旧。
- 纯 Decimal 运算，无 LLM、无随机，便于契约测试钉死勾稽关系。

种子来自 kernel/reporting/statements（已落地的实际三表），预测模块不反向依赖账本内核。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, LedgerSet, Period
from kernel.reporting import mapping as M
from kernel.reporting.statements import (
    ReportError,
    balance_sheet,
    ending_balance,
    income_statement,
)

ZERO = Decimal("0")
PPY = 12  # 预测以月为步长，每年 12 期
_Q = Decimal("0.01")  # 金额量子：真实货币以分为最小单位，量化避免浮点式漂移


def _q(x: Decimal) -> Decimal:
    """金额量化到分。既符合真实货币口径，又使三表勾稽保持精确等式。"""
    return x.quantize(_Q)

# ---- 科目编码前缀分类（小企业/一般企业准则共用 GAAP 编码结构） ----
CASH_PREFIX = ("1001", "1002")
RECEIVABLE_PREFIX = ("1121", "1122", "1123", "1131", "1221", "1231")
INVENTORY_PREFIX = ("1403", "1405", "1408", "1471")
FA_GROSS_PREFIX = ("1501", "1503", "1601", "1604", "1701", "1801", "1901")
ACCUM_DEP_PREFIX = ("1602", "1702")
PAYABLE_PREFIX = ("2201", "2202", "2203", "2211", "2221", "2231", "2232", "2241")
PAID_IN_PREFIX = ("3001", "3002")
RE_PREFIX = ("3101", "3103", "3104")


def _startsany(code: str, prefixes: tuple[str, ...]) -> bool:
    return code.startswith(prefixes)


@dataclass
class Seed:
    """预测起点（上期末实际数）。所有金额单位与账本一致（Decimal 字符串）。"""

    cash: Decimal = ZERO
    ar: Decimal = ZERO
    ap: Decimal = ZERO
    inventory: Decimal = ZERO
    fa_gross: Decimal = ZERO
    accum_dep: Decimal = ZERO
    paid_in_capital: Decimal = ZERO
    retained_earnings: Decimal = ZERO
    # 基期（最近一期）月度营业收入，作为增长锚点
    revenue: Decimal = ZERO
    # 未分类资产/负债残差，保持预测表与实际上期末平衡
    other_assets: Decimal = ZERO
    other_liabilities: Decimal = ZERO

    @property
    def fa_net(self) -> Decimal:
        return self.fa_gross - self.accum_dep


@dataclass
class Assumptions:
    """驱动假设（全部可编辑，缺省由实际数推导）。"""

    rev_growth: Decimal = Decimal("0.03")      # 月度收入增长率
    gross_margin: Decimal = Decimal("0.60")    # 营业成本/营业收入
    opex_ratio: Decimal = Decimal("0.25")      # 现金费用（不含折旧）/营业收入
    tax_rate: Decimal = Decimal("0.00")        # 所得税率（小规模优惠默认 0）
    ar_days: int = 30                          # 应收账款周转天数
    ap_days: int = 30                          # 应付账款周转天数
    inv_days: int = 30                         # 存货周转天数
    capex_pct: Decimal = Decimal("0.00")       # 资本开支/营业收入
    dep_rate: Decimal = Decimal("0.10")        # 年折旧率（按 fa_gross）
    dividend_pct: Decimal = Decimal("0.00")    # 股利/净利润
    debt_draw: Decimal = ZERO                  # 每期新增筹资（净流入）
    periods_per_year: int = PPY


# 默认情景缩放系数（best/base/worst）
def build_scenarios(base: Assumptions) -> dict[str, Assumptions]:
    """由基准假设派生 best/base/worst 三情景。"""
    best = replace(
        base,
        rev_growth=base.rev_growth * Decimal("1.5"),
        gross_margin=base.gross_margin * Decimal("0.9"),
        opex_ratio=base.opex_ratio * Decimal("0.9"),
    )
    worst = replace(
        base,
        rev_growth=-abs(base.rev_growth) * Decimal("0.5")
        if base.rev_growth > ZERO
        else Decimal("-0.02"),
        gross_margin=min(Decimal("0.95"), base.gross_margin * Decimal("1.1")),
        opex_ratio=base.opex_ratio * Decimal("1.15"),
    )
    return {"best": best, "base": base, "worst": worst}


@dataclass
class PeriodResult:
    year: int
    month: int
    income: dict
    balance: dict
    cashflow: dict


def _classify_endings(session: Session, period: Period) -> dict[str, Decimal]:
    """返回 科目编码 → 期末余额（含未分类聚合）。"""
    out: dict[str, Decimal] = {}
    rows = session.scalars(
        select(Balance).where(Balance.period_id == period.id)
    ).all()
    for b in rows:
        acc = session.get(Account, b.account_id)
        if acc is None:
            continue
        end = ending_balance(
            acc.code, Decimal(str(b.debit_total)), Decimal(str(b.credit_total))
        )
        out[acc.code] = out.get(acc.code, ZERO) + end
    return out


def extract_seed_from_actuals(
    session: Session,
    ledger_set_id: str,
    year: int,
    month: int,
    standard: str = "small_business",
) -> tuple[Seed, Assumptions]:
    """从上期末实际三表抽取预测种子，并推导默认驱动假设。"""
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise ReportError(f"种子期间 {year}-{month:02d} 不存在")

    bs = balance_sheet(session, ledger_set_id, year, month, standard)
    is_ = income_statement(session, ledger_set_id, year, month, standard)
    ends = _classify_endings(session, period)

    cash = ZERO
    ar = ZERO
    inv = ZERO
    fa_gross = ZERO
    accum_dep = ZERO
    ap = ZERO
    paid_in = ZERO
    re = ZERO
    other_assets = ZERO
    other_liab = ZERO

    for code, end in ends.items():
        if _startsany(code, CASH_PREFIX):
            cash += end
        elif _startsany(code, RECEIVABLE_PREFIX):
            ar += end
        elif _startsany(code, INVENTORY_PREFIX):
            inv += end
        elif _startsany(code, FA_GROSS_PREFIX):
            fa_gross += end
        elif _startsany(code, ACCUM_DEP_PREFIX):
            accum_dep += end
        elif _startsany(code, PAYABLE_PREFIX):
            ap += end
        elif _startsany(code, PAID_IN_PREFIX):
            paid_in += end
        elif _startsany(code, RE_PREFIX):
            re += end
        else:
            # 归类不到驱动项的资产/负债，按借贷方向并入残差
            if code.startswith(("1", "6")) and not code.startswith(("6001", "6051", "6301")):
                other_assets += end
            else:
                other_liab += end

    # 以实际三表总额校正残差，保证预测起点表与实际期末严格平衡。
    # 资产/负债侧：未归类项并入 other_* 残差；权益侧：实收资本单列，
    # 留存收益用「权益总额 − 实收资本」反推，确保种子表本身借贷平衡
    # （任何未识别的权益子项都被折入留存收益，不污染负债侧）。
    classified_assets = cash + ar + inv + (fa_gross - accum_dep)
    other_assets = bs["assets"]["total"] - classified_assets
    other_liab = bs["liabilities"]["total"] - ap
    if other_assets < ZERO:
        other_assets = ZERO
    if other_liab < ZERO:
        other_liab = ZERO
    re = bs["equity"]["total"] - paid_in

    revenue = is_["revenue"]
    cogs = ZERO
    opex_excl_dep = ZERO
    for it in is_["items"]:
        if it["item"] == "营业成本":
            cogs += it["amount"]
        elif it["side"] == "debit" and it["item"] != "营业成本":
            opex_excl_dep += it["amount"]

    monthly_rev = revenue / PPY if revenue > ZERO else ZERO
    monthly_cogs = cogs / PPY if cogs > ZERO else ZERO

    gross_margin = (cogs / revenue) if revenue > ZERO else Decimal("0.60")
    opex_ratio = (opex_excl_dep / revenue) if revenue > ZERO else Decimal("0.25")
    ar_days = int((ar / monthly_rev).to_integral_value()) if monthly_rev > ZERO else 30
    inv_days = int((inv / monthly_cogs).to_integral_value()) if monthly_cogs > ZERO else 30
    ap_days = int((ap / monthly_cogs).to_integral_value()) if monthly_cogs > ZERO else 30
    ar_days = max(1, min(ar_days, 365))
    inv_days = max(1, min(inv_days, 365))
    ap_days = max(1, min(ap_days, 365))

    seed = Seed(
        cash=cash,
        ar=ar,
        ap=ap,
        inventory=inv,
        fa_gross=fa_gross,
        accum_dep=accum_dep,
        paid_in_capital=paid_in,
        retained_earnings=re,
        revenue=revenue,
        other_assets=other_assets,
        other_liabilities=other_liab,
    )
    assumptions = Assumptions(
        gross_margin=gross_margin,
        opex_ratio=opex_ratio,
        ar_days=ar_days,
        ap_days=ap_days,
        inv_days=inv_days,
    )
    return seed, assumptions


def _next_period(year: int, month: int) -> tuple[int, int]:
    if month >= 12:
        return year + 1, 1
    return year, month + 1


def forecast_statements(
    seed: Seed,
    assumptions: Assumptions,
    horizon: int,
    start_year: int,
    start_month: int,
    scenario: str = "base",
) -> dict:
    """外推未来 horizon 期三表，返回带勾稽校验的结构。

    纯函数：不碰数据库，便于契约测试钉死勾稽。
    """
    if horizon < 1:
        raise ReportError("horizon 必须 ≥ 1")
    a = assumptions
    ppy = Decimal(str(a.periods_per_year))

    # 运行态（上期末实际数）
    prev_cash = seed.cash
    prev_ar = seed.ar
    prev_ap = seed.ap
    prev_inv = seed.inventory
    prev_fa_gross = seed.fa_gross
    prev_accum_dep = seed.accum_dep
    prev_re = seed.retained_earnings
    prev_rev = seed.revenue

    y, m = start_year, start_month
    periods: list[PeriodResult] = []

    for _ in range(horizon):
        # ---- 利润表 ----
        rev = _q(prev_rev * (Decimal("1") + a.rev_growth))
        cogs = _q(rev * a.gross_margin)
        gp = _q(rev - cogs)
        depr = _q(prev_fa_gross * (a.dep_rate / ppy))
        opex_cash = _q(rev * a.opex_ratio)
        total_opex = _q(opex_cash + depr)
        ebit = _q(gp - total_opex)
        tax = _q(ebit * a.tax_rate) if ebit > ZERO else ZERO
        ni = _q(ebit - tax)

        # ---- 资产负债表（驱动项） ----
        ar = _q(rev * (Decimal(str(a.ar_days)) / ppy))
        ap = _q(cogs * (Decimal(str(a.ap_days)) / ppy))
        inv = _q(cogs * (Decimal(str(a.inv_days)) / ppy))
        capex = _q(rev * a.capex_pct)
        fa_gross = _q(prev_fa_gross + capex)
        accum_dep = _q(prev_accum_dep + depr)
        fa_net = _q(fa_gross - accum_dep)
        div = _q(ni * a.dividend_pct)
        re = _q(prev_re + ni - div)
        equity = _q(seed.paid_in_capital + re)

        # ---- 现金流 ----
        cfo = _q(
            ni
            + depr
            - (ar - prev_ar)
            - (inv - prev_inv)
            + (ap - prev_ap)
        )
        cfi = _q(-capex)
        cff = _q(a.debt_draw - div)
        dcash = _q(cfo + cfi + cff)
        cash = _q(prev_cash + dcash)

        total_assets = _q(cash + ar + inv + fa_net + seed.other_assets)
        total_liab = _q(ap + seed.other_liabilities)
        total_equity = equity
        diff = total_assets - (total_liab + total_equity)

        income = {
            "revenue": rev,
            "cogs": cogs,
            "gross_profit": gp,
            "depreciation": depr,
            "opex_cash": opex_cash,
            "total_opex": total_opex,
            "ebit": ebit,
            "tax": tax,
            "net_profit": ni,
            "items": [
                {"item": "营业收入", "amount": rev},
                {"item": "营业成本", "amount": cogs},
                {"item": "毛利", "amount": gp},
                {"item": "折旧摊销", "amount": depr},
                {"item": "付现费用", "amount": opex_cash},
                {"item": "息税前利润", "amount": ebit},
                {"item": "所得税", "amount": tax},
                {"item": "净利润", "amount": ni},
            ],
        }
        balance = {
            "cash": cash,
            "ar": ar,
            "ap": ap,
            "inventory": inv,
            "fa_gross": fa_gross,
            "accum_dep": accum_dep,
            "fa_net": fa_net,
            "retained_earnings": re,
            "equity": equity,
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "total_equity": total_equity,
            "balanced": diff == ZERO,
            "check": {
                "assets": total_assets,
                "liabilities_plus_equity": total_liab + total_equity,
                "diff": diff,
            },
        }
        cashflow = {
            "operating": cfo,
            "investing": cfi,
            "financing": cff,
            "net_increase": dcash,
            "opening_cash": prev_cash,
            "closing_cash": cash,
            "reconcile": {
                "opening_cash": prev_cash,
                "net_increase": dcash,
                "closing_cash": cash,
                "ok": (prev_cash + dcash) == cash,
            },
        }

        periods.append(PeriodResult(y, m, income, balance, cashflow))

        # 推进运行态到下期末（量化后保持分位一致）
        prev_cash, prev_ar, prev_ap = cash, ar, ap
        prev_inv, prev_fa_gross, prev_accum_dep = inv, fa_gross, accum_dep
        prev_re, prev_rev = re, rev
        y, m = _next_period(y, m)

    return {
        "scenario": scenario,
        "horizon": horizon,
        "start_period": {"year": start_year, "month": start_month},
        "assumptions": {
            "rev_growth": str(a.rev_growth),
            "gross_margin": str(a.gross_margin),
            "opex_ratio": str(a.opex_ratio),
            "tax_rate": str(a.tax_rate),
            "ar_days": a.ar_days,
            "ap_days": a.ap_days,
            "inv_days": a.inv_days,
            "capex_pct": str(a.capex_pct),
            "dep_rate": str(a.dep_rate),
            "dividend_pct": str(a.dividend_pct),
            "debt_draw": str(a.debt_draw),
        },
        "periods": [
            {
                "year": p.year,
                "month": p.month,
                "income_statement": p.income,
                "balance_sheet": p.balance,
                "cash_flow": p.cashflow,
            }
            for p in periods
        ],
    }


def forecast_from_actuals(
    session: Session,
    ledger_set_id: str,
    base_year: int,
    base_month: int,
    horizon: int = 12,
    scenario: str = "base",
    assumptions_override: Assumptions | None = None,
    standard: str | None = None,
) -> dict:
    """端到端：从上期末实际数抽取种子→预测。scenario='all' 返回三情景。"""
    if standard is None:
        ls = session.get(LedgerSet, ledger_set_id)
        standard = (ls.accounting_standard if ls else None) or "small_business"

    seed, derived = extract_seed_from_actuals(
        session, ledger_set_id, base_year, base_month, standard
    )
    base = assumptions_override or derived

    ny, nm = _next_period(base_year, base_month)

    if scenario == "all":
        out = {}
        for name, asm in build_scenarios(base).items():
            out[name] = forecast_statements(seed, asm, horizon, ny, nm, name)
        return {"scenarios": out, "standard": standard, "base_period": {"year": base_year, "month": base_month}}

    asm = base
    if scenario == "best":
        asm = build_scenarios(base)["best"]
    elif scenario == "worst":
        asm = build_scenarios(base)["worst"]
    return forecast_statements(seed, asm, horizon, ny, nm, scenario)
