"""P0-10 TDD：Drill 对话式建账向导。

DoD：空白库两步工具调用（init_ledger_set → import_opening_balances）得到可记账账套；
期初试算不平被拦截且不落任何数据。
"""
import asyncio
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/drill.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        s.commit()
        ids["coa"] = len(load_template_rows())
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def clean_server(env):
    """指向独立空库的 server（模拟全新部署）。"""
    d = tempfile.mkdtemp()
    from xerp_mcp.server import build_server

    return build_server(f"sqlite:///{d}/fresh.db")


def _call(server, tool, **args):
    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(tool, args)
            if getattr(res, "data", None) is not None:
                return res.data
            import json

            return json.loads(res.content[0].text)

    return asyncio.run(inner())


def test_kernel_import_opening_balances():
    """内核层：期初导入→POSTED 凭证+投影+链完整；不平→拦截。"""
    from kernel.db.models import Balance, Voucher
    from kernel.ledger import verify_chain
    from kernel.opening import import_opening_balances

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ls_ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ls_ids["ledger_set_id"], load_template_rows())
    s.commit()
    actor = {"type": "user", "id": ls_ids["subject_id"]}

    v = import_opening_balances(
        s,
        ledger_set_id=ls_ids["ledger_set_id"],
        actor=actor,
        lines=[
            {"account_code": "1002", "debit": "50000", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "50000"},
        ],
    )
    s.commit()
    assert v.status == "POSTED" and v.voucher_no.startswith("期初-")
    bal = s.scalars(select(Balance)).all()
    totals = {b.account_id: (b.debit_total, b.credit_total) for b in bal}
    assert totals[ls_ids["bank_account_id"]][0] == Decimal("50000.00")
    ok, problem = verify_chain(s, ls_ids["ledger_set_id"])
    assert ok and problem is None
    # 不平 → 拒绝且不落数据
    n_vouchers = len(s.scalars(select(Voucher)).all())
    with pytest.raises(Exception) as ei:
        import_opening_balances(
            s,
            ledger_set_id=ls_ids["ledger_set_id"],
            actor=actor,
            lines=[
                {"account_code": "1002", "debit": "10", "credit": ""},
                {"account_code": "3001", "debit": "", "credit": "6"},
            ],
        )
    assert ei.value.code == "TRIAL_BALANCE_UNBALANCED"
    s.rollback()
    assert len(s.scalars(select(Voucher)).all()) == n_vouchers
    s.close()


def test_drill_two_step_to_ready(clean_server):
    """MCP 层：空白库 → init_ledger_set → import_opening_balances → 可记账。"""
    r1 = _call(
        clean_server,
        "init_ledger_set",
        name="新创公司账套",
        owner_name="丞辰",
    )
    assert r1["ok"] is True
    assert r1["accounts_created"] >= 140
    assert r1["open_period"]["year"] == 2026
    ls_id = r1["ledger_set_id"]
    owner = r1["owner_subject_id"]

    r2 = _call(
        clean_server,
        "import_opening_balances",
        ledger_set_id=ls_id,
        actor_id=owner,
        lines=[
            {"account_code": "1002", "debit": "200000", "credit": ""},
            {"account_code": "1001", "debit": "5000", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "205000"},
        ],
    )
    assert r2["ok"] is True and r2["voucher"]["status"] == "POSTED"

    bal = _call(
        clean_server,
        "query_balances",
        ledger_set_id=ls_id,
        period_year=r1["open_period"]["year"],
        period_month=r1["open_period"]["month"],
    )
    totals = {b["account_code"]: b for b in bal["balances"]}
    assert totals["1002"]["debit_total"] == "200000.00"
    assert totals["3001"]["credit_total"] == "205000.00"

    # 幂等：同名列建账返回既有账套
    again = _call(clean_server, "init_ledger_set", name="新创公司账套", owner_name="丞辰")
    assert again["ok"] and again.get("replayed") is True


def test_drill_unbalanced_rejected_via_mcp(clean_server):
    r1 = _call(clean_server, "init_ledger_set", name="不平账套", owner_name="测试者")
    bad = _call(
        clean_server,
        "import_opening_balances",
        ledger_set_id=r1["ledger_set_id"],
        actor_id=r1["owner_subject_id"],
        lines=[
            {"account_code": "1002", "debit": "100", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "99"},
        ],
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "TRIAL_BALANCE_UNBALANCED"
