"""E3 情景推演（TDD）。

DoD：
- what_if 以实际三表为种子跑基准 + 各杠杆情景，返回 baseline / variants / impact / impact_summary。
- ar_acceleration 杠杆 → 期末应收相对基准下降（delta<0）。
- margin_compression / growth_halt 杠杆 → 期末累计净利润相对基准下降。
- 只读：不改账（Voucher 行数不变）。
- tool_calls 逐条溯源（每杠杆含 assumption_overrides）。
"""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Period, Voucher
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.simulation import preset_lever_names, what_if
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


def _seed_data(sess, env):
    """造一笔「应收远大于月收入」的种子：AR 130k / AP 120k / 收入 10k（毛利派生=0）。"""
    _post(sess, env, [
        {"account_code": "1122", "debit": "120000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "2202", "debit": "", "credit": "120000",
         "aux_dims": {"supplier": "某供应商"}},
    ])
    _post(sess, env, [
        {"account_code": "1122", "debit": "10000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "10000"},
    ])


def test_preset_lever_names():
    names = preset_lever_names()
    assert "ar_acceleration" in names
    assert "margin_compression" in names
    assert "growth_halt" in names


def test_what_if_baseline_and_variants(sess, env):
    _seed_data(sess, env)
    res = what_if(
        sess, ledger_set_id=env["ledger_set_id"], base_year=2026, base_month=8,
        horizon=3, levers=["ar_acceleration", "margin_compression", "growth_halt"],
    )
    assert res["base_period"] == {"year": 2026, "month": 8}
    assert res["horizon"] == 3
    assert "baseline" in res and "periods" in res["baseline"]
    assert set(res["variants"]) == {
        "ar_acceleration", "margin_compression", "growth_halt"
    }
    # 溯源：每杠杆都带假设覆盖
    for name, v in res["variants"].items():
        assert "assumption_overrides" in v and v["assumption_overrides"]
        assert "impact" in v and "closing_cash" in v["impact"]
        assert "net_profit" in v["impact"]


def test_what_if_ar_acceleration_lowers_ar(sess, env):
    _seed_data(sess, env)
    res = what_if(
        sess, ledger_set_id=env["ledger_set_id"], base_year=2026, base_month=8,
        horizon=3, levers=["ar_acceleration"],
    )
    delta = Decimal(res["variants"]["ar_acceleration"]["impact"]["ar"]["delta"])
    assert delta < ZERO, f"加速回款应使期末应收下降，实际 delta={delta}"


def test_what_if_margin_compression_lowers_profit(sess, env):
    _seed_data(sess, env)
    res = what_if(
        sess, ledger_set_id=env["ledger_set_id"], base_year=2026, base_month=8,
        horizon=3, levers=["margin_compression"],
    )
    delta = Decimal(res["variants"]["margin_compression"]["impact"]["net_profit"]["delta"])
    assert delta < ZERO, f"毛利压缩应使净利润下降，实际 delta={delta}"


def test_what_if_growth_halt_lowers_profit(sess, env):
    _seed_data(sess, env)
    res = what_if(
        sess, ledger_set_id=env["ledger_set_id"], base_year=2026, base_month=8,
        horizon=3, levers=["growth_halt"],
    )
    delta = Decimal(res["variants"]["growth_halt"]["impact"]["net_profit"]["delta"])
    assert delta < ZERO, f"增长停滞应使净利润下降，实际 delta={delta}"


def test_what_if_impact_summary_present(sess, env):
    _seed_data(sess, env)
    res = what_if(
        sess, ledger_set_id=env["ledger_set_id"], base_year=2026, base_month=8,
        horizon=6, levers=["ar_acceleration", "margin_compression"],
    )
    s = res["impact_summary"]
    assert "closing_cash" in s and "net_profit" in s
    assert Decimal(s["closing_cash"]["baseline"]) == res["baseline"]["periods"][-1][
        "balance_sheet"]["cash"]


def test_what_if_readonly(sess, env):
    _seed_data(sess, env)
    before = len(sess.scalars(
        select(Voucher).where(Voucher.ledger_set_id == env["ledger_set_id"])
    ).all())
    what_if(
        sess, ledger_set_id=env["ledger_set_id"], base_year=2026, base_month=8,
        horizon=3, levers=["ar_acceleration", "margin_compression"],
    )
    after = len(sess.scalars(
        select(Voucher).where(Voucher.ledger_set_id == env["ledger_set_id"])
    ).all())
    assert before == after, "what_if 必须只读，绝不写账"
