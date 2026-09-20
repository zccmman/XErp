"""审计追踪（v2.1 / B2）：把不可篡改事件账本变成人类可读、可证明的审计追踪报告。

守 ADR-002——单一真源，不重复实现：

- 完整性证明直接复用 :func:`kernel.ledger.chain.verify_chain`（同一套
  ``compute_event_hash`` 算法，本模块绝不自行重算哈希，否则会与内核口径漂移、
  把"真完整"误报成"被篡改"）；
- 中文事件名直接复用 :data:`kernel.events.DESCRIPTIONS`（事件目录单一真源，
  不另写一份映射）。

本模块**只读生成报告**，绝不修改事件链、绝不制单/过账/结账/替 Boss 做任何终态
动作——审计追踪本身也必须"可审计"。它服务 XErp 的护城河卖点：
"金额 deterministic 可审计、账本不可篡改"。
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from kernel.db.models import Event
from kernel.events import DESCRIPTIONS, E
from kernel.ledger.chain import verify_chain


# 审计意义最强的 payload 键，按优先级抽取时间线摘要
_SUMMARY_KEYS = ("voucher_no", "voucher_id", "period", "amount", "reason",
                 "invoice_no", "source", "breaker", "decision")


def _event_label(event_type: str) -> str:
    """事件类型 → 中文名；未知类型原样返回（不臆测）。"""
    try:
        return DESCRIPTIONS.get(E(event_type), event_type)
    except ValueError:
        return event_type


def _actor_name(actor: dict | None) -> str:
    if not actor:
        return "系统"
    return str(actor.get("display_name") or actor.get("name")
               or actor.get("id") or "未知")


def _payload_summary(payload: dict | None) -> str:
    if not payload:
        return ""
    for key in _SUMMARY_KEYS:
        val = payload.get(key)
        if val not in (None, ""):
            return f"{key}={val}"
    items = list(payload.items())[:1]
    return f"{items[0][0]}={items[0][1]}" if items else ""


def _in_period(occurred_at, year: int, month: int) -> bool:
    """按 occurred_at 的年月过滤；year=0 表示不限期间。"""
    if year == 0 or occurred_at is None:
        return year == 0
    if occurred_at.year != year:
        return False
    return month == 0 or occurred_at.month == month


def audit_timeline(session, ledger_set_id: str,
                   period_year: int = 0, period_month: int = 0,
                   limit: int = 200) -> list[dict]:
    """账套事件时间线（按 id 倒序，即最新在前），可选按 occurred_at 年月过滤。

    每条 = {event_id, event_type, label(中文), actor(执行人),
    occurred_at, aggregate_id, summary(payload 摘要)}。
    """
    events = session.scalars(
        select(Event)
        .where(Event.ledger_set_id == ledger_set_id)
        .order_by(Event.id.desc())
    ).all()
    rows: list[dict] = []
    for ev in events:
        if not _in_period(ev.occurred_at, period_year, period_month):
            continue
        rows.append({
            "event_id": ev.id,
            "event_type": ev.event_type,
            "label": _event_label(ev.event_type),
            "actor": _actor_name(ev.actor),
            "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            "aggregate_id": ev.aggregate_id,
            "summary": _payload_summary(ev.payload),
        })
        if len(rows) >= limit:
            break
    return rows


def build_audit_report(session, ledger_set_id: str,
                       period_year: int = 0, period_month: int = 0) -> dict:
    """整合审计追踪报告：完整性证明 + 类型分布 + 时间线 + 可读摘要。

    返回 {ledger_set_id, tamper_proof, chain_problem, integrity_severity,
    total_events, by_type(中文→{code,count}), period_scope, timeline, summary}。
    """
    ok, problem = verify_chain(session, ledger_set_id)

    events = session.scalars(
        select(Event).where(Event.ledger_set_id == ledger_set_id)
    ).all()
    total = len(events)
    by_type = Counter(ev.event_type for ev in events)
    by_type_readable = {
        _event_label(t): {"code": t, "count": c}
        for t, c in by_type.most_common()
    }

    timeline = audit_timeline(session, ledger_set_id, period_year, period_month)

    if not ok:
        detail = (problem or {}).get("detail", "审计链校验失败")
        eid = (problem or {}).get("event_id")
        summary = f"⚠️ 审计链完整性校验失败：{detail}"
        if eid is not None:
            summary += f"（断点 event_id={eid}）"
        severity = "alert"
    elif total == 0:
        summary = "ℹ️ 该账套暂无审计事件"
        severity = "info"
    else:
        summary = (f"✅ 审计链完整可信（共 {total} 条事件，"
                   f"密码学校验通过，无篡改痕迹）")
        severity = "ok"

    return {
        "ledger_set_id": ledger_set_id,
        "tamper_proof": ok,
        "chain_problem": problem,
        "integrity_severity": severity,
        "total_events": total,
        "by_type": by_type_readable,
        "period_scope": {"period_year": period_year, "period_month": period_month},
        "timeline": timeline,
        "summary": summary,
    }
