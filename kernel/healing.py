"""异常自愈建议（Phase E / E4）：基于 kernel/anomaly.py 的确定性规则扫描，生成 HITL 动作清单。

设计铁律（项目「HITL + 事件溯源 + 确定性」）：
- **只出「建议 / 草稿」，绝不自动修复**——终态动作必须人类确认（O11 红线：
  Agent 不能自解断路器；制单人 ≠ 审批人）。本模块是「诊断 + 建议」，不是「执行」。
- 复用 anomaly.rule_scan（确定性、只读、**不会跳闸**）与 breaker_is_open（读 ``agent_breakers``
  状态表）单一真源；不另建投影、不写账（ADR-002）。
- 每条建议含 ``suggested_action_zh`` + ``draft_payload``（人类确认后才可执行的草稿）
  + ``human_approval_required=True`` + ``auto_executable=False``。
- 严重项（critical / 断路器开）→ 经 ``operator.signal(ALERT)`` 联动算子（E5，复用跨进程桥）。
- 与 Copilot 衔接：``ask("异常怎么处理")`` 即路由到本模块。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.anomaly import breaker_is_open, rule_scan
from kernel.db.models import AgentBreaker, Subject, Voucher, utcnow

ZERO = Decimal("0.00")

# 规则 → 整改模板。severity 沿用 anomaly 判定（info/warn/critical），并映射动作文案。
_REMEDIATION: dict[str, dict[str, Any]] = {
    "large_amount": {
        "severity": "warn",
        "suggested_action_zh": (
            "复核大额凭证金额与业务附件；建议升级审批流（双人 / 主管复核）后再过账，"
            "确认非重复支付或误录。"
        ),
        "draft_payload": {
            "action": "escalate_approval",
            "target": "voucher",
            "note": "触发大额复核工作流，待人类审批确认",
        },
    },
    "off_hours": {
        "severity": "info",
        "suggested_action_zh": (
            "确认操作人身份与授权；非常规时间（夜间 / 周末，北京时间）创建需事后补登记，"
            "排除账号被盗用。"
        ),
        "draft_payload": {
            "action": "confirm_operator",
            "target": "voucher",
            "note": "人工确认操作人授权，非自动化动作",
        },
    },
    "rare_account": {
        "severity": "info",
        "suggested_action_zh": (
            "罕见科目出现，确认科目映射与核算合规；如误用请在审批前红字冲销后重制，"
            "避免挂错科目污染报表。"
        ),
        "draft_payload": {
            "action": "review_account_mapping",
            "target": "voucher_line",
            "note": "人工复核科目映射，可触发红字冲销草稿（需确认）",
        },
    },
    "freq_spike": {
        "severity": "warn",
        "suggested_action_zh": (
            "当日凭证频率激增，排查批量 / 重复 / 脚本录入；确认非误操作或欺诈，"
            "必要时临时冻结该主体自治。"
        ),
        "draft_payload": {
            "action": "review_batch",
            "target": "subject_day",
            "note": "人工排查当日批量，可触发断路器跳闸（需人类 / admin）",
        },
    },
    "llm_suspicious": {
        "severity": "warn",
        "suggested_action_zh": (
            "LLM 判定可疑，调取原始凭证与业务背景人工复核；必要时冻结该 Agent 自治，"
            "待风控确认后由 admin 解除。"
        ),
        "draft_payload": {
            "action": "human_review",
            "target": "voucher",
            "note": "人工风控复核，可触发断路器（O11：仅人类 / admin 解除）",
        },
    },
}


def _severity_rank(sev: str) -> int:
    return {"critical": 3, "warn": 2, "info": 1}.get(sev, 0)


def _map_finding(voucher_no: str, rule: str, severity: str, message_zh: str,
                 idx: int) -> dict[str, Any]:
    tpl = _REMEDIATION.get(rule, {
        "severity": severity,
        "suggested_action_zh": "未知规则，建议人工复核该凭证。",
        "draft_payload": {"action": "human_review", "target": "voucher"},
    })
    eff_sev = tpl["severity"] if _severity_rank(tpl["severity"]) >= _severity_rank(severity) else severity
    return {
        "action_id": f"H{idx:03d}",
        "source_voucher_no": voucher_no,
        "rule": rule,
        "severity": eff_sev,
        "finding_zh": message_zh,
        "suggested_action_zh": tpl["suggested_action_zh"],
        "draft_payload": tpl["draft_payload"],
        # 铁律：自愈只是「建议草稿」，绝不自动执行，终态须人类点头
        "auto_executable": False,
        "human_approval_required": True,
    }


def _breaker_review(session: Session) -> list[dict[str, Any]]:
    """汇总当前处于「开」状态的 Agent 断路器，给出人类复核 / 解除建议（只读）。"""
    rows = session.scalars(select(AgentBreaker).where(AgentBreaker.is_open.is_(True))).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        subj = session.get(Subject, r.subject_id)
        if subj is None or subj.type != "agent":
            continue  # 断路器只冻结 agent 主体；人类永不受其影响
        reasons = r.reasons or []
        out.append({
            "action_id": f"B-{r.subject_id[:8]}",
            "subject_id": r.subject_id,
            "tripped_at": r.tripped_at.isoformat() if r.tripped_at else None,
            "reasons": reasons,
            "severity": "critical",
            "suggested_action_zh": (
                "该 Agent 自治已被断路器冻结（" + "；".join(reasons)[:200] + "）。"
                "请风控 / admin 复核后通过 anomaly_release（人类鉴权）解除；"
                "Agent 主体不能自解（O11 红线）。"
            ),
            "draft_payload": {
                "action": "anomaly_release",
                "target": "agent_breaker",
                "note": "需 admin 人类鉴权，HITL 解除断路器",
            },
            "auto_executable": False,
            "human_approval_required": True,
        })
    return out


# ------------------------------------------------------------ 主入口


def healing_suggestions(
    session: Session,
    *,
    ledger_set_id: str,
    voucher_id: str | None = None,
    lookback_days: int = 30,
    thresholds: dict | None = None,
) -> dict[str, Any]:
    """异常自愈建议（只读、HITL 动作清单）。

    - ``voucher_id`` 给定 → 只扫描该凭证；否则扫描该账套近 ``lookback_days`` 天内创建的全部凭证。
    - 对每个 Finding 产出 HITL 整改建议；另汇总当前 open 的 Agent 断路器复核建议。
    - 严重项（critical / 断路器开）→ severity_rank 置顶，由调用方（Copilot）联动 ALERT。
    """
    if voucher_id is not None:
        vouchers = [session.get(Voucher, voucher_id)]
        vouchers = [v for v in vouchers if v is not None]
    else:
        since = utcnow() - timedelta(days=lookback_days)
        vouchers = list(session.scalars(
            select(Voucher).where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.created_at >= since,
            )
        ).all())

    suggestions: list[dict[str, Any]] = []
    findings_count = 0
    idx = 1
    for v in vouchers:
        findings = rule_scan(session, v, thresholds=thresholds)
        for f in findings:
            findings_count += 1
            suggestions.append(
                _map_finding(v.voucher_no, f.rule, f.severity, f.message_zh, idx)
            )
            idx += 1

    breaker = _breaker_review(session)
    suggestions.extend(breaker)

    worst = max(
        [_severity_rank(s["severity"]) for s in suggestions], default=0
    )
    severity_rank = {3: "critical", 2: "warn", 1: "info", 0: "normal"}[worst]

    return {
        "ledger_set_id": ledger_set_id,
        "scanned_vouchers": len(vouchers),
        "lookback_days": None if voucher_id else lookback_days,
        "findings_count": findings_count,
        "suggestions": suggestions,
        "breaker_open_count": len(breaker),
        "severity_rank": severity_rank,
        "tool_calls": [
            {"tool": "anomaly.rule_scan",
             "args": {"voucher_id": voucher_id, "lookback_days": lookback_days}}
        ]
        + ([{"tool": "anomaly.breaker_is_open", "args": {}}] if breaker else []),
        "basis": (
            "anomaly.rule_scan（确定性规则，只读不跳闸）+ breaker_is_open（读 agent_breakers 状态表）；"
            "ADR-002 单一真源、不改账；建议均需人类确认（O11 / HITL）"
        ),
    }


def signal_if_critical(result: dict[str, Any]) -> bool:
    """若 healing 结果为 critical（断路器开 / critical 建议），联动算子信号桥置 ALERT。

    纯增值、失败静默；推送 ≠ 执行，不改账。
    """
    if result.get("severity_rank") in ("critical", "warn"):
        try:
            from kernel.operator import OperatorState, signal as _op_signal

            _op_signal(OperatorState.ALERT, source="healing")
            return True
        except Exception:  # noqa: BLE001
            return False
    return False
