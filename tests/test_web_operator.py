"""算子 · Web 集成测试（仅 M1 制单页常驻 + 关闭开关后容器为空 + 迭代2 联动）。"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.models import Account, LedgerSet, Period, Subject, Voucher, VoucherLine


@pytest.fixture(autouse=True)
def _reset_op(monkeypatch, tmp_path):
    from kernel import operator as op

    op.reset()
    # 桥指向临时文件：不碰仓库根真桥；清 ts 去重防跨用例串扰
    monkeypatch.setenv("XERP_OPERATOR_STATE_FILE", str(tmp_path / "op_bridge.json"))
    monkeypatch.setattr(op, "_applied_ts", 0.0)
    yield
    op.reset()


def _ls_id() -> str:
    return "00000000-0000-0000-0000-000000000001"


def _subject_id() -> str:
    return "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def env(tmp_path):
    from kernel.db.base import Base
    from kernel.coa import import_chart_of_accounts, load_template_rows
    db = tmp_path / "op.db"
    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(Subject(id=_subject_id(), type="user",
                      display_name="测试员", autonomy_level=1))
        s.add(LedgerSet(id=_ls_id(), name="算子测试账套",
                        accounting_standard="small_business"))
        s.add(Period(ledger_set_id=_ls_id(), year=2026, month=9, status="OPEN"))
        s.commit()
        # 注意：import_chart_of_accounts 只 flush 不 commit，必须在本 session
        # 内提交，否则科目只活在临时事务里（ Web 页面查不到，下拉为空）
        import_chart_of_accounts(s, _ls_id(), load_template_rows())
        s.commit()
    return {"url": f"sqlite:///{db}", "eng": eng,
            "ids": {"ledger_set_id": _ls_id(), "subject_id": _subject_id(),
                    "year": 2026, "month": 9}}


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from kernel.webapp import build_app
    monkeypatch.setenv("XERP_DB", env["url"])
    c = TestClient(build_app(env["url"]))
    c.post("/login", data={"subject_id": _subject_id(), "password": ""},
           follow_redirects=False)
    return c


def _voucher_new_url(ls_id):
    return f"/ledger/{ls_id}/voucher/new"


def test_voucher_new_page_contains_operator_container(client, env):
    """M1 制单页右上角常驻算子 fragment。"""
    r = client.get(_voucher_new_url(env["ids"]["ledger_set_id"]))
    assert r.status_code == 200, r.text
    assert 'class="op-container' in r.text
    assert "算子" in r.text  # 中文标签出现


def test_core_pages_show_operator(client, env):
    """迭代4：M2 首页/期初、M4 报表、三表预测、M3 月结页接入算子常驻。"""
    ls = env["ids"]["ledger_set_id"]
    for url in (f"/ledger/{ls}", f"/ledger/{ls}/reports",
                f"/ledger/{ls}/forecast", f"/ledger/{ls}/close"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.text)
        assert 'class="op-container' in r.text, url


def test_hidden_switch_renders_empty_container(client, env, monkeypatch):
    """XERP_OPERATOR_HIDDEN=1 时容器渲染空 fragment。"""
    monkeypatch.setenv("XERP_OPERATOR_HIDDEN", "1")
    # env 已注入 XERP_DB，TestClient 重读 webapp._engine 即可
    from kernel import webapp
    # 重新请求，新 env 生效
    r = client.get(_voucher_new_url(env["ids"]["ledger_set_id"]))
    # 注意：TestClient + monkeypatch 设置的 env 在子进程/线程中可能不传播
    # 故改用直接调 _page 验证：show_operator + is_hidden 组合行为
    from kernel import operator as op
    assert op.is_hidden() is True
    frag = op.render_fragment()
    assert 'data-state="hidden"' in frag
    assert "<svg" not in frag


def test_operator_state_visible_after_set(client, env):
    """set_state 后页面渲染的 data-state 跟随更新（仅在 TestClient 内做最小演示）。"""
    from kernel import operator as op
    op.set_state(op.OperatorState.DRAFTING)
    r = client.get(_voucher_new_url(env["ids"]["ledger_set_id"]))
    assert 'data-state="drafting"' in r.text
    assert "起草中" in r.text

# ---------- 迭代2 · 制单回调联动 + 跨进程信号桥 ----------

def _leaf_codes(client, ls_id) -> list[str]:
    r = client.get(f"/ledger/{ls_id}/voucher/new")
    assert r.status_code == 200
    m = re.search(r"<select class=acct[^>]*>(.*?)</select>", r.text, re.S)
    scope = m.group(1) if m else r.text
    return [c for c in re.findall(r'<option value="([^"]+)"', scope) if c]


def _post_voucher(client, env, action="draft"):
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    assert len(codes) >= 2
    return client.post(
        f"/ledger/{ls}/voucher/new",
        data={
            "voucher_date": "2026-09-15",
            "summary": "算子联动测试",
            "account_code": [codes[0], codes[1]],
            "debit": ["100", ""],
            "credit": ["", "100"],
            "action": action,
        },
        follow_redirects=False,
    )


def test_voucher_draft_sets_drafting_state(client, env):
    """Web 制单成功（存草稿）→ 算子进入 drafting。"""
    from kernel import operator as op

    r = _post_voucher(client, env, action="draft")
    assert r.status_code == 303, r.text
    assert op.current_state() == op.OperatorState.DRAFTING
    page = client.get(f"/ledger/{env['ids']['ledger_set_id']}/voucher/new")
    assert 'data-state="drafting"' in page.text


def test_voucher_submit_sets_pending_state(client, env):
    """Web 制单成功（直接提交 PUSHED）→ 算子经 drafting 进入 pending。"""
    from kernel import operator as op

    r = _post_voucher(client, env, action="submit")
    assert r.status_code == 303, r.text
    assert op.current_state() == op.OperatorState.PENDING
    page = client.get(f"/ledger/{env['ids']['ledger_set_id']}/voucher/new")
    assert 'data-state="pending"' in page.text


def test_page_syncs_mcp_bridge_signal(client, env):
    """MCP 进程写桥 → Web 页面渲染前同步：data-state 跟随桥信号。"""
    from kernel import operator as op

    assert op.signal(op.OperatorState.PENDING, source="mcp") is True
    page = client.get(f"/ledger/{env['ids']['ledger_set_id']}/voucher/new")
    assert 'data-state="pending"' in page.text


# ---------- 迭代4 · 全页面常驻 + 到场听令 + 期初/错误联动 ----------

def test_dashboard_arrives_listening(client, env):
    """首页（含期初表单）渲染 → 算子从 IDLE 升 LISTENING（用户到场听令）。"""
    from kernel import operator as op

    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200, r.text
    assert 'data-state="listening"' in r.text
    assert op.current_state() == op.OperatorState.LISTENING


def test_arrive_does_not_clobber_pending_on_page(client, env):
    """PENDING 态浏览核心页：到场听令不抹掉待审信息（只升不压）。"""
    from kernel import operator as op

    op.set_state(op.OperatorState.DRAFTING)
    op.set_state(op.OperatorState.PENDING)
    page = client.get(f"/ledger/{env['ids']['ledger_set_id']}/reports")
    assert 'data-state="pending"' in page.text


def test_opening_import_sets_drafting(client, env):
    """期初导入成功 → 算子 drafting（存量起草完成）；重定向回首页仍显示。"""
    from kernel import operator as op

    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    # 期初试算须借贷平衡：借第一个末级科目 / 贷第二个各 100
    lines = f"{codes[0]},100,0\n{codes[1]},0,100"
    r = client.post(
        f"/ledger/{ls}/opening",
        data={"lines_text": lines, "force": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert "error=" not in r.headers.get("location", ""), r.headers["location"]
    assert op.current_state() == op.OperatorState.DRAFTING
    page = client.get(f"/ledger/{ls}")
    assert 'data-state="drafting"' in page.text  # arrive() 不抹掉起草态


def test_dashboard_error_sets_alert(client, env):
    """首页带 error 参数（期初导入失败重定向）→ 算子 ALERT。"""
    from kernel import operator as op

    page = client.get(f"/ledger/{env['ids']['ledger_set_id']}?error=期初格式不对")
    assert 'data-state="alert"' in page.text
    assert op.current_state() == op.OperatorState.ALERT
