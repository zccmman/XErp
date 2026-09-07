"""D5 TDD：aux_dims 维度声明校验下沉到 validate_voucher（内核与适配器统一）。

断言：
- 行提供的 aux_dims key 必须 ∈ 科目 aux_dim_defs；
- 科目无声明维度则行不得带任何维度；
- 与适配器 _resolve_aux_dims 语义对齐：有维度才校验匹配，不强制全提供。
"""

from __future__ import annotations

import pytest
from datetime import date
from decimal import Decimal
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Account
from kernel.posting import PostingError, PostingLine, validate_voucher
from kernel.seed import seed_demo_ledger

ZERO = Decimal("0.00")


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        accs = {a.id: a for a in s.scalars(
            select(Account).where(
                Account.ledger_set_id == ids["ledger_set_id"])).all()}
        yield {"s": s, "ids": ids, "accs": accs}


def _call(accs, lines):
    validate_voucher(
        lines=lines, accounts_by_id=accs,
        period_status="OPEN", period_year=2026, period_month=8,
        voucher_date=date(2026, 8, 27),
    )


def test_declared_dimension_accepted(ctx):
    """科目声明了 department → 行带 department 通过。"""
    exp = ctx["accs"][ctx["ids"]["expense_account_id"]]  # 6602 声明 department
    assert "department" in (exp.aux_dim_defs or [])
    _call(ctx["accs"], [
        PostingLine(exp.id, Decimal("100.00"), ZERO, {"department": "销售部"}),
        PostingLine(ctx["ids"]["cash_account_id"], ZERO, Decimal("100.00")),
    ])


def test_undeclared_dimension_rejected(ctx):
    """科目未声明任何维度 → 行带 department 报 AUX_DIM_UNDECLARED。"""
    cash = ctx["accs"][ctx["ids"]["cash_account_id"]]  # 1001 无声明
    assert not (cash.aux_dim_defs or [])
    with pytest.raises(PostingError) as ei:
        _call(ctx["accs"], [
            PostingLine(cash.id, Decimal("100.00"), ZERO, {"department": "销售部"}),
            PostingLine(ctx["ids"]["expense_account_id"], ZERO, Decimal("100.00")),
        ])
    assert ei.value.code == "AUX_DIM_UNDECLARED"
    assert "department" in ei.value.details["illegal"]


def test_declared_but_not_provided_accepted(ctx):
    """声明了维度但本行不提供 → 通过（与适配器一致：不强制全提供）。"""
    exp = ctx["accs"][ctx["ids"]["expense_account_id"]]
    _call(ctx["accs"], [
        PostingLine(exp.id, Decimal("100.00"), ZERO),  # 不带 aux_dims
        PostingLine(ctx["ids"]["cash_account_id"], ZERO, Decimal("100.00")),
    ])


def test_partial_dimensions_accepted(ctx):
    """多维度科目：只提供部分已声明维度 → 通过。"""
    cash = ctx["accs"][ctx["ids"]["cash_account_id"]]
    cash.aux_dim_defs = ["customer", "department"]
    ctx["s"].flush()
    _call(ctx["accs"], [
        PostingLine(cash.id, Decimal("100.00"), ZERO, {"customer": "客户甲"}),
        PostingLine(ctx["ids"]["expense_account_id"], ZERO, Decimal("100.00")),
    ])
