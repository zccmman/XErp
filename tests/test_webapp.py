"""P0-13 TDD：Web 最小界面（HTML 兜底）——工作区/凭证列表/余额表/建账向导。

DoD：浏览器可见凭证与余额，数值与 MCP 工具查询一致。
"""

import tempfile

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Subject, Voucher, VoucherLine
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/web.db"
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
    """已登录的客户端（以演示账套所有者身份）。

    认证接入后所有页面都要求会话，测试必须先登录。开放模式下口令留空即可。
    """
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


@pytest.fixture()
def anon_client(env):
    """未登录客户端，用于验证访问被拦截。"""
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    return TestClient(build_app(env["url"]))


def _post_voucher(env, no="记-7001"):
    """直接经内核造一张 POSTED 凭证，作为网页展示数据。"""
    from datetime import date
    from decimal import Decimal

    from kernel.posting import post_voucher
    from kernel.state import transition

    engine = create_engine(env["url"])
    with Session(engine) as s:
        ids = env["ids"]
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=no,
            voucher_date=date(2026, 8, 27),
            status="DRAFT",
            summary="网页展示用",
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("120.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("120.00")),
        ]
        s.add(v)
        s.flush()
        transition(
            s, voucher_id=v.id,
            actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED",
        )
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer_subject_id"]}, target="APPROVED")
        post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
        vid = v.id
    engine.dispose()
    return vid


def test_index_lists_workspace(client, env):
    r = client.get("/")
    assert r.status_code == 200 and "演示账套" in r.text


def test_dashboard_shows_voucher_and_balances(client, env):
    _post_voucher(env, "记-7001")
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}")
    assert r.status_code == 200
    assert "记-7001" in r.text and "POSTED" in r.text
    assert "120.00" in r.text  # 余额投影数值与内核一致


def test_voucher_detail_page(client, env):
    vid = _post_voucher(env, "记-7002")
    r = client.get(f"/voucher/{vid}")
    assert r.status_code == 200
    assert "6602" in r.text and "1001" in r.text and "POSTED" in r.text


def test_init_wizard_creates_ledger(client, env):
    r = client.post("/init", data={"name": "网页向导账套", "owner_name": "网主"},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    r2 = client.get("/")
    assert "网页向导账套" in r2.text


def test_opening_balance_import_via_web(client, env):
    client.post("/init", data={"name": "期初向导账套", "owner_name": "OW"},
                follow_redirects=False)
    # 拿到新账套 id（通过工作区页解析）
    home = client.get("/").text
    # 找到该账套的链接 id
    import re

    m = re.search(r"/ledger/([0-9a-f]{32})['\"][^>]*>期初向导账套", home)
    assert m, home[:500]
    ls = m.group(1)
    r2 = client.post(
        f"/ledger/{ls}/opening",
        data={"lines_text": "1002,200000,\n3001,,200000"},
        follow_redirects=False,
    )
    assert r2.status_code in (302, 303)
    dash = client.get(f"/ledger/{ls}").text
    assert "期初-0001" in dash and "200000.00" in dash


# ════════════════════════════════════════════════════════════
# 全新安装首启（空库不能死锁，否则客户拿到手就打不开）
# ════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def empty_env():
    """完全空的库 —— 模拟客户第一次启动，没有任何账套、任何身份。"""
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/empty.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return {"url": url}


@pytest.fixture()
def empty_client(empty_env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    return TestClient(build_app(empty_env["url"]))


def test_fresh_install_reaches_init_not_deadlocked(empty_client):
    """空库访问根路径 → 跳登录 → 登录页必须给出建账去路，而不是空下拉框。"""
    r = empty_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["location"]

    r2 = empty_client.get("/login")
    assert r2.status_code == 200
    assert "/init" in r2.text, "登录页必须给出建账入口，否则用户无从下手"
    assert "欢迎使用 XErp" in r2.text


def test_fresh_install_can_open_init_anonymously(empty_client):
    """全新安装时建账向导免登录 —— 否则「无身份→不能登录→不能建账」死锁。"""
    r = empty_client.get("/init", follow_redirects=False)
    assert r.status_code == 200, "空库必须能匿名打开建账向导"
    assert "首次使用" in r.text


def test_fresh_install_init_then_logged_in(empty_client, empty_env):
    """建账成功后自动以新建的所有者身份登录（建账即登录），并落到账套页。"""
    r = empty_client.post(
        "/init",
        data={"name": "客户第一套账", "owner_name": "张会计"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert "/ledger/" in r.headers["location"], r.headers["location"]

    from kernel import webauth

    assert webauth.COOKIE_NAME in r.cookies, "建账后应直接下发会话，无需再登录一次"

    # 随后可正常访问受保护页面，且右上角显示真实身份
    home = empty_client.get("/")
    assert home.status_code == 200
    assert "客户第一套账" in home.text
    assert "张会计" in home.text


def test_after_bootstrap_init_requires_login(empty_client, empty_env):
    """建账产生第一个身份后，/init 立刻回归受保护（防他人擅自再建账）。

    不依赖其他测试的执行顺序：先确保库中已有身份。
    """
    engine = create_engine(empty_env["url"])
    with Session(engine) as s:
        if s.scalars(select(Subject).limit(1)).first() is None:
            s.add(Subject(type="user", display_name="张会计", autonomy_level=3))
            s.commit()
    engine.dispose()

    r = empty_client.get("/init", follow_redirects=False)
    assert r.status_code in (302, 303), "已有身份后 /init 必须要求登录"
    assert "/login" in r.headers["location"]

    r2 = empty_client.get("/login")
    assert "张会计" in r2.text, "新建的所有者身份应出现在登录页下拉框中"


@pytest.fixture(scope="module")
def guarded_env():
    """空库 + 已设管理员口令。"""
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/guarded.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return {"url": url}


@pytest.fixture()
def guarded_client(guarded_env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    return TestClient(build_app(guarded_env["url"]))


def test_fresh_install_with_password_rejects_wrong(guarded_client, guarded_env, monkeypatch):
    """设了口令的全新安装：错误口令不能建账（防他人抢先占下管理员身份）。"""
    from urllib.parse import unquote

    monkeypatch.setenv("XERP_WEB_PASSWORD", "adm1n")
    import importlib

    from kernel import webauth

    importlib.reload(webauth)
    try:
        r = guarded_client.get("/init")
        assert "管理员口令" in r.text, "设了口令时建账表单必须要求输入口令"

        r2 = guarded_client.post(
            "/init",
            data={"name": "偷建账套", "owner_name": "陌生人", "password": "wrong"},
            follow_redirects=False,
        )
        loc = unquote(r2.headers.get("location", ""))
        assert "口令错误" in loc, loc

        from kernel.db.models import LedgerSet

        engine = create_engine(guarded_env["url"])
        with Session(engine) as s:
            assert s.scalars(select(LedgerSet)).all() == [], "口令错误不应建出账套"
        engine.dispose()

        # 正确口令可建账，并自动登录
        r3 = guarded_client.post(
            "/init",
            data={"name": "正主账套", "owner_name": "李主管", "password": "adm1n"},
            follow_redirects=False,
        )
        assert r3.status_code in (302, 303)
        assert webauth.COOKIE_NAME in r3.cookies
        assert "正主账套" in guarded_client.get("/").text
    finally:
        monkeypatch.delenv("XERP_WEB_PASSWORD")
        importlib.reload(webauth)


# ════════════════════════════════════════════════════════════
# 认证与真实 actor（P0-4 / P0-5 回归）
# ════════════════════════════════════════════════════════════

def test_anonymous_redirected_to_login(anon_client):
    """未登录访问任何页面都必须被拦截（P0-4）。"""
    for path in ("/", "/init", "/api/workspace"):
        r = anon_client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303), f"{path} 未被拦截"
        assert "/login" in r.headers.get("location", ""), path


def test_login_page_reachable_anonymously(anon_client):
    """登录页本身必须可匿名访问，否则会死循环重定向。"""
    r = anon_client.get("/login")
    assert r.status_code == 200
    assert "选择操作身份" in r.text


def test_login_page_lists_subjects(anon_client, env):
    """登录页列出可选身份，含演示账套所有者与审批人。"""
    r = anon_client.get("/login")
    assert env["ids"]["reviewer_subject_id"] in r.text


def test_wrong_password_rejected(anon_client, env, monkeypatch):
    """设置口令后，错误口令必须被拒。"""
    from urllib.parse import unquote

    monkeypatch.setenv("XERP_WEB_PASSWORD", "s3cret")
    import importlib

    from kernel import webauth

    importlib.reload(webauth)
    try:
        r = anon_client.post(
            "/login",
            data={"subject_id": env["ids"]["subject_id"], "password": "wrong"},
            follow_redirects=False,
        )
        loc = unquote(r.headers.get("location", ""))
        assert "口令错误" in loc, loc
    finally:
        monkeypatch.delenv("XERP_WEB_PASSWORD")
        importlib.reload(webauth)


def test_correct_password_accepted(anon_client, env, monkeypatch):
    """正确口令放行并下发会话 Cookie。"""
    monkeypatch.setenv("XERP_WEB_PASSWORD", "s3cret")
    import importlib

    from kernel import webauth

    importlib.reload(webauth)
    try:
        r = anon_client.post(
            "/login",
            data={"subject_id": env["ids"]["subject_id"], "password": "s3cret"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303)
        assert webauth.COOKIE_NAME in r.cookies, "登录后应下发会话 Cookie"
        r2 = anon_client.get("/", follow_redirects=False)
        assert r2.status_code == 200
    finally:
        monkeypatch.delenv("XERP_WEB_PASSWORD")
        importlib.reload(webauth)


def test_logout_clears_session(client):
    """退出后回到未认证态。"""
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    r2 = client.get("/", follow_redirects=False)
    assert "/login" in r2.headers.get("location", "")


def test_page_shows_current_identity(client, env):
    """页面右上角显示当前身份——「谁在操作」必须始终可见。"""
    r = client.get("/")
    assert "当前身份" in r.text


def test_actor_is_logged_in_subject_not_first_subject(client, env):
    """P0-5 核心：期初凭证的制单人必须是登录身份，而非数据库第一个主体。"""
    from kernel.db.models import Voucher

    # 用「非第一个」的身份登录（审批人是 fixture 里后加的）
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    reviewer = env["ids"]["reviewer_subject_id"]
    r = c.post("/login", data={"subject_id": reviewer, "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303)

    # 建一个新账套，再以期初导入检验 actor
    c.post("/init", data={"name": "actor 校验账套", "owner_name": "OW2"},
           follow_redirects=False)
    import re

    home = c.get("/").text
    m = re.search(r"/ledger/([0-9a-f]{32})['\"][^>]*>actor 校验账套", home)
    assert m, home[:500]
    ls = m.group(1)

    c.post(f"/ledger/{ls}/opening",
           data={"lines_text": "1002,5000,\n3001,,5000"}, follow_redirects=False)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalar(
            __import__("sqlalchemy").select(Voucher).where(
                Voucher.ledger_set_id == ls, Voucher.voucher_no.like("期初-%")
            )
        )
        assert v is not None
        assert v.created_by == reviewer, (
            f"actor 应为登录身份 {reviewer}，实为 {v.created_by}（疑似退回『第一个主体』）"
        )
    engine.dispose()
