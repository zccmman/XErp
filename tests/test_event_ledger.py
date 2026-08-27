"""P0-04 TDD：事件账本 — append-only 触发器 + sha256 成链 + verify_chain 篡改检测。

DoD（DEVPLAN）：篡改一条 → verify_chain 校验失败的测试通过。
分层：链逻辑/SQLite 触发器本轮全绿；PG 触发器等价物在迁移 0002 中，@postgres 层待 PG 环境。
"""

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from kernel.db.append_only import install_append_only_sqlite
from kernel.db.base import Base
from kernel.db.models import Event
from kernel.ledger import append_event, verify_chain

LS = "ledger_a"
ACTOR = {"type": "user", "id": "u1", "display_name": "丞辰"}


def _evt(i: int) -> dict:
    return {
        "ledger_set_id": LS,
        "event_type": "voucher.created",
        "aggregate_id": f"v-{i}",
        "payload": {"voucher_no": f"记-{i:04d}", "amount": "100.00"},
        "actor": ACTOR,
    }


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    install_append_only_sqlite(engine)
    with Session(engine) as s:
        yield s


def _seed_three(session):
    for i in (1, 2, 3):
        append_event(session, **_evt(i))
    session.flush()


def test_append_links_and_verifies(session):
    _seed_three(session)
    events = session.scalars(select(Event).order_by(Event.id)).all()
    assert len(events) == 3
    assert events[0].prev_hash == "0" * 64
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash
    ok, problem = verify_chain(session, LS)
    assert ok and problem is None


def test_trigger_blocks_update(session):
    _seed_three(session)
    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE events SET payload = payload WHERE id = 1")
        )
    session.rollback()


def test_trigger_blocks_delete(session):
    _seed_three(session)
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM events WHERE id = 1"))
    session.rollback()


def test_tamper_detected_by_verify(tmp_path):
    """绕过触发器模拟攻击者直改 DB：payload 被改 → hash 不匹配。"""
    engine = create_engine(f"sqlite:///{tmp_path}/tamper.db")
    Base.metadata.create_all(engine)  # 不装触发器 = 模拟超级权限直改
    with Session(engine) as s:
        _seed_three(s)
    with engine.begin() as conn:  # 第二个连接模拟外部篡改
        conn.execute(
            text("UPDATE events SET payload = '{\"amount\": \"999.00\"}' WHERE id = 2")
        )
    with Session(engine) as s:
        ok, problem = verify_chain(s, LS)
        assert not ok
        assert problem is not None and problem["event_id"]


def test_deletion_breaks_linkage(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/delete.db")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed_three(s)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM events WHERE id = 2"))
    with Session(engine) as s:
        ok, problem = verify_chain(s, LS)
        assert not ok and problem is not None


def test_chains_isolated_per_ledger_set(session):
    _seed_three(session)
    append_event(session, **{**_evt(1), "ledger_set_id": "ledger_b"})
    session.flush()
    ok_a, _ = verify_chain(session, LS)
    ok_b, _ = verify_chain(session, "ledger_b")
    assert ok_a and ok_b
