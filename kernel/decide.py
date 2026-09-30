"""JEV-Decide：XErp 确定性决策引擎（判断 · 打分 · 选择）。

JEV = Just Enough Verification / Just-Events Vault（XErp 100% 确定性内核）。
本模块把 JEV 从「二值规则校验」升级为「判断 · 打分 · 选择」引擎，接管财务流程中
高频、边界清晰、风险中低的小决策，把 LLM 从「分类/评分」这类贱活里解放出来，
把规则引擎从「二值脆断」升级为「连续可解释评分」。

设计铁律（继承 XErp 内核约束，详见 XErp-JEV决策引擎深化方案.md）：
- 100% 确定性：绝不调用 LLM、绝不触碰网络（内核零外部依赖）。
- 只读账本单一真源（amounts_by_code / balance_sheet / arap.open_items /
  anomaly.rule_scan / budget.budget_vs_actual）；不写凭证、不进余额投影、不改任何状态。
- 歧义或硬约束不满足 → 标记 human_review_required，绝不擅自裁决。
- 内核零 WorkBuddy / 零适配器依赖（kernel/** 无 workbuddy import）。
- 决策只产出「确定性决策草稿」；终态（过账/支付/改账）仍由人类 HITL 点头。

三原语（JEV-Decide）：
- score：连续打分 → 分级（绿/黄/红 severity band）。
- classify：多类判定，top-1 选中；top1-top2 < ε 视为歧义 → 升人工。
- select：策略化选择，按优先序取首个满足硬约束者；无候选满足 → 升人工。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from kernel.db.models import Account, LedgerSet, Period, Subject, Voucher, utcnow

ZERO = Decimal("0")
_BIG = Decimal("100000000")


class Severity(str, Enum):
    """决策严重度（连续评分映射到的语义档位）。"""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEV_RANK[self]


_SEV_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class DecideError(ValueError):
    """JEV 决策错误：code / message_zh / details 可直接被 MCP 层消费。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _jsonify(o: Any) -> Any:
    """把决策结果里残留的 Decimal/date/datetime 转成 JSON 安全值。"""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, dict):
        return {k: _jsonify(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonify(v) for v in o]
    return o


@dataclass
class Decision:
    """类型化决策输出：判断 / 打分 / 选择 的统一载体，可解释、可审计。"""

    kind: str  # classify | score | select
    label: str  # 人类可读结论
    value: Any = None  # 分类=类别名；打分=数值；选择=选项键
    confidence: Decimal = Decimal("1")  # 0–1 校准置信度（确定性规则命中=1）
    severity: Severity = Severity.INFO
    human_review_required: bool = False
    basis: list[str] = field(default_factory=list)  # 可解释依据（命中规则/阈值）
    evidence: dict[str, Any] = field(default_factory=dict)  # 引用的真源数值

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "value": self.value,
            "confidence": str(self.confidence),
            "severity": self.severity.value,
            "human_review_required": self.human_review_required,
            "basis": list(self.basis),
            "evidence": _jsonify(self.evidence),
        }

    def __str__(self) -> str:
        flag = " [需人工复核]" if self.human_review_required else ""
        return (
            f"<JEV:{self.kind} {self.label}={self.value} "
            f"sev={self.severity.value}{flag}>"
        )


# ---------------------------------------------------------- 三原语


def score(
    *,
    value: Decimal,
    bands: list[tuple[Decimal, Severity, str]],
    label: str,
    evidence: dict | None = None,
    basis: list[str] | None = None,
) -> Decision:
    """连续打分 → 分级。

    bands 按阈值升序：每个元组 (max, severity, name) 表示 value <= max 落入该档；
    最后一项作兜底（max 取极大值）。返回 Decision(kind=score)。
    """
    chosen: tuple[Severity, str, Decimal] | None = None
    for mx, sev, name in bands:
        if value <= mx:
            chosen = (sev, name, mx)
            break
    if chosen is None:  # 超过所有显式档 → 用最后一档
        mx, sev, name = bands[-1]
        chosen = (sev, name, mx)
    return Decision(
        kind="score",
        label=label,
        value=str(value),
        severity=chosen[0],
        confidence=Decimal("1"),
        basis=(basis or []) + [f"落入档位：{chosen[1]}（阈值≤{chosen[2]}）"],
        evidence=evidence or {},
    )


def classify(
    *,
    choices: list[str],
    scores: dict[str, Decimal],
    label: str,
    epsilon: Decimal = Decimal("0.0001"),
) -> Decision:
    """多类判定。scores 给出每个候选的确定性得分（如命中强度）；top-1 选中。

    若 top1-top2 < epsilon → 歧义，标记 human_review_required（绝不强行裁决）。
    返回 Decision(kind=classify)。
    """
    ranked = sorted(choices, key=lambda c: scores.get(c, ZERO), reverse=True)
    top1 = ranked[0]
    top2 = ranked[1] if len(ranked) > 1 else None
    s1 = scores.get(top1, ZERO)
    s2 = scores.get(top2, ZERO) if top2 else ZERO
    ambiguous = (s1 - s2) < epsilon
    basis = [f"候选评分：{c}={scores.get(c, ZERO)}" for c in ranked]
    if ambiguous:
        basis.append("歧义：top1-top2 < ε，需人工/LLM 复核")
    return Decision(
        kind="classify",
        label=label,
        value=top1,
        confidence=Decimal("0") if ambiguous else Decimal("1"),
        severity=Severity.MEDIUM if ambiguous else Severity.LOW,
        human_review_required=ambiguous,
        basis=basis,
        evidence={"scores": {c: str(scores.get(c, ZERO)) for c in choices}},
    )


def select(
    *,
    options: list[str],
    ranked: list[str],
    label: str,
    hard_constraint_met: bool = True,
    constraint_note: str = "",
) -> Decision:
    """策略化选择。ranked 为按优先序排列的候选（index 小者优先）；

    取首个满足硬约束者；若无候选满足 → human_review_required。
    返回 Decision(kind=select)。
    """
    chosen = ranked[0] if ranked else None
    if chosen is None or not hard_constraint_met:
        return Decision(
            kind="select",
            label=label,
            value=None,
            severity=Severity.HIGH,
            human_review_required=True,
            basis=["无候选满足硬约束，需人工决策"]
            + ([constraint_note] if constraint_note else []),
            evidence={"options": options, "ranked": ranked},
        )
    return Decision(
        kind="select",
        label=label,
        value=chosen,
        severity=Severity.LOW,
        confidence=Decimal("1"),
        basis=[f"按优先序选中：{chosen}（优先序：{' > '.join(ranked)}）"],
        evidence={"options": options, "ranked": ranked},
    )


# ---------------------------------------------------------- 决策点实现（P0，单一真源）


def _off_hours(created_at: datetime) -> bool:
    """非常规时间判定（北京时间，固定 UTC+8，不引 tzdata 依赖）。

    与 anomaly._is_off_hours 同口径：夜间窗口 [22:00, 06:00) 或周末。
    此处自包含实现，避免 JEV 内核耦合 anomaly 私有函数。
    """
    local = created_at.replace(tzinfo=timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    )
    hour = local.hour
    if hour >= 22 or hour < 6:
        return True
    return local.weekday() >= 5


# ---- F11 预算差异分级（score）


def decide_budget_variance(
    session,
    *,
    ledger_set_id: str,
    fiscal_year: int,
    period_month: int,
    budget_id: str | None = None,
    bands: list[tuple[Decimal, Severity, str]] | None = None,
) -> Decision:
    """F11 预算差异预警分级（绿/黄/红）。

    实际数复用 budget.budget_vs_actual（amounts_by_code 单一真源）。
    bands 默认：偏差率≤5% 绿、≤15% 黄、其余红（按 |variance_pct| 绝对值分级）。
    """
    from kernel.budget import BudgetError, budget_vs_actual

    res = budget_vs_actual(
        session, ledger_set_id=ledger_set_id, fiscal_year=fiscal_year,
        period_month=period_month, budget_id=budget_id,
    )
    if not res["has_budget"]:
        return Decision(
            kind="score", label="预算差异分级", value="无预算",
            severity=Severity.INFO, human_review_required=False,
            basis=["该账套该期间无 ACTIVE 预算，无法分级"],
            evidence={"has_budget": False},
        )
    bands = bands or [
        (Decimal("5"), Severity.LOW, "绿(偏差≤5%)"),
        (Decimal("15"), Severity.MEDIUM, "黄(偏差5-15%)"),
        (_BIG, Severity.HIGH, "红(偏差>15%)"),
    ]
    row_grades: list[dict] = []
    worst = Severity.INFO
    for r in res["rows"]:
        pct = abs(Decimal(str(r["variance_pct"])))
        d = score(
            value=pct, bands=bands, label=f"{r['account_code']} 差异",
            evidence={
                "variance_pct": r["variance_pct"], "variance": r["variance"],
                "actual": r["actual"], "budget": r["budget"],
            },
        )
        row_grades.append({"account_code": r["account_code"], **d.to_dict()})
        if d.severity.rank > worst.rank:
            worst = d.severity
    return Decision(
        kind="score", label="预算差异分级（整体）",
        value=worst.value, severity=worst, confidence=Decimal("1"),
        basis=[f"共 {len(res['rows'])} 行，最差档位={worst.value}"],
        evidence={
            "total_budget": res["totals"]["budget"],
            "total_actual": res["totals"]["actual"],
            "total_variance": res["totals"]["variance"],
            "rows": row_grades,
        },
    )


# ---- F14 风险严重度评分（score）


def decide_risk_severity(
    session,
    *,
    ledger_set_id: str,
    voucher_id: str,
    thresholds: dict | None = None,
) -> Decision:
    """F14 凭证风险严重度评分（0–100 → 低/中/高）。

    复用 anomaly.rule_scan（确定性规则通道）的命中结果做加权评分：
    critical×40 + warn×10 + info×2，再映射到 severity band。
    """
    from kernel.anomaly import rule_scan
    from sqlalchemy import select as _select

    v = session.get(Voucher, voucher_id)
    if v is None:
        raise DecideError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    findings = rule_scan(session, v, thresholds=thresholds)
    return _severity_from_findings(findings, voucher_no=v.voucher_no)


def _severity_from_findings(findings: list, voucher_no: str | None = None) -> Decision:
    counts = {"info": 0, "warn": 0, "critical": 0}
    details: list[dict] = []
    for f in findings:
        s = f.severity if hasattr(f, "severity") else f.get("severity")
        counts[s] = counts.get(s, 0) + 1
        details.append({
            "rule": f.rule if hasattr(f, "rule") else f.get("rule"),
            "severity": s,
            "message": f.message_zh if hasattr(f, "message_zh") else f.get("message"),
        })
    raw = counts["critical"] * 40 + counts["warn"] * 10 + counts["info"] * 2
    bands = [
        (Decimal("0"), Severity.INFO, "无风险"),
        (Decimal("9"), Severity.LOW, "低风险"),
        (Decimal("39"), Severity.MEDIUM, "中风险"),
        (_BIG, Severity.HIGH, "高风险"),
    ]
    d = score(
        value=Decimal(raw), bands=bands, label="凭证风险严重度",
        evidence={"counts": counts, "raw_score": str(raw),
                  "voucher_no": voucher_no, "findings": details},
    )
    d.basis = [f"规则扫描命中 {len(findings)} 条"] + d.basis
    return d


# ---- F3 重复凭证标记（classify）


def decide_duplicate_voucher(
    session,
    *,
    ledger_set_id: str,
    voucher_id: str,
    window_days: int = 7,
) -> Decision:
    """F3 疑似重复凭证标记（同创建人 / 同金额 / 同摘要 / 时间窗内）。

    纯凭证明细扫描，确定性、零成本、可全量逐笔；命中即标「疑似重复」并升级人工复核。
    """
    from sqlalchemy import select as _select

    v = session.get(Voucher, voucher_id)
    if v is None:
        raise DecideError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    target_total = sum((Decimal(str(ln.debit)) for ln in v.lines), ZERO)
    target_summary = (v.summary or "").strip()
    others = session.scalars(
        _select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.id != v.id,
            Voucher.status == "POSTED",
            Voucher.created_by == v.created_by,
        )
    ).all()
    dups: list[str] = []
    for o in others:
        if (o.created_at.date() - v.created_at.date()).days > window_days:
            continue
        o_total = sum((Decimal(str(ln.debit)) for ln in o.lines), ZERO)
        if o_total != target_total:
            continue
        if target_summary and (o.summary or "").strip() != target_summary:
            continue
        dups.append(o.voucher_no)
    if dups:
        return Decision(
            kind="classify", label="重复凭证标记", value="疑似重复",
            severity=Severity.HIGH, confidence=Decimal("1"),
            basis=[
                f"与 {len(dups)} 张凭证金额/摘要/创建人/时间窗相似：{', '.join(dups)}"
            ],
            evidence={
                "target_total": str(target_total),
                "target_summary": target_summary,
                "duplicates": dups, "window_days": window_days,
            },
        )
    return Decision(
        kind="classify", label="重复凭证标记", value="唯一",
        severity=Severity.LOW, confidence=Decimal("1"),
        basis=["未发现同金额/摘要/创建人/时间窗的重复凭证"],
        evidence={"target_total": str(target_total)},
    )


# ---- F1 应付未清项健康度（score）


def decide_ap_open_health(
    session,
    *,
    ledger_set_id: str,
    partner: str | None = None,
    as_of_date: date | None = None,
    buckets: tuple[int, ...] = (30, 60, 90),
) -> Decision:
    """F1 应付未清项健康度分级（按账龄）。

    复用 arap.open_items（单据级未清项，单一真源）。>90 天红、61–90 天黄、否则绿。
    """
    from kernel.reporting.arap import open_items

    res = open_items(
        session, ledger_set_id=ledger_set_id, dim_key="supplier", partner=partner,
        as_of_date=as_of_date, bucket_days=buckets,
    )
    items = res["items"]
    if not items:
        return Decision(
            kind="score", label="应付未清项健康度", value="无未清项",
            severity=Severity.INFO,
            basis=["当前无应付未清项"],
            evidence={"totals": res["totals"]},
        )
    overdue = [i for i in items if i["days"] > 90]
    watch = [i for i in items if 60 < i["days"] <= 90]
    worst = Severity.HIGH if overdue else (Severity.MEDIUM if watch else Severity.LOW)
    return Decision(
        kind="score", label="应付未清项健康度",
        value=worst.value, severity=worst, confidence=Decimal("1"),
        basis=[
            f"未清 {len(items)} 笔；逾期(>90天) {len(overdue)}；"
            f"关注(61-90天) {len(watch)}"
        ],
        evidence={
            "totals": res["totals"],
            "overdue": [i["voucher_no"] for i in overdue],
            "watch": [i["voucher_no"] for i in watch],
        },
    )


# ---- F6 费用合规判定（classify）


def decide_expense_compliance(
    session,
    *,
    ledger_set_id: str,
    voucher_id: str,
    policy: dict | None = None,
) -> Decision:
    """F6 费用报销合规判定（合规 / 超标 / 非工作时段 / 重复摘要）。

    纯确定性规则：费用类借方行超单笔阈值 → 超标；创建于夜间/周末 → 非工作时段；
    同创建人同摘要近期重复 → 重复摘要。任一命中即标「不合规」并给依据。
    """
    from kernel.reporting import mapping as M
    from sqlalchemy import select as _select

    policy = policy or {}
    max_single = Decimal(str(policy.get("max_single", "5000.00")))
    v = session.get(Voucher, voucher_id)
    if v is None:
        raise DecideError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    accs = {
        a.id: a for a in session.scalars(
            _select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    mp = M.get_mapping("small_business")
    expense_prefixes: list[str] = []
    for _name, prefixes, side in mp["income_statement"]:
        if side == "debit":
            expense_prefixes.extend(prefixes)
    expense_prefixes = tuple(expense_prefixes)

    flags: list[tuple[str, str]] = []
    for ln in v.lines:
        acc = accs.get(ln.account_id)
        if acc and acc.code.startswith(expense_prefixes) and ln.debit > max_single:
            flags.append(
                ("超标", f"费用科目 {acc.code} 单行借方 {ln.debit} 超单笔阈值 {max_single}")
            )
    if _off_hours(v.created_at):
        flags.append(("非工作时段", "创建于夜间/周末（北京时间）"))
    if v.summary:
        others = session.scalars(
            _select(Voucher).where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.id != v.id,
                Voucher.status == "POSTED",
                Voucher.created_by == v.created_by,
                Voucher.summary == v.summary,
            )
        ).all()
        if others:
            flags.append(
                ("重复摘要", f"存在 {len(others)} 张相同摘要凭证，疑似重复报销")
            )
    if not flags:
        return Decision(
            kind="classify", label="费用合规判定", value="合规",
            severity=Severity.LOW, confidence=Decimal("1"),
            basis=["未发现超标/非工作时段/重复摘要"],
            evidence={"expense_prefixes": list(expense_prefixes)},
        )
    sev = (
        Severity.HIGH
        if any(f[0] in ("重复摘要", "超标") for f in flags)
        else Severity.MEDIUM
    )
    return Decision(
        kind="classify", label="费用合规判定", value="不合规",
        severity=sev, confidence=Decimal("1"),
        basis=[f"{name}：{msg}" for name, msg in flags],
        evidence={"flags": [f[0] for f in flags]},
    )


# ---- F7 费用审批路由（select，P1 提前落地以展示 select 原语）


def decide_approval_route(
    session,
    *,
    ledger_set_id: str,
    voucher_id: str,
) -> Decision:
    """F7 费用审批路由选择：按凭证总额阈值策略化路由审批层级。

    路由优先序：直属主管(≤5000) > 财务经理(≤50000) > 总经理(>50000)。
    硬约束恒满足（金额必有值），故永不升人工——纯确定性路由。
    """
    v = session.get(Voucher, voucher_id)
    if v is None:
        raise DecideError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    total = sum((Decimal(str(ln.debit)) for ln in v.lines), ZERO)
    routes = ["line_manager", "finance_manager", "general_manager"]
    chosen = "line_manager"
    if total > Decimal("50000"):
        chosen = "general_manager"
    elif total > Decimal("5000"):
        chosen = "finance_manager"
    # 把选中层级排到优先序首位，由 select 原语语义化地返回（策略化选择）。
    ranked = [chosen] + [r for r in routes if r != chosen]
    return select(
        options=routes, ranked=ranked, label="费用审批路由",
        hard_constraint_met=True,
        constraint_note=f"凭证总额 {total}，按金额阈值路由",
    )


# ---------------------------------------------------------- 决策注册表（供 MCP/Web 分发）


@dataclass
class DecisionSpec:
    name: str
    title: str
    description: str
    handler: Callable
    read_only: bool = True


DECISION_REGISTRY: dict[str, DecisionSpec] = {
    "budget_variance": DecisionSpec(
        "budget_variance", "预算差异分级",
        "按偏差率对预算 v.s. 实际做绿/黄/红分级（F11）", decide_budget_variance,
    ),
    "risk_severity": DecisionSpec(
        "risk_severity", "凭证风险严重度",
        "基于规则扫描命中做 0–100 严重度评分（F14）", decide_risk_severity,
    ),
    "duplicate_voucher": DecisionSpec(
        "duplicate_voucher", "重复凭证标记",
        "同创建人/金额/摘要/时间窗的疑似重复凭证（F3）", decide_duplicate_voucher,
    ),
    "ap_open_health": DecisionSpec(
        "ap_open_health", "应付未清项健康度",
        "按账龄对 AP 未清项做健康分级（F1）", decide_ap_open_health,
    ),
    "expense_compliance": DecisionSpec(
        "expense_compliance", "费用合规判定",
        "对费用凭证做 合规/超标/非工作时段/重复 分类（F6）", decide_expense_compliance,
    ),
    "approval_route": DecisionSpec(
        "approval_route", "费用审批路由",
        "按凭证总额阈值把审批路由到对应层级（F7）", decide_approval_route,
    ),
}


def list_decisions() -> list[dict]:
    """列出全部可分发决策类型（含标题/描述/是否只读）。"""
    return [
        {
            "name": s.name, "title": s.title, "description": s.description,
            "read_only": s.read_only,
        }
        for s in DECISION_REGISTRY.values()
    ]


def _dispatch_local(name: str, session, **params) -> Decision:
    """本地确定性分发（不含任何云端路由），供 run_decision 与云端适配器复用。"""
    spec = DECISION_REGISTRY.get(name)
    if spec is None:
        raise DecideError(
            "UNKNOWN_DECISION",
            f"未知决策类型 {name}；可用：{', '.join(DECISION_REGISTRY)}",
        )
    sig = inspect.signature(spec.handler)
    valid = {p for p in sig.parameters if p != "session"}
    clean = {k: v for k, v in params.items() if k in valid}
    return spec.handler(session, **clean)


# 云端后端运行时注入点：核心层不静态依赖 kernel.adapters，由适配器在导入时注册。
# CLOUD_BACKEND 签名为 (name, session, *, ledger_set_id, **params) -> Decision。
CLOUD_BACKEND = None


def set_cloud_backend(fn) -> None:
    """由 kernel/adapters 在导入时注册云端决策后端（如 TypeSafe Jev）。"""
    global CLOUD_BACKEND
    CLOUD_BACKEND = fn


def run_decision(name: str, session, **params) -> Decision:
    """按决策类型名分发执行（只读、确定性）。

    默认返回本地确定性决策（evidence.backend='local'）；若已注册云端后端且该账套
    已显式授权数据出境，则由云端后端返回其决策（evidence.backend='typesafe'）；
    云端异常一律安全回退本地（evidence.cloud_fallback=True）。核心层零适配器依赖。

    未知类型 → DecideError(UNKNOWN_DECISION)。handler 参数均为 keyword-only。
    多余参数（不同决策类型字段不同）按 handler 签名自动过滤，避免误传 TypeError。
    """
    local = _dispatch_local(name, session, **params)
    ls = params.get("ledger_set_id")
    if CLOUD_BACKEND is not None and ls:
        try:
            return CLOUD_BACKEND(name, session, ledger_set_id=ls, **params)
        except Exception:  # noqa: BLE001 —— 云端失败/未授权一律回退本地，不改本地判定
            local.evidence.setdefault("backend", "local")
            local.evidence["cloud_fallback"] = True
            return local
    local.evidence.setdefault("backend", "local")
    return local
