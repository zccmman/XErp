"""P0-07 TDD：MCP Server 七工具（fastmcp 内存客户端替代 inspector 手测）。

覆盖：list_accounts / get_voucher 只读；create→push→approve→post 全链路；
balances 投影；不平衡硬拒（结构化中文错误）；禁止自审；跳步跃迁拒绝；幂等重放。
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


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/mcp.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        stats = import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.flush()
        ids["coa_created"] = stats["created"]
        ids["reviewer_subject_id"] = reviewer.id
        s.commit()  # MCP 工具用独立连接访问该库，种子必须真正落盘
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def server(env):
    from ledgeros_mcp.server import build_server

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


def _actor(env):
    return env["ids"]["subject_id"]


def _reviewer(env):
    return env["ids"]["reviewer_subject_id"]


def _balanced_lines():
    return [
        {"account_code": "6602", "debit": "800", "credit": ""},
        {"account_code": "1001", "debit": "", "credit": "800"},
    ]


def _mk_voucher(env, server, key=None):
    return _call(
        server,
        "create_voucher",
        ledger_set_id=env["ids"]["ledger_set_id"],
        voucher_date="2026-08-27",
        summary="联调凭证",
        actor_id=_actor(env),
        idempotency_key=key,
        lines=_balanced_lines(),
    )


# ---- 只读工具 ----


def test_get_workspace_bootstrap(env, server):
    r = _call(server, "get_workspace")
    assert r["ok"] is True
    assert any(ls["name"] == "演示账套" for ls in r["ledgers"])
    assert all(
        ls["open_periods"] == [{"year": 2026, "month": 8}] for ls in r["ledgers"]
    )
    names = {x["display_name"] for x in r["subjects"]}
    assert {"丞辰", "审批人"} <= names



def test_list_accounts(env, server):
    r = _call(server, "list_accounts", ledger_set_id=env["ids"]["ledger_set_id"])
    assert r["ok"] is True and len(r["accounts"]) >= 144
    assert any(a["name"] == "库存现金" for a in r["accounts"])


def test_get_voucher_roundtrip(env, server):
    made = _mk_voucher(env, server)
    assert made["ok"] is True
    g = _call(server, "get_voucher", voucher_id=made["voucher"]["id"])
    assert g["ok"] and g["voucher"]["status"] == "DRAFT"
    assert g["voucher"]["lines"][0]["account_code"] == "6602"


# ---- 写入链路 ----


def test_happy_path_to_posted_and_balances(env, server):
    made = _mk_voucher(env, server)
    vid = made["voucher"]["id"]

    push = _call(server, "push_voucher", voucher_id=vid, actor_id=_actor(env))
    assert push["ok"] and push["voucher"]["status"] == "PUSHED"

    apr = _call(server, "approve_voucher", voucher_id=vid, actor_id=_reviewer(env))
    assert apr["ok"] and apr["voucher"]["status"] == "APPROVED"

    post = _call(server, "post_voucher", voucher_id=vid, actor_id=_actor(env))
    assert post["ok"] and post["voucher"]["status"] == "POSTED"

    bal = _call(
        server,
        "query_balances",
        ledger_set_id=env["ids"]["ledger_set_id"],
        period_year=2026,
        period_month=8,
    )
    assert bal["ok"]
    totals = {b["account_code"]: b for b in bal["balances"]}
    assert totals["6602"]["debit_total"] == "800.00"
    assert totals["1001"]["credit_total"] == "800.00"


def test_unbalanced_rejected_with_zh_message(env, server):
    bad = _call(
        server,
        "create_voucher",
        ledger_set_id=env["ids"]["ledger_set_id"],
        voucher_date="2026-08-27",
        summary="不平凭证",
        actor_id=_actor(env),
        idempotency_key=None,
        lines=[
            {"account_code": "6602", "debit": "500", "credit": ""},
            {"account_code": "1001", "debit": "", "credit": "300"},
        ],
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "VOUCHER_UNBALANCED"
    msg = bad["error"]["message_zh"]
    assert isinstance(msg, str) and "借贷不平衡" in msg and "500" in msg


def test_no_self_approval(env, server):
    made = _mk_voucher(env, server)
    vid = made["voucher"]["id"]
    _call(server, "push_voucher", voucher_id=vid, actor_id=_actor(env))
    r = _call(server, "approve_voucher", voucher_id=vid, actor_id=_actor(env))
    assert r["ok"] is False and r["error"]["code"] == "NO_SELF_APPROVAL"


def test_skip_push_transition_rejected(env, server):
    made = _mk_voucher(env, server)
    r = _call(
        server, "approve_voucher", voucher_id=made["voucher"]["id"], actor_id=_reviewer(env)
    )
    assert r["ok"] is False and r["error"]["code"] == "INVALID_TRANSITION"


def test_idempotent_create_replays_same_voucher(env, server):
    a = _mk_voucher(env, server, key="idem-001")
    b = _mk_voucher(env, server, key="idem-001")
    assert a["ok"] and b["ok"]
    assert b.get("replayed") is True
    assert a["voucher"]["id"] == b["voucher"]["id"]
