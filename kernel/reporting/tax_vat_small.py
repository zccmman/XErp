"""小规模纳税人增值税及附加税费季报准备（v2.1 / B3）：只读生成申报草稿。

铁律：本模块**只读**账套状态、产出申报草稿（draft），**绝不做任何写动作**
（不制单、不过账、不结账、不替 Boss 报税）。所有数字来自内核既有只读函数
（amounts_by_code / ending_balance）与风险扫描（scan_risks），复用单一口径，
不复制配平/对账逻辑（守 ADR-002）。

政策口径（默认，参数可覆盖）：
    - 征收率 1%：增值税小规模纳税人 3% 减按 1%（政策窗口 2023.1.1–2027.12.31）。
    - 小微免征：季度销售额 ≤ 300,000 元免征增值税；免征同时免征附加税费
      （等价月销售额 ≤ 10 万元）。
    - 附加税费以「实际缴纳的增值税额」为计税依据：
        城建税 7%（市区）/ 5%（县城镇）/ 1%（其他），默认按市区 7%；
        教育费附加 3%；地方教育附加 2%。

销售额口径（MVP 约定）：
    - 应税销售额 = 主营业务收入(6001) + 其他业务收入(6051) 的**本期净额**
      （cr - dr，已扣除销售退回红字），按季度 3 个月聚合。
    - 默认 6001/6051 已按不含税金额入账（小规模常见记账实践，税额单列 222101）。
    - 缺失月份按 0 处理，不臆测。

申报前置检查：复用 B1 风险扫描（scan_risks）。alert 级（资产负债表不平、
货币资金为负）阻断申报；warn 级提示。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import Period
from kernel.reporting.risk_scan import scan_risks
from kernel.reporting.statements import (
    ReportError,
    amounts_by_code,
    ending_balance,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")

# 销售收入科目：主营业务收入 + 其他业务收入（本期净额口径）
SALES_CODES = ("6001", "6051")
# 小微免税阈值：季度销售额 ≤ 30 万免征增值税及附加
EXEMPT_QUARTER_THRESHOLD = Decimal("300000")
# 默认征收率 1%（3% 减按 1% 政策窗口）
DEFAULT_LEVY_RATE = Decimal("0.01")
# 附加税费率（计税依据 = 实际缴纳增值税额）
SURCHARGE_RATES = {
    "city_construction": Decimal("0.07"),  # 市区城建税 7%；县城镇 5% / 其他 1%
    "edu_surcharge": Decimal("0.03"),
    "local_edu_surcharge": Decimal("0.02"),
}


def _resolve_period(s, ls_id: str, yr: int, mo: int) -> tuple[int, int] | None:
    """yr/mo 为 0 时取最新 OPEN 期间，否则原样返回。无期间则 None。"""
    if yr and mo:
        return yr, mo
    per = s.scalars(
        select(Period)
        .where(Period.ledger_set_id == ls_id, Period.status == "OPEN")
        .order_by(Period.year.desc(), Period.month.desc())
    ).first()
    if per is None:
        per = s.scalars(
            select(Period)
            .where(Period.ledger_set_id == ls_id)
            .order_by(Period.year.desc(), Period.month.desc())
        ).first()
    return (per.year, per.month) if per else None


def _quarter_window(yr: int, mo: int) -> tuple[list[tuple[int, int]], int, int, int]:
    """由任意月份推出所属季度 [起月, 止月]，返回 (月份列表, 季度号, 起月, 止月)。"""
    q = (mo - 1) // 3 + 1
    q_end = q * 3
    q_start = q_end - 2
    months = [(yr, m) for m in range(q_start, q_end + 1)]
    return months, q, q_start, q_end


def _quarter_sales(s, ls_id: str, months: list[tuple[int, int]]) -> Decimal:
    """季度应税销售额 = 各月 6001/6051 本期净额（cr-dr）之和；缺月份按 0。"""
    total = ZERO
    for y, m in months:
        try:
            amounts = amounts_by_code(s, ls_id, y, m)
        except ReportError:
            continue  # 该月无期间/无数据 → 视为 0，不臆测
        for code in SALES_CODES:
            dr, cr = amounts.get(code, (ZERO, ZERO))
            total += ending_balance(code, dr, cr)  # 收入净额 cr - dr
    return total


def prep_vat_small(
    s,
    ls_id: str,
    yr: int,
    mo: int,
    standard: str = "small_business",
    levy_rate: Decimal = DEFAULT_LEVY_RATE,
    city_rate: Decimal = SURCHARGE_RATES["city_construction"],
) -> dict:
    """生成小规模纳税人增值税及附加税费季报草稿（只读）。

    返回结构化草稿，含销售额、应纳税额、附加税费与申报前置检查。
    yr/mo 传 0 自动取最新 OPEN 期间并据此季度计算；无可用期间返回 NO_PERIOD。
    """
    resolved = _resolve_period(s, ls_id, yr, mo)
    if resolved is None:
        return _no_period()
    yr, mo = resolved

    period = s.scalars(
        select(Period).where(
            Period.ledger_set_id == ls_id, Period.year == yr, Period.month == mo
        )
    ).first()
    if period is None:
        return _no_period(yr, mo)

    months, quarter, q_start, q_end = _quarter_window(yr, mo)
    quarter_sales = _quarter_sales(s, ls_id, months)

    # 免税判定（季 ≤ 30 万，含净退大于销的特殊情形）
    exempt = quarter_sales <= EXEMPT_QUARTER_THRESHOLD
    if quarter_sales <= ZERO:
        taxable_sales = ZERO
        exempt_sales = ZERO
        vat_due = ZERO
    elif exempt:
        taxable_sales = ZERO
        exempt_sales = quarter_sales
        vat_due = ZERO
    else:
        taxable_sales = quarter_sales
        exempt_sales = ZERO
        vat_due = (quarter_sales * levy_rate).quantize(CENT)

    # 附加税费以实际缴纳增值税额为计税依据；增值税免征则附加亦免征
    if vat_due > ZERO:
        cc = (vat_due * city_rate).quantize(CENT)
        es = (vat_due * SURCHARGE_RATES["edu_surcharge"]).quantize(CENT)
        les = (vat_due * SURCHARGE_RATES["local_edu_surcharge"]).quantize(CENT)
    else:
        cc = es = les = ZERO
    surcharge_total = cc + es + les

    # 申报前置检查：复用 B1 风险扫描
    risks = scan_risks(s, ls_id, yr, mo, standard)
    blocking = [f for f in risks["findings"] if f["severity"] == "alert"]
    warnings = [f for f in risks["findings"] if f["severity"] == "warn"]
    filing_ready = len(blocking) == 0

    return {
        "period": {"year": yr, "month": q_end, "quarter": quarter},
        "quarter_months": {"start": q_start, "end": q_end},
        "standard": standard,
        "filing_type": "小规模增值税及附加税费季报",
        "policy_note": (
            "增值税小规模纳税人，征收率 1%（2023.1.1–2027.12.31 减按 1%）；"
            "季销售额 ≤ 30 万免征增值税及附加。"
        ),
        "sales": {
            "total_sales": quarter_sales,
            "taxable_sales": taxable_sales,
            "exempt_sales": exempt_sales,
        },
        "vat": {
            "levy_rate": str(levy_rate),
            "exempt": exempt,
            "vat_due": vat_due,
        },
        "surcharge": {
            "city_construction": cc,
            "edu_surcharge": es,
            "local_edu_surcharge": les,
            "total": surcharge_total,
        },
        "precheck": {
            "filing_ready": filing_ready,
            "blocking": blocking,
            "warnings": warnings,
            "severity_counts": risks["severity_counts"],
        },
        "summary": _summary(quarter, quarter_sales, exempt, vat_due,
                            surcharge_total, filing_ready, blocking),
    }


def _no_period(yr: int = 0, mo: int = 0) -> dict:
    label = f"{yr}-{mo:02d} " if yr and mo else ""
    return {
        "findings": [{
            "code": "NO_PERIOD",
            "severity": "warn",
            "title": f"{label}无可用会计期间",
            "detail": "账套尚未建立对应的会计期间，无法生成申报草稿。",
            "suggestion": "先初始化期间（首次建账）再开始记账。",
        }],
        "summary": f"{label}无可用会计期间，请先建账。",
        "filing_type": "小规模增值税及附加税费季报",
        "period": {"year": yr, "month": mo, "quarter": 0},
        "quarter_months": {"start": 0, "end": 0},
        "standard": "small_business",
        "policy_note": "",
        "sales": {"total_sales": ZERO, "taxable_sales": ZERO, "exempt_sales": ZERO},
        "vat": {"levy_rate": "0.01", "exempt": True, "vat_due": ZERO},
        "surcharge": {
            "city_construction": ZERO, "edu_surcharge": ZERO,
            "local_edu_surcharge": ZERO, "total": ZERO,
        },
        "precheck": {
            "filing_ready": False, "blocking": [], "warnings": [],
            "severity_counts": {"alert": 0, "warn": 0, "info": 0},
        },
    }


def _summary(quarter, sales, exempt, vat_due, surcharge_total,
             filing_ready, blocking) -> str:
    if not filing_ready:
        return f"⛔ {quarter}季度 申报前置检查未通过：{blocking[0]['title']}"
    if sales <= ZERO:
        return f"📭 {quarter}季度 无应税销售额，无需缴纳增值税及附加。"
    if exempt:
        return (f"✅ {quarter}季度 季销售额 {sales:,.2f} ≤ 30万，"
                f"免征增值税及附加，可直接申报。")
    total = vat_due + surcharge_total
    return (f"🧾 {quarter}季度 应补增值税 {vat_due:,.2f} + 附加 "
            f"{surcharge_total:,.2f}，合计 {total:,.2f}。")
