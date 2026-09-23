"""Phase E / E2 实时 Copilot（确定性路由，TDD）。

DoD：
- 意图路由：逾期 / 全貌 / 对账 / 回款待匹配 / 外币 / 总览 正确路由到只读内核。
- 溯源：返回 tool_calls 列出实际调用的只读内核。
- 只读不变量：ask 不改账。
- 严重项（授信超额）→ severity=ALERT 并经算子信号桥置 ALERT（复用跨进程桥）。
- MCP copilot_ask 同源只读包装正确（空账套返回 overview）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.copilot import ask
from kernel.db.base import Base
from kernel.db.models import Period
from kernel.posting import post_voucher
from kernel.reporting.credit import set_credit_limit
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))


@pytest.fixture()
def sess():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def env(sess):
    ids = seed_demo_ledger(sess)
    import_chart_of_accounts(sess, ids["ledger_set_id"], load_template_rows())
    p = sess.scalars(
        select(Period).where(
            Period.ledger_set_id == ids["ledger_set_id"],
            Period.year == 2026, Period.month == 8,
        )
    ).first()
    if p is None:
        p = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                   status="OPEN")
        sess.add(p)
    sess.flush()
    return ids


def _post(sess, env, lines, voucher_date="2026-08-05"):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date=voucher_date, summary="x", lines=lines,
    )
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()
    return v


def test_route_partner_profile(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    out = ask(sess, ledger_set_id=env["ledger_set_id"],
              question_zh="示例科技 全貌怎么样", as_of_date=date(2026, 8, 31))
    assert out["intent"] == "partner_profile"
    assert "示例科技" in out["answer_zh"]
    assert out["evidence"]["partner_profile"]["partner"] == "示例科技"
    assert any(tc["tool"] == "operating_partner_profile" for tc in out["tool_calls"])


def test_route_overview(sess, env):
    out = ask(sess, ledger_set_id=env["ledger_set_id"],
              question_zh="应收敞口集中度", as_of_date=date(2026, 8, 31))
    assert out["intent"] == "overview"
    assert any(tc["tool"] == "operating_graph_metrics" for tc in out["tool_calls"])


def test_route_reconcile(sess, env):
    out = ask(sess, ledger_set_id=env["ledger_set_id"],
              question_zh="子账总账对账", as_of_date=date(2026, 8, 31))
    assert out["intent"] == "reconcile"
    assert any(tc["tool"] == "reconcile_subledger_gl" for tc in out["tool_calls"])


def test_route_unmatched(sess, env):
    out = ask(sess, ledger_set_id=env["ledger_set_id"],
              question_zh="还有哪些回款没匹配", as_of_date=date(2026, 8, 31))
    assert out["intent"] == "unmatched"
    assert any("unmatched" in tc["tool"] for tc in out["tool_calls"])


def test_route_collections(sess, env):
    out = ask(sess, ledger_set_id=env["ledger_set_id"],
              question_zh="谁逾期了该催收", as_of_date=date(2026, 8, 31))
    assert out["intent"] == "collections"
    assert any(tc["tool"] == "arap_collections_draft" for tc in out["tool_calls"])


def test_route_foreign(sess, env):
    out = ask(sess, ledger_set_id=env["ledger_set_id"],
              question_zh="外币 汇兑损益 重估", as_of_date=date(2026, 8, 31))
    assert out["intent"] == "foreign"
    assert any(tc["tool"] == "foreign_trial_balance" for tc in out["tool_calls"])


def test_alert_on_breach(sess, env, tmp_path):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    set_credit_limit(sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
                     partner="示例科技", limit="300", actor={"id": "u1"})
    sess.commit()

    # 重定向算子信号桥到临时文件，避免污染仓库根
    os.environ["XERP_OPERATOR_STATE_FILE"] = str(tmp_path / "op.json")
    try:
        out = ask(sess, ledger_set_id=env["ledger_set_id"],
                  question_zh="示例科技 全貌", as_of_date=date(2026, 8, 31))
        assert out["severity"] == "ALERT"
        raw = json.loads(Path(os.environ["XERP_OPERATOR_STATE_FILE"]).read_text(encoding="utf-8"))
        assert raw["state"] == "alert"
        assert raw["source"] == "copilot"
    finally:
        os.environ.pop("XERP_OPERATOR_STATE_FILE", None)


def test_copilot_readonly(sess, env):
    def _count():
        from kernel.db.models import Voucher, VoucherLine

        return len(sess.scalars(
            select(VoucherLine.id).where(
                VoucherLine.voucher_id.in_(
                    select(Voucher.id).where(
                        Voucher.ledger_set_id == env["ledger_set_id"])
                )
            )
        ).all())

    before = _count()
    ask(sess, ledger_set_id=env["ledger_set_id"],
        question_zh="应收敞口集中度", as_of_date=date(2026, 8, 31))
    ask(sess, ledger_set_id=env["ledger_set_id"],
        question_zh="示例科技 全貌", as_of_date=date(2026, 8, 31))
    assert _count() == before  # 只读，绝不写账


def test_mcp_copilot_ask_empty():
    """MCP 包装同源只读：空账套调用返回 ok，intent=overview（验证工具注册与接线）。"""
    from xerp_mcp.server import build_server

    d = tempfile.mkdtemp()
    server = build_server(f"sqlite:///{d}/fresh.db")

    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(
                "copilot_ask",
                {"ledger_set_id": "nonexistent", "question_zh": "应收敞口集中度"},
            )
            if getattr(res, "data", None) is not None:
                return res.data
            return json.loads(res.content[0].text)

    out = asyncio.run(inner())
    assert out["ok"] is True
    assert out["report"]["intent"] == "overview"
