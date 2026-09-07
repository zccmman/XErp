"""P1-01 三表前向预测契约测试。

锁定（见 kernel/forecast.py）：
- 预测是物化视图：纯驱动假设确定性推导，三表内部完全勾稽；
- 每期资产负债表 balanced（资产=负债+权益），现金流 closing_cash == 资产负债表 cash；
- 净利润流向留存收益（re_t = re_{t-1} + ni_t − div_t）；
- best/base/worst 三情景在正向增长基准下收入单调有序；
- 端到端：从上期末实际三表抽取种子 → 预测，仍勾稽。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Period, Subject, Voucher
from kernel.forecast import (
    Assumptions,
    Seed,
    build_scenarios,
    extract_seed_from_actuals,
    forecast_from_actuals,
    forecast_statements,
)
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher

ZERO = Decimal("0")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed() -> Seed:
    return Seed(
        cash=Decimal("1000"),
        ar=Decimal("500"),
        ap=Decimal("300"),
        inventory=Decimal("200"),
        fa_gross=Decimal("2000"),
        accum_dep=Decimal("500"),
        paid_in_capital=Decimal("1000"),
        retained_earnings=Decimal("1900"),
        revenue=Decimal("12000"),
    )


def _asm() -> Assumptions:
    return Assumptions(
        rev_growth=Decimal("0.03"),
        gross_margin=Decimal("0.60"),
        opex_ratio=Decimal("0.25"),
        tax_rate=Decimal("0.00"),
        ar_days=30,
        ap_days=30,
        inv_days=30,
        capex_pct=Decimal("0.00"),
        dep_rate=Decimal("0.10"),
        dividend_pct=Decimal("0.00"),
        debt_draw=ZERO,
    )


def _run(seed=None, asm=None, horizon=3):
    return forecast_statements(
        seed or _seed(), asm or _asm(), horizon, 2026, 9, "base"
    )


# ---------- 勾稽：每期平衡 + 现金流与资产负债表现金一致 ----------


def test_every_period_balanced_and_cash_reconciles():
    out = _run(horizon=6)
    assert len(out["periods"]) == 6
    for p in out["periods"]:
        assert p["balance_sheet"]["balanced"] is True, p["balance_sheet"]["check"]
        assert p["cash_flow"]["reconcile"]["ok"] is True
        # 现金流期末现金 == 资产负债表货币资金
        assert p["cash_flow"]["closing_cash"] == p["balance_sheet"]["cash"]


def test_zero_growth_flat_still_balanced():
    asm = _asm()
    asm.rev_growth = Decimal("0")
    asm.capex_pct = Decimal("0")
    asm.dividend_pct = Decimal("0")
    asm.debt_draw = ZERO
    out = _run(asm=asm, horizon=4)
    for p in out["periods"]:
        assert p["balance_sheet"]["balanced"] is True
        assert p["cash_flow"]["reconcile"]["ok"] is True


# ---------- 净利润流向留存收益 ----------


def test_net_income_flows_to_retained_earnings():
    seed = _seed()
    out = _run(seed=seed, horizon=3)
    prev_re = seed.retained_earnings
    for p in out["periods"]:
        ni = p["income_statement"]["net_profit"]
        div = ni * _asm().dividend_pct  # 0
        expected_re = prev_re + ni - div
        assert p["balance_sheet"]["retained_earnings"] == expected_re
        prev_re = expected_re


# ---------- 多情景有序 ----------


def test_scenarios_revenue_ordered():
    seed = _seed()
    asm = _asm()
    sc = build_scenarios(asm)
    last_base = forecast_statements(seed, sc["base"], 12, 2026, 9, "base")["periods"][-1]["income_statement"]["revenue"]
    last_best = forecast_statements(seed, sc["best"], 12, 2026, 9, "best")["periods"][-1]["income_statement"]["revenue"]
    last_worst = forecast_statements(seed, sc["worst"], 12, 2026, 9, "worst")["periods"][-1]["income_statement"]["revenue"]
    assert last_best > last_base > last_worst


# ---------- 端到端：从实际数种子 ----------


@pytest.fixture()
def demo_ctx(session):
    ids = seed_demo_ledger(session)
    session.flush()
    approver = Subject(type="user", display_name="审批人")
    session.add(approver)
    session.flush()
    return {"s": session, "ids": ids, "drafter": ids["subject_id"], "approver": approver.id}


def _post_revenue(ctx):
    s = ctx["s"]
    # 确保 2026-09 期间存在且 OPEN（跨月记账前置）
    period = s.scalars(
        select(Period).where(
            Period.ledger_set_id == ctx["ids"]["ledger_set_id"],
            Period.year == 2026,
            Period.month == 9,
        )
    ).first()
    if period is None:
        period = Period(
            ledger_set_id=ctx["ids"]["ledger_set_id"],
            year=2026,
            month=9,
            status="OPEN",
        )
        s.add(period)
        s.flush()
    v, _ = create_draft_voucher(
        s,
        ledger_set_id=ctx["ids"]["ledger_set_id"],
        actor={"type": "user", "id": ctx["drafter"]},
        voucher_date=date(2026, 9, 10),
        summary="销售收入",
        lines=[
            {"account_code": "1002", "debit": "1000.00", "credit": ""},
            {"account_code": "6001", "debit": "", "credit": "1000.00"},
        ],
    )
    transition(s, voucher_id=v.id, actor={"type": "user", "id": ctx["drafter"]}, target="PUSHED")
    transition(s, voucher_id=v.id, actor={"type": "user", "id": ctx["approver"]}, target="APPROVED")
    from kernel.posting import post_voucher
    post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ctx["approver"]})
    s.flush()
    return v


def _demo_period(ctx):
    s = ctx["s"]
    return s.scalars(select(Period).where(
        Period.ledger_set_id == ctx["ids"]["ledger_set_id"],
        Period.year == 2026,
        Period.month == 9,
    )).first()


def test_seed_extraction_pulls_revenue(demo_ctx):
    _post_revenue(demo_ctx)
    s = demo_ctx["s"]
    ls = demo_ctx["ids"]["ledger_set_id"]
    period = _demo_period(demo_ctx)
    seed, asm = extract_seed_from_actuals(s, ls, period.year, period.month)
    assert seed.revenue == Decimal("1000")  # 本月一笔收入
    assert asm.gross_margin == Decimal("0")  # 无成本 → 默认 0
    # 有收入但无应收 → ar_days 回退到下限 1；无成本 → ap/inv 天数回退 30
    assert asm.ar_days == 1
    assert asm.ap_days == 30
    assert asm.inv_days == 30


def test_forecast_from_actuals_balanced(demo_ctx):
    _post_revenue(demo_ctx)
    s = demo_ctx["s"]
    ls = demo_ctx["ids"]["ledger_set_id"]
    period = _demo_period(demo_ctx)
    out = forecast_from_actuals(s, ls, period.year, period.month, horizon=6, scenario="base")
    assert len(out["periods"]) == 6
    for p in out["periods"]:
        assert p["balance_sheet"]["balanced"] is True, p["balance_sheet"]["check"]
        assert p["cash_flow"]["reconcile"]["ok"] is True


def test_forecast_all_scenarios(demo_ctx):
    _post_revenue(demo_ctx)
    s = demo_ctx["s"]
    ls = demo_ctx["ids"]["ledger_set_id"]
    period = _demo_period(demo_ctx)
    out = forecast_from_actuals(s, ls, period.year, period.month, horizon=3, scenario="all")
    assert set(out["scenarios"].keys()) == {"best", "base", "worst"}
    for sc in out["scenarios"].values():
        for p in sc["periods"]:
            assert p["balance_sheet"]["balanced"] is True
