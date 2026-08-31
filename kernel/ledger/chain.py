"""事件账本 API（ADR-002）：append_event / verify_chain。

并发说明：P0 采用「事务内取尾哈希→插入」的简单策略，单进程/低并发下正确；
PG 上线的并发成链（advisory lock 或唯一 (ledger_set_id, prev_hash) 约束）在 P1 加固，
由 @postgres 分层测试覆盖。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Event, utcnow
from kernel.ledger.canonical import GENESIS, compute_event_hash


def append_event(
    session: Session,
    *,
    ledger_set_id: str,
    event_type: str,
    aggregate_id: str,
    payload: dict,
    actor: dict,
    occurred_at: datetime | None = None,
) -> Event:
    last = session.scalars(
        select(Event)
        .where(Event.ledger_set_id == ledger_set_id)
        .order_by(Event.id.desc())
        .limit(1)
    ).first()
    prev_hash = last.hash if last else GENESIS
    occurred_at = occurred_at or utcnow()
    evt = Event(
        ledger_set_id=ledger_set_id,
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload=payload,
        actor=actor,
        occurred_at=occurred_at,
    )
    evt.prev_hash = prev_hash
    evt.hash = compute_event_hash(
        prev_hash,
        ledger_set_id=ledger_set_id,
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload=payload,
        actor=actor,
        occurred_at=occurred_at,
    )
    session.add(evt)
    session.flush()
    return evt


def verify_chain(session: Session, ledger_set_id: str) -> tuple[bool, dict | None]:
    """校验账套内事件链。返回 (ok, problem)；problem 含 event_id 与 reason。"""
    events = session.scalars(
        select(Event)
        .where(Event.ledger_set_id == ledger_set_id)
        .order_by(Event.id)
    ).all()

    expected_prev = GENESIS
    for evt in events:
        if evt.prev_hash != expected_prev:
            return False, {
                "event_id": evt.id,
                "reason": "linkage_broken",
                "detail": f"prev_hash 不衔接（期望 {expected_prev[:12]}…）",
            }
        recomputed = compute_event_hash(
            evt.prev_hash,
            ledger_set_id=evt.ledger_set_id,
            event_type=evt.event_type,
            aggregate_id=evt.aggregate_id,
            payload=evt.payload,
            actor=evt.actor,
            occurred_at=evt.occurred_at,
        )
        if recomputed != evt.hash:
            return False, {
                "event_id": evt.id,
                "reason": "hash_mismatch",
                "detail": "内容被篡改：重算哈希与存链哈希不一致",
            }
        expected_prev = evt.hash
    return True, None
