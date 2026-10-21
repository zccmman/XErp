"""审计轨迹可视化（v2.1 / P-11）渲染测试。

DoD：/audit 渲染出「审计轨迹」标题 + 完整性横幅（✅完整 / ⚠️被篡改 / ℹ️暂无）
+ 事件时间线 + 只读审计追踪合规声明；全程只读、复用内核单一真源（audit_trail
.build_audit_report，底层 verify_chain）；空账套安全降级为 ℹ️ 而不 500。
"""

import pytest
from datetime import datetime
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import LedgerSet, Period
from kernel.events import E
from kernel.ledger.chain import append_event
from kernel.seed import seed_demo_ledger

_ACTOR = {"type": "user", "display_name": "丞辰"}


@pytest.fixture(scope="module")
def env():
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/audit_view.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        ls_id = ids["ledger_set_id"]
        import_chart_of_accounts(s, ls_id, load_template_rows())
        # 追加几条真实审计事件，使审计页有时间线可渲染（P-11 可视化目标）
        # —— 用内核 append_event 生成合法事件链（单一真源，与 verify_chain 同源）
        append_event(s, ledger_set_id=ls_id, event_type=E.VOUCHER_CREATED.value,
                     aggregate_id="v1", payload={"voucher_no": "记-0001"},
                     actor=_ACTOR, occurred_at=datetime(2026, 9, 5))
        append_event(s, ledger_set_id=ls_id, event_type=E.VOUCHER_APPROVED.value,
                     aggregate_id="v1", payload={"voucher_no": "记-0001"},
                     actor=_ACTOR, occurred_at=datetime(2026, 9, 5))
        append_event(s, ledger_set_id=ls_id, event_type=E.VOUCHER_POSTED.value,
                     aggregate_id="v1", payload={"voucher_no": "记-0001"},
                     actor=_ACTOR, occurred_at=datetime(2026, 9, 6))
        append_event(s, ledger_set_id=ls_id, event_type=E.CLOSING_EXECUTED.value,
                     aggregate_id="p-202609", payload={"period": "2026-09"},
                     actor=_ACTOR, occurred_at=datetime(2026, 9, 30))
        s.commit()
        ids["reviewer_subject_id"] = None
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post(
        "/login",
        data={"subject_id": env["ids"]["subject_id"], "password": ""},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture(scope="module")
def ls_info(env):
    engine = create_engine(env["url"])
    with Session(engine) as s:
        ls = s.get(LedgerSet, env["ids"]["ledger_set_id"])
        return (ls.id, ls.accounting_standard)


def test_audit_page_renders(client, ls_info):
    ls_id, _ = ls_info
    r = client.get(f"/ledger/{ls_id}/audit")
    assert r.status_code == 200, r.text[:500]
    assert "审计轨迹" in r.text            # 页面标题
    assert "审计" in r.text                # 侧边栏入口
    assert "审计链" in r.text              # 完整性证明横幅
    assert "事件时间线" in r.text          # 时间线视图
    assert "只读审计追踪" in r.text        # 只读合规声明
    assert "可审计" in r.text              # 护城河卖点


def test_audit_page_shows_integrity_ok(client, ls_info):
    """seed 账套事件链完整 → 应出现「完整可信」绿横幅而非「失败」红横幅。"""
    ls_id, _ = ls_info
    r = client.get(f"/ledger/{ls_id}/audit")
    assert "完整可信" in r.text
    assert "完整性校验失败" not in r.text


def test_audit_empty_ledger_safe(env):
    """无事件空账套：/audit 不 500，安全降级为 ℹ️ 暂无审计事件。"""
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    engine = create_engine(env["url"])
    with Session(engine) as s:
        ls = LedgerSet(name="空账套审计", accounting_standard="small_business")
        s.add(ls)
        s.commit()
        ls_id = ls.id
        s.add(Period(ledger_set_id=ls_id, year=2026, month=9, status="OPEN"))
        s.commit()
    try:
        c = TestClient(build_app(env["url"]))
        c.post(
            "/login",
            data={"subject_id": env["ids"]["subject_id"], "password": ""},
            follow_redirects=False,
        )
        r = c.get(f"/ledger/{ls_id}/audit")
        assert r.status_code == 200, r.text[:500]
        assert "暂无审计事件" in r.text
    finally:
        with Session(engine) as s:
            s.query(Period).filter(Period.ledger_set_id == ls_id).delete()
            s.query(LedgerSet).filter(LedgerSet.id == ls_id).delete()
            s.commit()
