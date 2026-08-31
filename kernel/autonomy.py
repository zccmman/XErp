"""L3 自治档（P3-03）：预算额度内自主过账 + 事后抽检 + 一键回放。

设计要点
--------
- **自治过账不是 Agent 自己审批自己**——状态机红线（AGENT_APPROVAL_FORBIDDEN）
  不可绕过。L3 的语义是「系统规则执行」：L3 主体 + 断路器闭合 + 单日额度内
  → 凭证直接以 POSTED 落账（与期初/结转同口径），审批链路上没有 Agent 审批；
- 每张自治凭证都打 ``autonomous: true`` 标记（事件 payload），**全部进入抽检池**；
- 抽检推翻 = **红字冲销**（生成全额反向凭证并 POSTED）——已过账凭证的账务
  正确撤销方式，绝不删历史；
- 一键回放 = 按凭证聚合事件流（含 agent.decision），审计轨迹一行不漏。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.anomaly import AnomalyError, check_breaker
from kernel.db.models import Account, Event, Subject, Voucher, VoucherLine, utcnow
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import PostingLine, _accumulate_balances

ZERO = Decimal("0.00")


class AutonomyError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _quota_used_today(session: Session, agent_id: str, today: date) -> Decimal:
    """当日自治过账金额合计（voucher.autonomous.posted 事件累计）。

    历史注记：第一版查询误写 agent. 前缀致额度恒 0，单测抓出。
    """
    used = ZERO
    for e in session.scalars(
        select(Event).where(Event.event_type == E.AUTONOMOUS_POSTED)
    ):
        if (e.payload or {}).get("subject_id") != agent_id:
            continue
        if e.occurred_at and e.occurred_at.date() != today:
            continue
        used += Decimal((e.payload or {}).get("total", "0"))
    return used


def autonomous_post(
    session: Session,
    *,
    ledger_set_id: str,
    voucher_date: date,
    actor_id: str,
    summary: str,
    lines: list[tuple[str, Decimal, Decimal]],   # (account_code, debit, credit)
) -> dict:
    """L3 自治过账：额度内 + 断路器闭合 → 直接 POSTED。

    任一前置不满足抛 AutonomyError（L3_REQUIRED / BREAKER_OPEN / QUOTA_EXCEEDED）。
    """
    subject = session.scalars(
        select(Subject).where(Subject.id == actor_id)
    ).first()
    if subject is None or subject.type != "agent" or (subject.autonomy_level or 0) < 3:
        raise AutonomyError(
            "L3_REQUIRED",
            "自治过账仅对 autonomy_level≥3 的 Agent 主体开放",
        )
    try:
        check_breaker(session, actor_id)
    except AnomalyError as e:
        raise AutonomyError("BREAKER_OPEN", e.message_zh) from e

    total = sum((d for _c, d, _cr in lines), ZERO)
    limit = subject.daily_voucher_limit
    used = _quota_used_today(session, actor_id, utcnow().date())
    if limit is not None and used + total > Decimal(str(limit)):
        raise AutonomyError(
            "QUOTA_EXCEEDED",
            f"今日自治额度不足：已用 {used:,.2f} + 本次 {total:,.2f} > 上限 "
            f"{Decimal(str(limit)):,.2f}（降低金额或等待人工审批通道）",
            {"used": str(used), "this": str(total), "limit": str(limit)},
        )

    codes = {code for code, _d, _c in lines}
    accounts = {
        a.code: a
        for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id,
                Account.code.in_(codes),
            )
        ).all()
    }
    missing = codes - accounts.keys()
    if missing:
        raise AutonomyError(
            "ACCOUNT_NOT_FOUND",
            f"账套缺少科目：{'、'.join(sorted(missing))}",
            {"missing": sorted(missing)},
        )
    non_leaf = sorted(c for c in codes if not accounts[c].is_leaf)
    if non_leaf:
        raise AutonomyError(
            "ACCOUNT_NOT_LEAF",
            f"自治过账只允许最明细科目：{'、'.join(non_leaf)}",
            {"non_leaf": non_leaf},
        )

    # 期间
    from kernel.db.models import Period

    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == voucher_date.year,
            Period.month == voucher_date.month,
        )
    ).first()
    if period is None:
        raise AutonomyError(
            "PERIOD_NOT_FOUND",
            f"账套不存在 {voucher_date.year}-{voucher_date.month:02d} 期间",
        )

    existing = session.scalars(
        select(Voucher.voucher_no).where(Voucher.ledger_set_id == ledger_set_id)
    ).all()
    seq = 0
    for no in existing:
        if no.startswith("记-") and no[2:].isdigit():
            seq = max(seq, int(no[2:]))
    voucher = Voucher(
        ledger_set_id=ledger_set_id,
        period_id=period.id,
        voucher_no=f"记-{seq + 1:04d}",
        voucher_date=voucher_date,
        status="POSTED",
        summary=summary,
        created_by=actor_id,
        posted_at=utcnow(),
        lines=[
            VoucherLine(
                line_no=i + 1,
                account_id=accounts[code].id,
                debit=debit, credit=credit,
            )
            for i, (code, debit, credit) in enumerate(lines)
        ],
    )
    session.add(voucher)
    session.flush()

    append_event(
        session, ledger_set_id=ledger_set_id,
        event_type=E.AUTONOMOUS_POSTED, aggregate_id=voucher.id,
        payload={
            "subject_id": actor_id,
            "total": str(total),
            "autonomous": True,
            "quota_used_today": str(used + total),
            "voucher_no": voucher.voucher_no,
        },
        actor={"type": "agent", "id": actor_id},
    )
    _accumulate_balances(
        session, voucher=voucher,
        lines=[
            PostingLine(account_id=ln.account_id, debit=ln.debit, credit=ln.credit)
            for ln in voucher.lines
        ],
    )
    session.flush()
    return {
        "voucher": {"id": voucher.id, "voucher_no": voucher.voucher_no,
                    "status": voucher.status, "summary": summary},
        "autonomous": True,
        "quota_used_today": f"{(used + total):,.2f}",
        "quota_limit": f"{Decimal(str(limit)):,.2f}" if limit is not None else None,
    }


def _is_autonomous(session: Session, voucher_id: str) -> Event | None:
    return session.scalars(
        select(Event).where(
            Event.event_type == E.AUTONOMOUS_POSTED,
            Event.aggregate_id == voucher_id,
        )
    ).first()


def audit_list(session: Session, *, ledger_set_id: str) -> dict:
    """抽检池：全部自治过账凭证 + 抽检状态（pending / passed / reversed）。"""
    pool: list[dict] = []
    for e in session.scalars(
        select(Event).where(
            Event.event_type == E.AUTONOMOUS_POSTED
        ).order_by(Event.id.desc())
    ):
        p = e.payload or {}
        vid = e.aggregate_id
        v = session.get(Voucher, vid)
        verdict = "pending"
        for re_ in session.scalars(
            select(Event).where(
                Event.event_type == E.AUTONOMOUS_REVIEWED,
                Event.aggregate_id == vid,
            )
        ):
            verdict = (re_.payload or {}).get("verdict", "pending")
        reversed_ = session.scalars(
            select(Event).where(
                Event.event_type == E.AUTONOMOUS_REVERSED,
                Event.aggregate_id == vid,
            )
        ).first() is not None
        if reversed_:
            verdict = "reversed"
        pool.append({
            "voucher_id": vid,
            "voucher_no": p.get("voucher_no") or (v.voucher_no if v else "?"),
            "total": p.get("total"),
            "subject_id": p.get("subject_id"),
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "audit_status": verdict,
        })
    return {"pool": pool,
            "pending": sum(1 for x in pool if x["audit_status"] == "pending")}


def audit_review(
    session: Session, *, voucher_id: str, verdict: str,
    reviewer_id: str, note: str = "",
) -> dict:
    """抽检裁决：pass（通过）/ reverse（推翻——红字冲销）。"""
    if verdict not in ("pass", "reverse"):
        raise AutonomyError("BAD_VERDICT", "verdict 必须是 pass 或 reverse")
    auto = _is_autonomous(session, voucher_id)
    if auto is None:
        raise AutonomyError("NOT_AUTONOMOUS", "该凭证不是自治过账凭证，无需抽检")
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise AutonomyError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")

    already = session.scalars(
        select(Event).where(
            Event.event_type == E.AUTONOMOUS_REVIEWED,
            Event.aggregate_id == voucher_id,
        )
    ).first()
    if already is not None:
        raise AutonomyError(
            "ALREADY_REVIEWED",
            f"该凭证已抽检（{(already.payload or {}).get('verdict')}），不可重复裁决",
        )

    result: dict[str, Any] = {"voucher_no": voucher.voucher_no,
                              "verdict": verdict}
    if verdict == "pass":
        append_event(
            session, ledger_set_id=voucher.ledger_set_id,
            event_type=E.AUTONOMOUS_REVIEWED, aggregate_id=voucher_id,
            payload={"verdict": "pass", "note": note,
                     "voucher_no": voucher.voucher_no},
            actor={"type": "user", "id": reviewer_id},
        )
    else:
        # 红字冲销：全额反向凭证，直接 POSTED（人工裁决 = 系统规则执行）
        {a.id: a for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == voucher.ledger_set_id)).all()}
        existing = session.scalars(
            select(Voucher.voucher_no).where(
                Voucher.ledger_set_id == voucher.ledger_set_id)
        ).all()
        seq = max((int(no[2:]) for no in existing
                   if no.startswith("记-") and no[2:].isdigit()), default=0)
        reversal = Voucher(
            ledger_set_id=voucher.ledger_set_id,
            period_id=voucher.period_id,
            voucher_no=f"记-{seq + 1:04d}",
            voucher_date=utcnow().date(),
            status="POSTED",
            summary=f"[抽检推翻] {voucher.voucher_no} {note}".strip(),
            created_by=reviewer_id,
            posted_at=utcnow(),
            lines=[
                VoucherLine(line_no=ln.line_no, account_id=ln.account_id,
                            debit=ln.credit, credit=ln.debit)
                for ln in voucher.lines
            ],
        )
        session.add(reversal)
        session.flush()
        append_event(
            session, ledger_set_id=voucher.ledger_set_id,
            event_type=E.AUTONOMOUS_REVIEWED, aggregate_id=voucher_id,
            payload={"verdict": "reverse", "note": note,
                     "reversal_voucher_no": reversal.voucher_no,
                     "voucher_no": voucher.voucher_no},
            actor={"type": "user", "id": reviewer_id},
        )
        append_event(
            session, ledger_set_id=voucher.ledger_set_id,
            event_type=E.AUTONOMOUS_REVERSED, aggregate_id=voucher_id,
            payload={"reversal_voucher_no": reversal.voucher_no,
                     "voucher_no": voucher.voucher_no},
            actor={"type": "user", "id": reviewer_id},
        )
        from kernel.posting import PostingLine

        _accumulate_balances(
            session, voucher=reversal,
            lines=[
                PostingLine(account_id=ln.account_id,
                            debit=ln.debit, credit=ln.credit)
                for ln in reversal.lines
            ],
        )
        result["reversal_voucher_no"] = reversal.voucher_no
    session.flush()
    return result


def replay(session: Session, *, voucher_id: str) -> dict:
    """一键回放：按凭证聚合全部事件（含 agent.decision），审计轨迹不漏一行。"""
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise AutonomyError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    timeline = []
    for e in session.scalars(
        select(Event).where(Event.aggregate_id == voucher_id).order_by(Event.id)
    ):
        timeline.append({
            "id": e.id,
            "event_type": e.event_type,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "actor": (e.actor or {}).get("id"),
            "payload_summary": {
                k: v for k, v in (e.payload or {}).items()
                if k in ("voucher_no", "total", "autonomous", "subject_id",
                         "verdict", "reversal_voucher_no", "prompt_sha256",
                         "model", "output_summary", "reasons")
            },
        })
    return {
        "voucher_no": voucher.voucher_no,
        "status": voucher.status,
        "summary": voucher.summary or "",
        "timeline": timeline,
        "event_count": len(timeline),
    }
