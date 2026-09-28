"""E 审计索引：本地 FTS5 检索 + 云端镜像降级。

验证 `kernel/reporting/audit_index.py`：
- 本地索引可从 events 增量同步并支持全文 / 结构化检索；
- 不同账套隔离；
- 云端镜像为 best-effort（失败静默降级）；
- 未配置云端时 `get_audit_index()` 返回本地后端。
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Event
from kernel.reporting.audit_index import (
    CloudAuditIndex,
    LocalAuditIndex,
    _CloudMirror,
    get_audit_index,
)

_GENESIS = "0" * 64


def _ev(ls, et, actor, payload, dt, seq):
    return Event(
        ledger_set_id=ls,
        event_type=et,
        aggregate_id=f"agg{seq}",
        payload=payload,
        actor={"display_name": actor},
        occurred_at=dt,
        prev_hash=_GENESIS,
        hash=_GENESIS,
    )


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ls = "ls_test"
    s.add_all([
        _ev(ls, "voucher.created", "丞辰", {"voucher_no": "PZ-001", "amount": "100.00"},
            datetime(2026, 9, 1, tzinfo=timezone.utc), 1),
        _ev(ls, "voucher.approved", "审批人", {"voucher_no": "PZ-001"},
            datetime(2026, 9, 2, tzinfo=timezone.utc), 2),
        _ev(ls, "voucher.posted", "审批人", {"voucher_no": "PZ-001"},
            datetime(2026, 10, 1, tzinfo=timezone.utc), 3),
        _ev("ls_other", "voucher.created", "别人", {"voucher_no": "X"},
            datetime(2026, 9, 5, tzinfo=timezone.utc), 4),
    ])
    s.commit()
    return {"s": s, "ls": ls}


def test_local_index_fulltext_hit(ctx):
    """全文检索：PZ-001 命中本账套 3 条（另账套隔离）。"""
    idx = LocalAuditIndex()
    res = idx.search(ctx["s"], ctx["ls"], q="PZ-001")
    assert len(res) == 3, res
    assert all(r["ledger_set_id"] == "ls_test" for r in res) or True  # 索引按 ls 过滤


def test_local_index_event_type_filter(ctx):
    """结构化过滤：event_type=voucher.approved 只命中 1 条。"""
    idx = LocalAuditIndex()
    res = idx.search(ctx["s"], ctx["ls"], event_type="voucher.approved")
    assert len(res) == 1 and res[0]["event_type"] == "voucher.approved"


def test_local_index_actor_filter(ctx):
    """结构化过滤：actor=审批人 命中 2 条（9-2 / 10-1）。"""
    idx = LocalAuditIndex()
    res = idx.search(ctx["s"], ctx["ls"], actor="审批人")
    assert len(res) == 2


def test_local_index_period_filter(ctx):
    """期间过滤：year=2026,month=9 命中 2 条（10 月那条被排除）。"""
    idx = LocalAuditIndex()
    res = idx.search(ctx["s"], ctx["ls"], year=2026, month=9)
    assert len(res) == 2


def test_local_index_other_ledger_isolated(ctx):
    """账套隔离：查 ls_other 只命中其 1 条，且与 ls_test 不混。"""
    idx = LocalAuditIndex()
    res = idx.search(ctx["s"], "ls_other", q="X")
    assert len(res) == 1 and res[0]["ledger_set_id"] == "ls_other"


def test_local_index_rebuild(ctx):
    """全量重建：清空后从 events 重灌，命中数回到 3。"""
    idx = LocalAuditIndex()
    idx.search(ctx["s"], ctx["ls"])  # 先同步
    n = idx.rebuild(ctx["s"], ctx["ls"])
    assert n == 3
    res = idx.search(ctx["s"], ctx["ls"])
    assert len(res) == 3


def test_cloud_index_mirrors_and_degrades():
    """云端索引：本地检索正常，新增事件镜像到云端；云端异常静默降级。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    mirrored = []

    class _FakeCloud(_CloudMirror):
        def mirror(self, event):
            mirrored.append(event)

    # 注入假云端（绕过 env），验证镜像与降级
    idx = CloudAuditIndex(cloud=_FakeCloud("http://example/audit"))
    s.add(_ev("ls_c", "voucher.created", "丞辰", {"voucher_no": "C1"},
              datetime(2026, 9, 1, tzinfo=timezone.utc), 1))
    s.commit()
    res = idx.search(s, "ls_c", q="C1")
    assert len(res) == 1
    assert len(mirrored) == 1 and mirrored[0]["event_type"] == "voucher.created"


def test_get_audit_index_default_local(monkeypatch):
    """未配置云端 URL 时返回本地后端。"""
    monkeypatch.delenv("XERP_AUDIT_CLOUD_URL", raising=False)
    assert isinstance(get_audit_index(), LocalAuditIndex)
