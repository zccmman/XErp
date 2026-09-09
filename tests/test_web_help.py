"""帮助中心 · Web 测试（产品化收尾：知识放离操作最近的地方）。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Subject


def _sid() -> str:
    import uuid

    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def env():
    import tempfile

    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webhelp.db"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        u = Subject(type="user", display_name="帮助页用户")
        s.add(u)
        s.commit()
        ids = {"subject_id": u.id}
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": env["ids"]["subject_id"],
                               "password": ""}, follow_redirects=False)
    assert r.status_code in (200, 303)
    return c


def test_help_page_renders_all_sections(client):
    t = client.get("/help").text
    assert t.count("<h3>") >= 6
    for sec in ("快速上手", "凭证的生命周期", "审批的三条通道",
                "月末结账", "三表预测", "常见问题"):
        assert sec in t, sec


def test_help_page_key_facts_accurate(client):
    """与内核语义一致的关键事实（驳回回草稿/不能自审/期初是存量）。"""
    t = client.get("/help").text
    assert "驳回必须写原因" in t
    assert "不能审自己的单" in t
    assert "期初是存量" in t
    assert "按此假设预测" in t


def test_userbar_links_to_help(client):
    """任意登录页 userbar 都有「帮助」入口。"""
    t = client.get("/").text
    assert 'href="/help"' in t


def test_help_page_no_auth_redirect(env):
    """帮助页不强制登录也可浏览（降低上手门槛）。"""
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.get("/help")
    assert r.status_code == 200
    assert "快速上手" in r.text
