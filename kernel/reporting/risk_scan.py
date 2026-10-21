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
    R7  VAT_BURDEN_RATE     税负率偏低（warn，季销售额超免征额却少提增值税）
    R8  INVOICE_VAT_MISSING 发票异常·应税收入零增值税计提（warn）
    R9  INPUT_OUTPUT_DIVERGENCE 进销项背离·进项税/多缴挂账（warn，应交税费借方余额）

R7–R9 为 v2.1 P-10 深化，属「增值税风险」一类。本系统无独立发票子表
（发票以凭证形式存在），故三规则以**账簿勾稽**方式代理（ledger-proxied）：
    - 进销项比对 → 销项（6001/6051 应计增值税）与进项（2221 借方=待抵扣）勾稽；
    - 税负率   → 实账面增值税 / 季度应税销售额 vs 法定征收率 1%；
    - 发票异常 → 应税收入却零增值税计提（疑似不开票/私户收款）。
严格守「宁可漏报不误报」：季销售额 ≤ 30 万免征额（合规小规模）一律不报；
无 2221/销项数据则跳过。

每条 finding：{code, severity, title, detail, suggestion}，severity ∈ alert|warn|info。
detail 内含可证伪的具体数字（销售额/账面增值税/税负率/阈值），即证据链。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import Account, Period, Voucher, VoucherLine
from kernel.reporting.statements import (
    ReportError,
    amounts_by_code,
    balance_sheet,
    ending_balance,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")

# 现金类科目（货币资金）：期末余额不允许为负
CASH_CODES = ("1001", "1002")
# 应收类（常态借方）与应付类（常态贷方）科目前缀
AR_PREFIXES = ("1122", "1123", "1221")  # 应收票据及账款 / 预付账款 / 其他应收
AP_PREFIXES = ("2202", "2203", "2241")  # 应付账款 / 预收账款 / 其他应付
# 异常大额阈值：期间 POSTED 凭证行金额中位数 × 倍数，且绝对值不低于 floor
LARGE_MULTIPLE = Decimal("10")
LARGE_FLOOR = Decimal("100000")

# ── 增值税风险口径（P-10 / R7–R9；与 tax_vat_small 同政策，但本模块不反向依赖
#    以避免循环导入；所有数字来自内核既有只读取数，守 ADR-002）──
VAT_CODE = "2221"                       # 应交税费（小规模：6001/6051 不含税入账，税额单列 2221）
SALES_CODES = ("6001", "6051")          # 主营业务收入 / 其他业务收入（净额口径）
EXEMPT_QUARTER_THRESHOLD = Decimal("300000")   # 小规模季销售额 ≤ 30 万免征
VAT_LEVY_RATE = Decimal("0.01")         # 征收率 1%（3% 减按 1%）
VAT_BURDEN_TOLERANCE = Decimal("0.5")   # 账面增值税 < 应计 50% → 税负率偏低
INPUT_VAT_FLOOR = Decimal("100")        # 进项/多缴挂账需达此额才告警（去噪）


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


def _quarter_window(yr: int, mo: int) -> tuple[list[tuple[int, int]], int]:
    """由任意月份推出所属季度 [起月, 止月] 与季度号。"""
    q = (mo - 1) // 3 + 1
    q_end = q * 3
    q_start = q_end - 2
    months = [(yr, m) for m in range(q_start, q_end + 1)]
    return months, q


def _quarter_sales(s, ls_id: str, yr: int, mo: int) -> Decimal:
    """季度应税销售额 = 各月 6001/6051 本期净额（cr-dr）之和；缺月份按 0。

    复用 amounts_by_code 单一口径（与 tax_vat_small 同），不复制取数逻辑。
    """
    total = ZERO
    months, _ = _quarter_window(yr, mo)
    for y, m in months:
        try:
            amts = amounts_by_code(s, ls_id, y, m)
        except ReportError:
            continue  # 该月无期间/无数据 → 视为 0，不臆测
        for code in SALES_CODES:
            dr, cr = amts.get(code, (ZERO, ZERO))
            total += ending_balance(code, dr, cr)  # 收入净额 cr - dr
    return total


def _vat_rules(s, ls_id: str, yr: int, mo: int, amounts: dict) -> list[dict]:
    """增值税风险（P-10 / R7–R9）：税负率 / 发票异常 / 进销项背离。

    纯只读、ledger-proxied、缺数据跳过。返回 list[dict] findings。
    `amounts` 为当前期间 amounts_by_code 结果（含 2221 的 dr/cr）。
    """
    findings: list[dict[str, Any]] = []
    q_sales = _quarter_sales(s, ls_id, yr, mo)
    dr2221, cr2221 = amounts.get(VAT_CODE, (ZERO, ZERO))
    # 应交税费期末余额：贷方正=应缴/销项计提；借方负=进项税/多缴挂账
    vat_end = ending_balance(VAT_CODE, dr2221, cr2221)

    # R9 进销项背离：应交税费出现借方余额（进项税/多缴长期挂账）
    if vat_end < 0 and abs(vat_end) >= INPUT_VAT_FLOOR:
        findings.append({
            "code": "INPUT_OUTPUT_DIVERGENCE",
            "severity": "warn",
            "title": f"应交税费(2221)出现借方余额 {abs(vat_end):,.2f}（进项税/多缴挂账）",
            "detail": (
                f"应交税费出现借方余额 {abs(vat_end):,.2f}，疑似待抵扣进项税额长期挂账、"
                f"或预缴税款未及时冲抵，造成进销项勾稽失衡（进>销）。"
            ),
            "suggestion": (
                "核对该科目明细账与进销项台账，确认进项税是否已认证抵扣、"
                "是否存在不得抵扣需转出情形。"
            ),
        })

    # 税负率 / 发票异常：仅当季度销售额超免征额，避免误报合规免征的小规模
    if q_sales > EXEMPT_QUARTER_THRESHOLD:
        expected = (q_sales * VAT_LEVY_RATE).quantize(CENT)
        if vat_end <= 0:
            # R8 发票异常：应税收入却零增值税计提
            findings.append({
                "code": "INVOICE_VAT_MISSING",
                "severity": "warn",
                "title": f"发票异常：季度应税销售额 {q_sales:,.2f} 却零增值税计提",
                "detail": (
                    f"季度应税销售额 {q_sales:,.2f} 已超 30 万免征额，但账面增值税(2221)为 "
                    f"{vat_end:,.2f}，收入未见对应增值税计提，疑似不开票收入、私户收款未入账或漏提增值税。"
                ),
                "suggestion": (
                    "核对收入合同、开票系统与银行流水，确认应税收入是否均已如实计提增值税。"
                ),
            })
        elif vat_end < expected * VAT_BURDEN_TOLERANCE:
            # R7 税负率偏低：已计提但显著低于法定征收率
            burden = (vat_end / q_sales).quantize(Decimal("0.0001"))
            findings.append({
                "code": "VAT_BURDEN_RATE",
                "severity": "warn",
                "title": f"税负率偏低：增值税计提 {vat_end:,.2f}（≈{burden * 100:.2f}%）",
                "detail": (
                    f"季度应税销售额 {q_sales:,.2f}，按征收率 1% 应计提约 {expected:,.2f}，"
                    f"账面仅 {vat_end:,.2f}（税负率 ≈{burden * 100:.2f}%），显著低于法定征收率，"
                    f"疑似漏提增值税或进销不匹配。"
                ),
                "suggestion": (
                    "复核销项税额计提是否完整，比对开票金额与账面收入，"
                    "确认是否存在应计未计增值税。"
                ),
            })
    return findings


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
    # R7/R8/R9 增值税风险（进销项·税负率·发票异常，ledger-proxied）
    findings.extend(_vat_rules(s, ls_id, yr, mo, amounts))

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
