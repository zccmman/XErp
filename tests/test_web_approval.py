"""G1-51 Web 审批闭环 TDD。

此前 Web 端凭证只有「制单 + 推送」两步（G1-50），审批人既看不到待办队列，
也无法在网页上同意/驳回——审批仍要回到 MCP 对话或 IM 卡片，Web 端是断头路。
会计不用命令行、老板不进群聊卡片，审批闭环必须在 Web 端贯通。

本文件覆盖 /todo 待办列表 + 详情页操作区 + 四个跃迁路由（同意/驳回/撤回/过账），
重点验证与 MCP guarded 完全同语义的四个门禁：
  1. 制单人不能审批自己的凭证（NO_SELF_APPROVAL）
  2. Agent 不能审批/驳回（AGENT_APPROVAL_FORBIDDEN）
  3. 驳回必须填写原因（REJECT_REASON_REQUIRED）
  4. 非制单人不能撤回（NOT_VOUCHER_MAKER）
以及最容易被偷工减料的一条：**过账必须走 post_voucher 而非裸 transition**，
否则 balances 投影不更新，账账核对探针当场爆炸。
"""

import re
import tempfile
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Balance, Event, Period, Subject, Voucher
from kernel.events import E
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webapproval.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="王审批", autonomy_level=3)
        agent = Subject(type="agent", display_name="助理Agent", autonomy_level=3)
        s.add_all([reviewer, agent])
        s.commit()
        # agent 也授 reviewer 角色是有意的：为了验证 Agent 门禁来自内核状态机
        # （AGENT_APPROVAL_FORBIDDEN）而不是被 casbin 提前拦截成 FORBIDDEN。
        from kernel.authz import grant_ledger_role

        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=ids["subject_id"], role="accountant")
        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=reviewer.id, role="reviewer")
        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=agent.id, role="reviewer")
        s.commit()
        ids["reviewer_id"] = reviewer.id
        ids["agent_id"] = agent.id
        p = s.get(Period, ids["period_id"])
        ids["year"], ids["month"] = p.year, p.month
    engine.dispose()
    return {"url": url, "ids": ids}


def _client(env, subject_id: str) -> TestClient:
    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": subject_id, "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture()
def maker_client(env):
    return _client(env, env["ids"]["subject_id"])


@pytest.fixture()
def reviewer_client(env):
    return _client(env, env["ids"]["reviewer_id"])


@pytest.fixture()
def agent_client(env):
    return _client(env, env["ids"]["agent_id"])


@pytest.fixture()
def anon_client(env):
    from kernel.webapp import build_app

    return TestClient(build_app(env["url"]))


def _leaf_codes(client, ls_id) -> list[str]:
    r = client.get(f"/ledger/{ls_id}/voucher/new")
    assert r.status_code == 200
    return [c for c in re.findall(r'<option value="([^"]+)"', r.text) if c]


def _make_pushed(client, env, summary: str, amount="66.00") -> str:
    """Web 表单制单 + 推送，返回凭证 id。"""
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    r = client.post(
        f"/ledger/{ls}/voucher/new",
        data={
            "voucher_date": f"{env['ids']['year']}-{env['ids']['month']:02d}-15",
            "summary": summary,
            "account_code": [codes[0], codes[1]],
            "debit": [amount, ""],
            "credit": ["", amount],
            "action": "submit",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[-1]


# ---------- 待办列表 ----------


def test_todo_requires_login(anon_client, env):
    r = anon_client.get("/todo", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_todo_splits_by_role(maker_client, reviewer_client, env):
    """同一张待审单：制单人看到「撤回」，审批人看到「去处理」。"""
    vid = _make_pushed(maker_client, env, "待办分流")
    rt = reviewer_client.get("/todo").text
    mt = maker_client.get("/todo").text
    assert vid in rt and "去处理" in rt, "审批人应看到待审队列"
    assert vid in mt and "撤回" in mt, "制单人应看到自己的可撤回单"
    assert "待我审批" in rt and "我推送的" in mt


def test_todo_agent_sees_warning(agent_client, maker_client, env):
    _make_pushed(maker_client, env, "agent围观")
    t = agent_client.get("/todo").text
    assert "审批与驳回必须由人执行" in t
    assert "去处理" in t, "队列仍应可见（仅供查看）"


def test_nav_has_todo_link(maker_client, env):
    t = maker_client.get("/").text
    assert "/todo" in t, "工作区应有审批待办入口"


# ---------- 详情页操作区 ----------


def test_detail_buttons_by_role(maker_client, reviewer_client, env):
    vid = _make_pushed(maker_client, env, "按钮分流")
    rt = reviewer_client.get(f"/voucher/{vid}").text
    mt = maker_client.get(f"/voucher/{vid}").text
    assert "/approve" in rt and "/reject" in rt, "审批人应看到同意/驳回"
    assert f"/voucher/{vid}/withdraw" in mt, "制单人应看到撤回"
    assert "/approve" not in mt, "制单人不应看到同意按钮（NO_SELF_APPROVAL 的 UI 预防）"


def test_reject_requires_reason_textarea(reviewer_client, maker_client, env):
    vid = _make_pushed(maker_client, env, "驳回表单")
    t = reviewer_client.get(f"/voucher/{vid}").text
    assert 'name=reason' in t, "驳回表单必须有原因输入框"


# ---------- 跃迁路由：正常流 ----------


def test_approve_flow(reviewer_client, maker_client, env):
    vid = _make_pushed(maker_client, env, "网页审批")
    r = reviewer_client.post(f"/voucher/{vid}/approve", follow_redirects=False)
    assert r.status_code == 303
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.get(Voucher, vid)
        assert v.status == "APPROVED"
        ev = s.scalars(select(Event).where(
            Event.aggregate_id == vid, Event.event_type == E.VOUCHER_APPROVED
        )).first()
        assert ev is not None, "审批必须留事件"
        assert (ev.actor or {}).get("id") == env["ids"]["reviewer_id"]
    engine.dispose()


def test_reject_with_reason_returns_to_draft(reviewer_client, maker_client, env):
    vid = _make_pushed(maker_client, env, "网页驳回")
    r = reviewer_client.post(
        f"/voucher/{vid}/reject", data={"reason": "金额与发票不符"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.get(Voucher, vid)
        assert v.status == "DRAFT"
        ev = s.scalars(select(Event).where(
            Event.aggregate_id == vid, Event.event_type == E.VOUCHER_REJECTED
        )).first()
        assert ev is not None, "驳回必须留 VOUCHER_REJECTED 事件"
        assert (ev.payload or {}).get("reason") == "金额与发票不符", "原因必须留痕"
    engine.dispose()


def test_withdraw_by_maker(reviewer_client, maker_client, env):
    vid = _make_pushed(maker_client, env, "网页撤回")
    r = maker_client.post(f"/voucher/{vid}/withdraw", follow_redirects=False)
    assert r.status_code == 303
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.get(Voucher, vid)
        assert v.status == "DRAFT"
        ev = s.scalars(select(Event).where(
            Event.aggregate_id == vid, Event.event_type == E.VOUCHER_WITHDRAWN
        )).first()
        assert ev is not None, "撤回必须是 VOUCHER_WITHDRAWN 事件（与驳回可区分）"
    engine.dispose()


def test_post_after_approve_updates_balances(reviewer_client, maker_client, env):
    """过账走 post_voucher 全链路：状态 POSTED + balances 投影同步。"""
    vid = _make_pushed(maker_client, env, "网页过账", amount="88.00")
    assert reviewer_client.post(f"/voucher/{vid}/approve",
                                follow_redirects=False).status_code == 303
    r = maker_client.post(f"/voucher/{vid}/post", follow_redirects=True)
    assert r.status_code == 200
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.get(Voucher, vid)
        assert v.status == "POSTED"
        # 余额投影按「账套+期间+科目」聚合（无 voucher 维度），
        # 过账后必须出现 88.00 的发生额行 —— 否则就是跳过了 post_voucher。
        from decimal import Decimal

        rows = s.scalars(
            select(Balance).where(
                Balance.ledger_set_id == env["ids"]["ledger_set_id"],
                Balance.period_id == env["ids"]["period_id"],
            )
        ).all()
        assert any(
            Decimal("88.00") in (b.debit_total, b.credit_total) for b in rows
        ), f"balances 投影未反映过账金额，行数={len(rows)}"
    engine.dispose()


# ---------- 跃迁路由：门禁流 ----------


def test_self_approval_blocked_on_web(maker_client, env):
    """制单人强 POST approve 自己的单 → Web 端也要拦（错误信息可读）。"""
    vid = _make_pushed(maker_client, env, "自批拦截")
    r = maker_client.post(f"/voucher/{vid}/approve", follow_redirects=True)
    assert "不能" in r.text and ("审批" in r.text or "撤回" in r.text)
    engine = create_engine(env["url"])
    with Session(engine) as s:
        assert s.get(Voucher, vid).status == "PUSHED", "状态不得被篡改"
    engine.dispose()


def test_agent_cannot_approve(agent_client, maker_client, env):
    """Agent 即使有 reviewer 角色也必须被内核 AGENT_APPROVAL_FORBIDDEN 拦截
    （验证门禁来自状态机而非 casbin）。"""
    vid = _make_pushed(maker_client, env, "agent审批")
    r = agent_client.post(f"/voucher/{vid}/approve", follow_redirects=True)
    assert "Agent" in r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        assert s.get(Voucher, vid).status == "PUSHED"
    engine.dispose()


def test_reject_without_reason_is_blocked(reviewer_client, maker_client, env):
    vid = _make_pushed(maker_client, env, "空原因")
    r = reviewer_client.post(f"/voucher/{vid}/reject", data={"reason": ""},
                             follow_redirects=True)
    assert "原因" in r.text, "必须提示驳回需要原因"
    engine = create_engine(env["url"])
    with Session(engine) as s:
        assert s.get(Voucher, vid).status == "PUSHED"
    engine.dispose()


def test_non_maker_cannot_withdraw(reviewer_client, maker_client, env):
    """审批人不能「撤回」别人的单——他只有「驳回（带原因）」这一条路。"""
    vid = _make_pushed(maker_client, env, "越权撤回")
    r = reviewer_client.post(f"/voucher/{vid}/withdraw", follow_redirects=True)
    assert "制单人本人" in r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        assert s.get(Voucher, vid).status == "PUSHED"
        # 不应留下 VOUCHER_WITHDRAWN 事件
        ev = s.scalars(select(Event).where(
            Event.aggregate_id == vid, Event.event_type == E.VOUCHER_WITHDRAWN
        )).first()
        assert ev is None
    engine.dispose()


def test_error_is_displayed_on_detail_page(reviewer_client, maker_client, env):
    """错误信息带 query 参数回详情页可见，不能静默丢失。"""
    vid = _make_pushed(maker_client, env, "错误可见")
    r = reviewer_client.post(
        f"/voucher/{vid}/reject", data={"reason": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert f"/voucher/{vid}?error=" in loc, "错误必须随重定向带回页面"
    detail = reviewer_client.get(loc).text
    assert "原因" in detail


# ---------- 回归防线 ----------


def test_pushed_cannot_post_directly(maker_client, reviewer_client, env):
    """没审批就点过账 → POSTED 分支要求状态为 APPROVED，必须被拒。"""
    vid = _make_pushed(maker_client, env, "跳步过账")
    r = maker_client.post(f"/voucher/{vid}/post", follow_redirects=True)
    assert "APPROVED" in r.text or "记账" in r.text or "审批" in r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        assert s.get(Voucher, vid).status == "PUSHED"
    engine.dispose()
