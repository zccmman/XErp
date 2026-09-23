"""G9 子账↔总账对账（TDD）。

DoD：
- subledger_gl_reconcile(customer) 控制科目 1122 总额 == 客户明细余额之和 → ok。
- 漏挂往来单位（入 1122 无 customer）→ 差异≠0，unassigned_lines 列出，ok=False。
- supplier（2202）同理。
- 只读：不改账。
- 坏 dim_key → ArapError。
- MCP reconcile_subledger_gl 同源只读包装正确。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Period, Voucher
from kernel.posting import post_voucher
from kernel.reporting.arap import (
    ArapError,
    subledger_gl_reconcile,
)
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher

ZERO = Decimal("0.00")


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


def test_ar_reconciles(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    rep = subledger_gl_reconcile(sess, ledger_set_id=env["ledger_set_id"],
                                dim_key="customer", as_of_date=date(2026, 8, 31))
    assert rep["ok"] is True
    assert Decimal(rep["control_total"]) == Decimal("1000.00")
    assert Decimal(rep["subledger_total"]) == Decimal("1000.00")
    assert Decimal(rep["difference"]) == ZERO
    assert rep["partner_count"] == 1
    assert rep["partners"][0]["partner"] == "示例科技"


def test_ar_mismatch_unassigned(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    _post(sess, env, [
        {"account_code": "1122", "debit": "500", "credit": ""},  # 漏挂客户
        {"account_code": "1001", "debit": "", "credit": "500"},
    ], voucher_date="2026-08-06")
    rep = subledger_gl_reconcile(sess, ledger_set_id=env["ledger_set_id"],
                                dim_key="customer", as_of_date=date(2026, 8,31))
    assert rep["ok"] is False
    assert Decimal(rep["control_total"]) == Decimal("1500.00")
    assert Decimal(rep["subledger_total"]) == Decimal("1000.00")
    assert Decimal(rep["difference"]) == Decimal("500.00")
    assert rep["unassigned_count"] == 1
    assert Decimal(rep["unassigned_total"]) == Decimal("500.00")


def test_ap_reconciles(sess, env):
    _post(sess, env, [
        {"account_code": "6001", "debit": "800", "credit": ""},
        {"account_code": "2202", "debit": "", "credit": "800",
         "aux_dims": {"supplier": "供货商A"}},
    ])
    rep = subledger_gl_reconcile(sess, ledger_set_id=env["ledger_set_id"],
                                dim_key="supplier", as_of_date=date(2026, 8, 31))
    assert rep["ok"] is True
    assert Decimal(rep["control_total"]) == Decimal("800.00")
    assert Decimal(rep["subledger_total"]) == Decimal("800.00")
    assert rep["partner_count"] == 1


def test_ap_mismatch(sess, env):
    _post(sess, env, [
        {"account_code": "6001", "debit": "800", "credit": ""},
        {"account_code": "2202", "debit": "", "credit": "800",
         "aux_dims": {"supplier": "供货商A"}},
    ])
    _post(sess, env, [
        {"account_code": "6602", "debit": "200", "credit": ""},
        {"account_code": "2202", "debit": "", "credit": "200"},  # 漏挂供应商
    ], voucher_date="2026-08-06")
    rep = subledger_gl_reconcile(sess, ledger_set_id=env["ledger_set_id"],
                                dim_key="supplier", as_of_date=date(2026, 8, 31))
    assert rep["ok"] is False
    assert Decimal(rep["difference"]) == Decimal("200.00")


def test_readonly(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    before = len(sess.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == env["ledger_set_id"])).all())
    subledger_gl_reconcile(sess, ledger_set_id=env["ledger_set_id"],
                          dim_key="customer", as_of_date=date(2026, 8, 31))
    after = len(sess.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == env["ledger_set_id"])).all())
    assert before == after  # 只读，绝不写账


def test_bad_dim(sess, env):
    with pytest.raises(ArapError):
        subledger_gl_reconcile(sess, ledger_set_id=env["ledger_set_id"],
                              dim_key="nonsense", as_of_date=date(2026, 8, 31))


def test_mcp_reconcile_subledger_gl_empty():
    """MCP 包装同源只读：空账套调用返回 ok=True、无控制科目（验证工具注册与接线）。"""
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT / "mcp-server"))
    from xerp_mcp.server import build_server

    d = tempfile.mkdtemp()
    server = build_server(f"sqlite:///{d}/fresh.db")

    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(
                "reconcile_subledger_gl",
                {"ledger_set_id": "nonexistent", "dim_key": "customer"},
            )
            if getattr(res, "data", None) is not None:
                return res.data
            import json

            return json.loads(res.content[0].text)

    out = asyncio.run(inner())
    assert out["ok"] is True
    assert out["report"]["accounts"] == []
