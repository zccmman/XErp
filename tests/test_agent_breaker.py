"""D4 TDD：断路器状态从 ``__breaker__`` 伪账套迁移到 ``agent_breakers`` 状态表。

断言：
- 状态以 agent_breakers 表为单一真源（直接改表即改状态）；
- trip/release 审计事件 ledger_set_id=='*'（全局），不再伪装成账套；
- 无任何 ledger_set_id=='__breaker__' 的事件残留；
- 重复 trip 状态表幂等（后者覆盖 reasons）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.anomaly import breaker_is_open, release_breaker, trip_breaker
from kernel.db.base import Base
from kernel.db.models import AgentBreaker, Event, Subject


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Subject(type="agent", display_name="bot", autonomy_level=2))
        s.add(Subject(type="user", display_name="admin", autonomy_level=3))
        s.commit()
        yield s


def _actor() -> dict:
    return {"type": "user", "id": "admin", "display_name": "admin"}


def test_trip_marks_state_open(session):
    trip_breaker(session, subject_id="bot", reasons=["large_amount: x"],
                 actor=_actor())
    session.commit()
    state = breaker_is_open(session, "bot")
    assert state is not None
    assert any("large_amount" in r for r in state["reasons"])
    row = session.get(AgentBreaker, "bot")
    assert row.is_open is True
    assert row.released_at is None


def test_release_clears_state(session):
    trip_breaker(session, subject_id="bot", reasons=["x"], actor=_actor())
    session.commit()
    release_breaker(session, subject_id="bot", actor=_actor(), note="误报")
    session.commit()
    assert breaker_is_open(session, "bot") is None
    row = session.get(AgentBreaker, "bot")
    assert row.is_open is False
    assert row.released_by == "admin"


def test_state_table_is_single_source(session):
    """直接改状态表即改状态——证明读的是状态表而非事件流。"""
    trip_breaker(session, subject_id="bot", reasons=["x"], actor=_actor())
    session.commit()
    assert breaker_is_open(session, "bot") is not None
    row = session.get(AgentBreaker, "bot")
    row.is_open = False  # 人为改状态表
    session.commit()
    assert breaker_is_open(session, "bot") is None


def test_audit_event_is_global_not_breaker_sentinel(session):
    trip_breaker(session, subject_id="bot", reasons=["x"], actor=_actor())
    release_breaker(session, subject_id="bot", actor=_actor(), note="n")
    session.commit()
    evs = session.scalars(select(Event)).all()
    breaker_evs = [e for e in evs
                   if e.event_type in ("BREAKER_TRIPPED", "BREAKER_RELEASED")]
    assert breaker_evs, "应至少有一条断路器审计事件"
    assert all(e.ledger_set_id == "*" for e in breaker_evs)
    assert not any(e.ledger_set_id == "__breaker__" for e in evs)
    types = [e.event_type for e in breaker_evs]
    assert types.count("BREAKER_TRIPPED") == 1
    assert types.count("BREAKER_RELEASED") == 1


def test_repeated_trip_is_idempotent_on_state(session):
    trip_breaker(session, subject_id="bot", reasons=["a"], actor=_actor())
    trip_breaker(session, subject_id="bot", reasons=["b", "c"], actor=_actor())
    session.commit()
    state = breaker_is_open(session, "bot")
    assert state is not None
    assert set(state["reasons"]) == {"b", "c"}  # 后者覆盖
    assert len(session.scalars(select(AgentBreaker)).all()) == 1
