"""税务健康分 + 申报日历（v2.1 / P-09）：只读生成税务就绪度仪表数据。

铁律：本模块**只读**账套状态、产出税务健康分与申报日历，**绝不做任何写动作**
（不制单、不过账、不结账、不替 Boss 报税）。所有数字来自内核既有只读函数
（`prep_vat_small` 已内含 `scan_risks`、`amounts_by_code` / `ending_balance` /
`balance_sheet`），复用单一口径，不复制配平/对账逻辑（守 ADR-002）。

设计要点：
    - 税务健康分 = 以 100 为基、按风险/阻断/逾期未结账扣分得到的 0–100 综合分，
      并给出 优/良/中/差 评级。分数可被账套数据证伪，缺数据不臆测（返回 info）。
    - 申报日历 = 按小规模纳税人「季报」口径（季度终了次月 15 日截止）推算当年
      4 个季度的申报窗口，并交叉账套实际期间标注 无数据/待办/临近/逾期/已结转。
    - 与 D6 决策对齐：仅做「只读准备 + 人申报」，**绝不代申报**（绝不写申报状态、
      绝不调用电子税务局 API）。副轨直连（需资质/高风险）不在本模块范围。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import Period
from kernel.reporting.tax_vat_small import prep_vat_small

ZERO = Decimal("0")

# 健康分扣分项（每类封顶，避免单类刷爆）
_PEN_ALERT = 15          # 每项 alert 级风险
_CAP_ALERT = 60          # alert 总扣分上限
_PEN_WARN = 5            # 每项 warn 级风险
_CAP_WARN = 25           # warn 总扣分上限
_PEN_BLOCKING = 20       # 存在阻断申报的 alert 问题
_PEN_OVERDUE_UNCLOSED = 10  # 申报窗口已过但季度仍未结转


def _quarter_deadline(yr: int, q: int) -> date:
    """小规模纳税人季报截止日：季度终了次月 15 日。

    Q4（止月 12）截止日为次年 1/15；其余为 (q*3+1)/15。
    """
    if q == 4:
        return date(yr + 1, 1, 15)
    return date(yr, q * 3 + 1, 15)


def _period_status(s, ls_id: str, yr: int, mo: int) -> str | None:
    """查询某年某月期间的 status；不存在返回 None。"""
    per = s.scalars(
        select(Period).where(
            Period.ledger_set_id == ls_id, Period.year == yr, Period.month == mo
        )
    ).first()
    return per.status if per else None


def tax_health_score(
    s,
    ls_id: str,
    yr: int,
    mo: int,
    standard: str = "small_business",
    today: date | None = None,
) -> dict:
    """生成税务健康分（只读）。

    入参：yr/mo 传 0 自动取最新 OPEN 期间（交由 prep_vat_small 解析）。
    today 默认 date.today()，可覆盖以便测试确定性。

    返回：
        score:            0–100 整数综合分
        grade:            优 / 良 / 中 / 差
        factors:          list[{reason, delta}] 扣分明细（可解释）
        prep:             prep_vat_small 的申报准备结果（透传，含 vat/surcharge/precheck）
        severity_counts:   alert/warn/info 计数（透传）
        filing_ready:     是否通过申报前置检查（透传）
        overdue_unclear:  申报窗口已过且季度未结转（bool）
        summary:          一句话总览
    """
    today = today or date.today()
    prep = prep_vat_small(s, ls_id, yr, mo, standard)
    sev = prep.get("precheck", {}).get("severity_counts", {"alert": 0, "warn": 0, "info": 0})
    alert = sev.get("alert", 0)
    warn = sev.get("warn", 0)
    blocking = prep.get("precheck", {}).get("blocking", [])
    quarter = prep.get("period", {}).get("quarter", 0)

    score = 100
    factors: list[dict[str, Any]] = []

    if alert:
        d = min(alert * _PEN_ALERT, _CAP_ALERT)
        score -= d
        factors.append({"reason": f"{alert} 项 alert 级风险（资产负债表/现金等）", "delta": -d})
    if warn:
        d = min(warn * _PEN_WARN, _CAP_WARN)
        score -= d
        factors.append({"reason": f"{warn} 项 warn 级风险（往来方向/大额/未结账等）", "delta": -d})
    if blocking:
        score -= _PEN_BLOCKING
        factors.append({"reason": "存在阻断申报的 alert 级问题", "delta": -_PEN_BLOCKING})

    overdue_unclear = False
    if quarter and quarter > 0:
        q_end = quarter * 3
        deadline = _quarter_deadline(prep["period"]["year"], quarter)
        status = _period_status(s, ls_id, prep["period"]["year"], q_end)
        if deadline < today and status != "CLOSED":
            score -= _PEN_OVERDUE_UNCLOSED
            factors.append({
                "reason": f"{quarter}季度申报窗口已于 {deadline.isoformat()} 截止且账套仍未结转",
                "delta": -_PEN_OVERDUE_UNCLOSED,
            })
            overdue_unclear = True

    score = max(0, min(100, score))
    grade = "优" if score >= 90 else "良" if score >= 75 else "中" if score >= 60 else "差"

    summary = (
        f"税务健康分 {score}（{grade}）："
        + ("数据健康、可申报。" if not factors else "；".join(f["reason"] for f in factors) + "。")
    )

    return {
        "score": score,
        "grade": grade,
        "factors": factors,
        "prep": prep,
        "severity_counts": sev,
        "filing_ready": prep.get("precheck", {}).get("filing_ready", False),
        "overdue_unclear": overdue_unclear,
        "summary": summary,
    }


def filing_calendar(
    s,
    ls_id: str,
    yr: int,
    standard: str = "small_business",
    today: date | None = None,
) -> dict:
    """生成当年（yr）小规模纳税人季报申报日历（只读）。

    对 4 个季度各推算截止日，并交叉账套实际期间标注状态：
        no_data   该季度账套无对应期间（未记账）
        upcoming  窗口未到
        due_soon  距截止 ≤ 7 天
        overdue   已过截止且季度未结转
        closed    季度已结转（视为已完成）
    同时透传该季度的申报准备结果（data_ready / vat_due / exempt / total_sales）。
    """
    today = today or date.today()
    quarters: list[dict[str, Any]] = []

    for q in range(1, 5):
        q_end = q * 3
        deadline = _quarter_deadline(yr, q)
        # 取该季度止月做申报准备解析（prep_vat_small 会归到所属季度）
        prep = prep_vat_small(s, ls_id, yr, q_end, standard)
        has_data = prep.get("period", {}).get("quarter", 0) == q
        status = _period_status(s, ls_id, yr, q_end)

        if not has_data:
            state = "no_data"
        elif status == "CLOSED":
            state = "closed"
        elif deadline < today:
            state = "overdue"
        elif (deadline - today).days <= 7:
            state = "due_soon"
        else:
            state = "upcoming"

        entry = {
            "quarter": q,
            "months": [q_end - 2, q_end - 1, q_end],
            "deadline": deadline.isoformat(),
            "days_left": (deadline - today).days,
            "state": state,
            "data_ready": prep.get("precheck", {}).get("filing_ready", False) if has_data else False,
            "total_sales": prep.get("sales", {}).get("total_sales", ZERO),
            "exempt": prep.get("vat", {}).get("exempt", True),
            "vat_due": prep.get("vat", {}).get("vat_due", ZERO),
        }
        quarters.append(entry)

    due = [q for q in quarters if q["state"] in ("due_soon", "overdue")]
    summary = (
        f"{yr} 年共 {len(quarters)} 个申报窗口，"
        + (f"{len(due)} 个临近/逾期待处理。" if due else "暂无临近/逾期事项。")
    )

    return {
        "year": yr,
        "standard": standard,
        "today": today.isoformat(),
        "quarters": quarters,
        "due_count": len(due),
        "summary": summary,
    }
