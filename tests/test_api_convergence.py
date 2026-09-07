"""P0 接口收敛：工具名 / 期间参数 / 会计口径 / 通道参数 的单一真源。

这些断言测的不是功能，是**接口契约**——任何一处退回去都会红。
目的：防止后续加功能时杂草再长回来（历史上同一概念曾出现三套叫法）。

覆盖：
- P0-A1 会话自举改名 get_workspace → get_session_context（旧名保留为别名）
- P0-A2 期间参数统一为 period_year / period_month
- P0-A3 会计口径以账套设置为唯一来源（不再各工具硬编码默认值）
- P0-A4 审批通道参数统一为 user（+ 飞书特有 user_type）
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Subject  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402

# 报表/期末类工具：口径应取自账套，不应各自硬编码
STANDARD_TOOLS = (
    "report_balance_sheet",
    "report_income_statement",
    "report_cash_flow",
    "close_period",
    "open_next_period",
    "reconcile_ledger",
)


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/conv.db"
    engine = create_engine(url, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.flush()
        ids["reviewer_subject_id"] = reviewer.id
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def server(env):
    from xerp_mcp.server import build_server

    return build_server(env["url"])


def _call(server, name, **args):
    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(name, args)
            if getattr(res, "data", None) is not None:
                return res.data
            import json

            return json.loads(res.content[0].text)

    return asyncio.run(inner())


def _tools(server):
    async def inner():
        return {t.name: t for t in await server.list_tools()}

    return asyncio.run(inner())


def _props(tool):
    return set((tool.parameters or {}).get("properties", {}))


# ---------------- P0-A1 会话自举改名 ----------------


def test_session_context_registered(server):
    assert "get_session_context" in _tools(server)


def test_workspace_alias_kept_and_identical(server):
    """旧名必须还在且返回值完全一致 —— 交付包已发出，别名是兜底。"""
    tools = _tools(server)
    assert "get_workspace" in tools
    assert _call(server, "get_session_context") == _call(server, "get_workspace")


def test_workspace_alias_marked_deprecated(server):
    tools = _tools(server)
    doc = (tools["get_workspace"].description or "") + (
        tools["get_workspace"].title or ""
    )
    assert "废弃" in doc or "弃用" in doc, "旧名必须标注废弃，否则 AI 会继续优先用它"


def test_session_context_uses_period_prefix(server):
    r = _call(server, "get_session_context")
    ls = r["ledgers"][0]
    assert "accounting_standard" in ls
    for p in ls["open_periods"]:
        assert "period_year" in p and "period_month" in p, (
            f"期间字段应为 period_year/period_month，实际 {sorted(p)}"
        )


# ---------------- P0-A2 期间参数统一 ----------------


def test_no_bare_year_month_params(server):
    """全系统期间参数只有一套叫法。voucher_date 是业务日期，不算期间。"""
    offenders = []
    for name, t in _tools(server).items():
        bad = _props(t) & {"year", "month"}
        if bad:
            offenders.append((name, sorted(bad)))
    assert not offenders, f"期间参数未统一为 period_year/period_month：{offenders}"


def test_period_params_always_paired(server):
    for name, t in _tools(server).items():
        p = _props(t)
        if "period_year" in p:
            assert "period_month" in p, f"{name} 有 period_year 却缺 period_month"


def test_partner_balances_accepts_period_params(env, server):
    """改名后仍可用（不传期间走「最新 OPEN」分支）。"""
    r = _call(server, "partner_balances", ledger_set_id=env["ids"]["ledger_set_id"])
    assert r["ok"] is True


# ---------------- P0-A3 会计口径单一真源 ----------------


def test_standard_default_is_empty(server):
    """默认留空 = 取账套值。禁止再硬编码 small_business。"""
    tools = _tools(server)
    for name in STANDARD_TOOLS:
        schema = (tools[name].parameters or {}).get("properties", {})
        assert "accounting_standard" in schema, name
        got = schema["accounting_standard"].get("default")
        assert got == "", f"{name} 的 accounting_standard 默认应为 ''（取账套），实际 {got!r}"


def test_standard_mismatch_rejected(env, server):
    r = _call(
        server,
        "report_balance_sheet",
        ledger_set_id=env["ids"]["ledger_set_id"],
        period_year=2026,
        period_month=8,
        accounting_standard="__not_a_standard__",
    )
    assert r["ok"] is False
    assert r["error"]["code"] == "STANDARD_MISMATCH"
    assert "actual" in r["error"]["details"], "错误详情要给出账套真实口径，便于自查"


def test_standard_from_ledger_accepted(env, server):
    """传账套真实口径应通过 —— 证明「传了必须一致」不是一律拒绝。"""
    std = _call(server, "get_session_context")["ledgers"][0]["accounting_standard"]
    r = _call(
        server,
        "report_balance_sheet",
        ledger_set_id=env["ids"]["ledger_set_id"],
        period_year=2026,
        period_month=8,
        accounting_standard=std,
    )
    assert r["ok"] is True


# ---------------- P0-A4 通道参数统一 ----------------


def test_channel_params_aligned(server):
    tools = _tools(server)
    fs = _props(tools["feishu_send_approval"])
    ws = _props(tools["wecom_send_approval"])
    assert "user" in fs, f"飞书侧应统一为 user，实际 {sorted(fs)}"
    assert "user" in ws, f"企微侧应有 user，实际 {sorted(ws)}"
    assert not (fs & {"receive_id", "receive_id_type"}), "飞书旧参数名应已移除"


def test_channel_user_type_optional(server):
    """user_type 是飞书特有（open_id/chat_id），必须是可选的。"""
    schema = (_tools(server)["feishu_send_approval"].parameters or {}).get(
        "properties", {}
    )
    assert schema["user_type"].get("default") == "open_id"
