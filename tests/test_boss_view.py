"""v1.3 老板经营看板 + 月度财报卡片 渲染测试。

DoD：/boss 渲染出 SVG 趋势/占比图与账本精灵提醒；/card 渲染出纯净可分享卡片；
_boss_data 聚合复用内核 statements，不复制配平逻辑。
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import LedgerSet, Period, Subject
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/boss.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()
        ids["reviewer_subject_id"] = reviewer.id
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


def test_boss_page_renders(client, ls_info):
    ls_id, _ = ls_info
    r = client.get(f"/ledger/{ls_id}/boss")
    assert r.status_code == 200, r.text[:500]
    assert "经营看板" in r.text
    assert "<svg" in r.text  # 趋势/占比图必出
    assert "账本精灵" in r.text  # O18 主动提醒区
    assert "资产 / 负债 / 权益" in r.text  # 趋势标题


def test_boss_data_aggregation(env, ls_info):
    from kernel.webapp import _boss_data

    engine = create_engine(env["url"])
    ls_id, std = ls_info
    with Session(engine) as s:
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ls_id)
        ).first()
        assert per is not None, "seed 账套应有期间"
        d = _boss_data(s, ls_id, per.year, per.month, std)
    assert isinstance(d["labels"], list) and d["labels"]
    assert isinstance(d["asset_t"], list) and len(d["asset_t"]) >= 1
    assert isinstance(d["asset_segs"], list)
    assert isinstance(d["tips"], list) and d["tips"]
    assert isinstance(d["rec"], dict) and "ok" in d["rec"]


def test_card_page_renders(client, ls_info):
    ls_id, _ = ls_info
    r = client.get(f"/ledger/{ls_id}/card")
    assert r.status_code == 200, r.text[:500]
    assert "财报卡片" in r.text
    assert "营业收入" in r.text
    assert "由 XErp 生成" in r.text
    assert "<svg" not in r.text  # 卡片纯净，无看板 SVG
