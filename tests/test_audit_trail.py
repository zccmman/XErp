"""审计追踪契约测试（v2.1 / B2）。

自包含 fixture（临时 sqlite + 内核 append_event 生成合法事件链），覆盖：
- 正常链 tamper_proof=True、类型分布含中文、时间线倒序、summary ✅；
- 篡改（改 hash）/ 断链（改 prev_hash）被 verify_chain 检出 → tamper_proof=False；
- 时间线按 occurred_at 年月过滤；
- 空账套 tamper_proof=True、total=0、summary ℹ️；
- 报告只读不改账（Event 行数不变、session 无脏写）；
- audit_trail 已接入 standard 档位。
"""

from __future__ import annotations

from datetime import datetime
from tempfile import mkdtemp

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Event
from kernel.events import E
from kernel.ledger.chain import append_event
from kernel.reporting.audit_trail import audit_timeline, build_audit_report

LS = "LS_AUDIT_TEST"
ACTOR = {"type": "user", "display_name": "丞辰"}


def _engine():
    eng = create_engine(f"sqlite:///{mkdtemp()}/audit.db")
    Base.metadata.create_all(eng)
    return eng


def _seed_chain(eng, events: list[tuple[str, str, dict, datetime]]):
    """用内核 append_event 生成合法事件链（同一事务内连续 append 后才 commit）。"""
    with Session(eng) as s:
        for etype, agg, payload, ts in events:
            append_event(
                s, ledger_set_id=LS, event_type=etype,
                aggregate_id=agg, payload=payload, actor=ACTOR, occurred_at=ts,
            )
        s.commit()


def _tamper(eng, event_id: int, **changes):
    """绕过 append-only 触发器（SQLite 无触发器）直接改库，模拟"如果有人改了链"。"""
    with Session(eng) as s:
        ev = s.get(Event, event_id)
        for k, v in changes.items():
            setattr(ev, k, v)
        s.commit()


def test_normal_chain_tamper_proof_true():
    eng = _engine()
    _seed_chain(eng, [
        (E.VOUCHER_CREATED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 5)),
        (E.VOUCHER_APPROVED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 5)),
        (E.VOUCHER_POSTED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 6)),
        (E.CLOSING_EXECUTED.value, "p-202609", {"period": "2026-09"}, datetime(2026, 9, 30)),
    ])
    with Session(eng) as s:
        r = build_audit_report(s, LS)
    assert r["tamper_proof"] is True
    assert r["integrity_severity"] == "ok"
    assert r["total_events"] == 4
    assert "✅" in r["summary"]
    # 类型分布含中文名
    labels = r["by_type"].keys()
    assert any("过账" in lbl for lbl in labels)
    assert any("结转" in lbl for lbl in labels)
    # 时间线倒序：最新（结转 9-30）在首位
    assert r["timeline"][0]["label"] == "期末损益结转执行"
    assert r["timeline"][0]["actor"] == "丞辰"
    assert r["timeline"][0]["summary"] == "period=2026-09"


def test_chain_tamper_detected_hash_mismatch():
    eng = _engine()
    _seed_chain(eng, [
        (E.VOUCHER_CREATED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 5)),
        (E.VOUCHER_POSTED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 6)),
    ])
    # 取第二条（id=2）篡改其哈希
    with Session(eng) as s:
        second = s.scalars(select(Event).order_by(Event.id).offset(1).limit(1)).first()
        target_id = second.id
    _tamper(eng, target_id, hash="0" * 64)
    with Session(eng) as s:
        r = build_audit_report(s, LS)
    assert r["tamper_proof"] is False
    assert r["integrity_severity"] == "alert"
    assert r["chain_problem"]["reason"] == "hash_mismatch"
    assert "⚠️" in r["summary"]


def test_chain_linkage_broken_detected():
    eng = _engine()
    _seed_chain(eng, [
        (E.VOUCHER_CREATED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 5)),
        (E.VOUCHER_POSTED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 6)),
    ])
    with Session(eng) as s:
        second = s.scalars(select(Event).order_by(Event.id).offset(1).limit(1)).first()
        target_id = second.id
    _tamper(eng, target_id, prev_hash="f" * 64)
    with Session(eng) as s:
        r = build_audit_report(s, LS)
    assert r["tamper_proof"] is False
    assert r["chain_problem"]["reason"] == "linkage_broken"


def test_timeline_period_filter():
    eng = _engine()
    _seed_chain(eng, [
        (E.VOUCHER_CREATED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 7, 10)),
        (E.VOUCHER_POSTED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 7, 12)),
        (E.VOUCHER_CREATED.value, "v2", {"voucher_no": "记-0002"}, datetime(2026, 9, 10)),
        (E.VOUCHER_POSTED.value, "v2", {"voucher_no": "记-0002"}, datetime(2026, 9, 12)),
    ])
    with Session(eng) as s:
        # 仅看 9 月
        tl = audit_timeline(s, LS, period_year=2026, period_month=9)
        full = audit_timeline(s, LS)
    assert len(full) == 4
    assert len(tl) == 2
    assert all(
        datetime.fromisoformat(x["occurred_at"]).month == 9 for x in tl
    )
    # 不限期间应含全部 4 条
    with Session(eng) as s:
        all_tl = audit_timeline(s, LS, period_year=0, period_month=0)
    assert len(all_tl) == 4


def test_empty_ledger_no_events():
    eng = _engine()
    with Session(eng) as s:
        r = build_audit_report(s, LS)
    assert r["tamper_proof"] is True
    assert r["total_events"] == 0
    assert r["integrity_severity"] == "info"
    assert "ℹ️" in r["summary"]
    assert r["timeline"] == []


def test_audit_report_readonly():
    eng = _engine()
    _seed_chain(eng, [
        (E.VOUCHER_CREATED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 5)),
        (E.VOUCHER_POSTED.value, "v1", {"voucher_no": "记-0001"}, datetime(2026, 9, 6)),
    ])
    before = None
    with Session(eng) as s:
        before = s.scalars(select(Event).where(Event.ledger_set_id == LS)).all().__len__()
        assert len(s.dirty) == 0  # 查询后应无脏对象
        r = build_audit_report(s, LS)
        # 报告生成过程中不得产生待写对象
        assert len(s.new) == 0 and len(s.dirty) == 0
    # 行数不变
    with Session(eng) as s:
        after = s.scalars(select(Event).where(Event.ledger_set_id == LS)).all().__len__()
    assert after == before == 2
    assert r["tamper_proof"] is True


def test_audit_trail_in_standard_profile():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
    from xerp_mcp import profiles  # noqa: E402

    assert "audit_trail" in profiles.STANDARD_EXTRA
    assert "audit_trail" in profiles.enabled_for("standard")
    assert "audit_trail" in profiles.enabled_for("pro")
    # 极简档不应暴露（审计追踪属增强能力，非黄金路径）
    assert "audit_trail" not in profiles.enabled_for("minimal")
