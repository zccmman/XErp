"""AI 风险预警（v2.1 / B1）：只读扫描小微企业常见财务风险。

铁律：本模块**只读**账套状态、产出风险发现（findings），**绝不做任何写动作**
（不制单、不过账、不结账、不审批）。所有数字来自内核既有只读函数
（amounts_by_code / balance_sheet）与受控 SQL 查询，复用 ending_balance 单一口径，
不复制配平/对账逻辑（守 ADR-002）。

设计原则 · 宁可漏报不误报：
    - 缺数据就跳过，不臆测；每条 finding 都应可被账套数据证伪。
    - 单期期末余额已从 Balance 投影折入期初，故 amounts_by_code(目标期)
      + ending_balance 即得真实期末余额，**绝不跨期相加**（见 models.Balance 语义契约）。

每类风险：
    R1  BS_UNBALANCED      资产负债表不平衡（alert）
    R2  NEGATIVE_CASH       现金/银行存款为负（alert）
    R3  AR_CREDIT_BALANCE  应收类科目出现贷方余额（warn，重分类/挂账方向异常）
    R4  AP_DEBIT_BALANCE   应付类科目出现借方余额（warn，重分类/挂账方向异常）
    R5  LARGE_AMOUNT       异常大额凭证行（warn）
    R6  UNCLOSED_HISTORY   历史期间仍未结账（warn）

每条 finding：{code, severity, title, detail, suggestion}，severity ∈ alert|warn|info。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import Account, Period, Voucher, VoucherLine
from kernel.reporting.statements import (
    amounts_by_code,
    balance_sheet,
    ending_balance,
)

ZERO = Decimal("0")

# 现金类科目（货币资金）：期末余额不允许为负
CASH_CODES = ("1001", "1002")
# 应收类（常态借方）与应付类（常态贷方）科目前缀
AR_PREFIXES = ("1122", "1123", "1221")  # 应收票据及账款 / 预付账款 / 其他应收
AP_PREFIXES = ("2202", "2203", "2241")  # 应付账款 / 预收账款 / 其他应付
# 异常大额阈值：期间 POSTED 凭证行金额中位数 × 倍数，且绝对值不低于 floor
LARGE_MULTIPLE = Decimal("10")
LARGE_FLOOR = Decimal("100000")


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


def _is_closed(s, ls_id: str, yr: int, mo: int) -> bool:
    """期间是否已期末结转（与 sprite_push/_boss_data 同一判定）。"""
    return (
        s.scalars(
            select(Voucher.id).where(
                Voucher.ledger_set_id == ls_id,
                Voucher.voucher_no.like(f"结转-{yr}{mo:02d}-%"),
            )
        ).first()
        is not None
    )


def scan_risks(
    s, ls_id: str, yr: int, mo: int, standard: str = "small_business"
) -> dict:
    """对某账套某期间做只读风险扫描，返回结构化 findings。

    返回：
        findings:        list[dict] 风险发现
        summary:         一句话总览
        severity_counts: {alert, warn, info}
        period_status:   none | OPEN | CLOSED | ...
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

    period_status = period.status
    findings: list[dict[str, Any]] = []
    amounts = amounts_by_code(s, ls_id, yr, mo)

    # R1 资产负债表不平衡（资产 ≠ 负债 + 权益）
    bs = balance_sheet(s, ls_id, yr, mo, standard)
    if not bs.get("balanced"):
        diff = bs.get("check", {}).get("diff", ZERO)
        findings.append({
            "code": "BS_UNBALANCED",
            "severity": "alert",
            "title": f"{yr}-{mo:02d} 资产负债表不平衡",
            "detail": f"资产与负债+权益相差 {diff:,.2f}，表内不平，禁止月结。",
            "suggestion": "先运行账账核对 reconcile_ledger 定位投影与凭证明细的差异源头。",
        })

    # R2 现金/银行存款为负；R3/R4 往来方向异常
    for code, (dr, cr) in sorted(amounts.items()):
        if dr == ZERO and cr == ZERO:
            continue
        end = ending_balance(code, dr, cr)
        if code in CASH_CODES and end < 0:
            findings.append({
                "code": "NEGATIVE_CASH",
                "severity": "alert",
                "title": f"科目 {code} 出现负余额 {end:,.2f}",
                "detail": "货币资金不允许为负，通常是记账方向写反或重复付款。",
                "suggestion": "核对涉及该科目的凭证借贷方向，必要时红字冲销后重做。",
            })
        if code.startswith(AR_PREFIXES) and end < 0:
            findings.append({
                "code": "AR_CREDIT_BALANCE",
                "severity": "warn",
                "title": f"应收类科目 {code} 出现贷方余额（预收/重分类疑似）{abs(end):,.2f}",
                "detail": "应收出现贷方余额，疑似预收/客户多付款误挂应收，或长期挂账未清理。",
                "suggestion": "核对该科目明细账，必要时重分类至预收款项或红字冲销。",
            })
        if code.startswith(AP_PREFIXES) and end < 0:
            findings.append({
                "code": "AP_DEBIT_BALANCE",
                "severity": "warn",
                "title": f"应付类科目 {code} 出现借方余额（多付/重分类疑似）{abs(end):,.2f}",
                "detail": "应付出现借方余额，疑似预付/供应商多收款误挂应付，或长期挂账未清理。",
                "suggestion": "核对该科目明细账，必要时重分类至预付款项或红字冲销。",
            })

    # R5 异常大额凭证行
    findings.extend(_large_amount_findings(s, ls_id, period.id))
    # R6 历史期间未结账
    findings.extend(_unclosed_history_findings(s, ls_id, yr, mo))

    severity_counts = {
        "alert": sum(1 for f in findings if f["severity"] == "alert"),
        "warn": sum(1 for f in findings if f["severity"] == "warn"),
        "info": sum(1 for f in findings if f["severity"] == "info"),
    }
    return {
        "findings": findings,
        "summary": _summary(findings, yr, mo),
        "severity_counts": severity_counts,
        "period_status": period_status,
    }


def _large_amount_findings(s, ls_id: str, period_id: str) -> list[dict]:
    """异常大额：期间 POSTED 凭证行中金额显著高于中位数的样例。

    仅统计该期间、该账套的 POSTED 行；样本不足（<5 笔）则跳过，不臆测。
    """
    rows = s.execute(
        select(VoucherLine.debit, VoucherLine.credit)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(Voucher.period_id == period_id, Voucher.status == "POSTED")
    ).all()
    amounts = [(dr or ZERO) + (cr or ZERO) for dr, cr in rows if (dr or ZERO) + (cr or ZERO) > ZERO]
    if len(amounts) < 5:
        return []
    amounts.sort()
    n = len(amounts)
    median = amounts[n // 2]
    threshold = max(LARGE_FLOOR, median * LARGE_MULTIPLE)
    hits = [a for a in amounts if a >= threshold]
    if not hits:
        return []
    top = sorted(hits, reverse=True)[:5]
    return [{
        "code": "LARGE_AMOUNT",
        "severity": "warn",
        "title": f"发现 {len(hits)} 笔异常大额凭证行（单笔 ≥ {threshold:,.0f}）",
        "detail": (
            f"期间凭证行金额中位数为 {median:,.2f}，最大单笔 {top[0]:,.2f}。"
            f"大额交易建议复核业务真实性、合同与发票。"
        ),
        "suggestion": "逐笔核对大额凭证的附件、合同与银行流水，确认非错记或重复记账。",
    }]


def _unclosed_history_findings(s, ls_id: str, yr: int, mo: int) -> list[dict]:
    """历史期间未结账：早于所查期间、仍 OPEN 且未做期末结转。"""
    open_periods = s.scalars(
        select(Period).where(Period.ledger_set_id == ls_id, Period.status == "OPEN")
    ).all()
    findings = []
    for p in open_periods:
        if (p.year, p.month) >= (yr, mo):
            continue
        if _is_closed(s, ls_id, p.year, p.month):
            continue
        findings.append({
            "code": "UNCLOSED_HISTORY",
            "severity": "warn",
            "title": f"历史期间 {p.year}-{p.month:02d} 仍未结账",
            "detail": "存在早于当前期间的 OPEN 期间且未做期末结转，影响报表连续性与税务申报。",
            "suggestion": "确认该期间数据无误后，由 Boss 显式执行月结（close_period）。",
        })
    return findings


def _no_period(yr: int = 0, mo: int = 0) -> dict:
    label = f"{yr}-{mo:02d} " if yr and mo else ""
    return {
        "findings": [{
            "code": "NO_PERIOD",
            "severity": "warn",
            "title": f"{label}无可用会计期间",
            "detail": "账套尚未建立对应的会计期间，无法扫描。",
            "suggestion": "先初始化期间（首次建账）再开始记账。",
        }],
        "summary": f"{label}无可用会计期间，请先建账。",
        "severity_counts": {"alert": 0, "warn": 1, "info": 0},
        "period_status": "none",
    }


def _summary(findings: list[dict], yr: int, mo: int) -> str:
    alerts = [f for f in findings if f["severity"] == "alert"]
    warns = [f for f in findings if f["severity"] == "warn"]
    if alerts:
        return f"⚠️ {yr}-{mo:02d} 风险预警：{alerts[0]['title']}"
    if warns:
        return f"🔔 {yr}-{mo:02d} 风险提示：{warns[0]['title']}"
    return f"✅ {yr}-{mo:02d} 未发现明显财务风险"
