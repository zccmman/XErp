"""D6 TDD：余额投影（Balance）语义契约钉死 —— 净额口径 vs 发生额漂移。

债务原文（docs/REVIEW-ontology.md D6）：投影语义契约漂移，净额 vs 发生额已咬人一次；
建议「Balance 文档宣示『净额口径』+ 测试固定」。

本文件把契约落成不可回退的断言（单一真源在 kernel/db/models.py Balance.docstring）：
1. Balance 每行 = 该期间借/贷**发生额**，换期另起一行，绝不跨期累计；
2. 账账核对对比基准确认是**净额 = debit_total − credit_total**，不是借贷合计（gross）；
   因此 P1-02 期末结转清理掉的净零损益行不会误报 PROJECTION_MISMATCH，
   而真正的 net 篡改仍能被检出。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Account, Balance, Period, Voucher, VoucherLine
from kernel.posting import post_voucher
from kernel.reconcile import reconcile_ledger
from kernel.seed import seed_demo_ledger

ZERO = Decimal("0.00")
ACTOR = {"type": "user", "id": "u1", "display_name": "测试"}


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        # 同账套开第二个期间，用于验证「换期另起一行、不跨期累计」
        p2 = Period(
            ledger_set_id=ids["ledger_set_id"], year=2026, month=9, status="OPEN"
        )
        s.add(p2)
        s.flush()
        ids["period2_id"] = p2.id
        accs = {
            a.id: a
            for a in s.scalars(
                select(Account).where(
                    Account.ledger_set_id == ids["ledger_set_id"]
                )
            ).all()
        }
        yield {"s": s, "ids": ids, "accs": accs}


def _post(s, ids, period_id, lines, voucher_no, vdate):
    # lines: list[VoucherLine]；post_voucher 内部会转成 PostingLine 去累加投影
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"],
        period_id=period_id,
        voucher_no=voucher_no,
        voucher_date=vdate,
        status="APPROVED",
        summary="投影合同测试",
        created_by=ids["subject_id"],
    )
    v.lines = lines
    s.add(v)
    s.flush()
    return post_voucher(s, voucher_id=v.id, actor=ACTOR)


def _bal(s, account_id, period_id, dims_key=""):
    return s.scalars(
        select(Balance).where(
            Balance.account_id == account_id,
            Balance.period_id == period_id,
            Balance.dims_key == dims_key,
        )
    ).one()


def test_balance_stores_period_activity_not_cumulative(ctx):
    """每个期间的发生额独立成行，绝不跨期合并（契约 1）。"""
    p1 = ctx["ids"]["period_id"]
    p2 = ctx["ids"]["period2_id"]
    exp = ctx["ids"]["expense_account_id"]
    cash = ctx["ids"]["cash_account_id"]

    _post(ctx["s"], ctx["ids"], p1, [
        VoucherLine(line_no=1, account_id=exp, debit=Decimal("100.00"), credit=ZERO),
        VoucherLine(line_no=2, account_id=cash, debit=ZERO, credit=Decimal("100.00")),
    ], "记-0001", date(2026, 8, 27))
    _post(ctx["s"], ctx["ids"], p2, [
        VoucherLine(line_no=1, account_id=exp, debit=Decimal("50.00"), credit=ZERO),
        VoucherLine(line_no=2, account_id=cash, debit=ZERO, credit=Decimal("50.00")),
    ], "记-0002", date(2026, 9, 1))

    b1 = _bal(ctx["s"], exp, p1)
    b2 = _bal(ctx["s"], exp, p2)
    # 每行是该期间发生额
    assert b1.debit_total == Decimal("100.00")
    assert b1.credit_total == ZERO
    # 换期另起一行：P2 是 50，而不是把 P1 的 100 累计进来变成 150
    assert b2.debit_total == Decimal("50.00")
    assert b2.credit_total == ZERO


def test_reconcile_compares_net_not_gross(ctx):
    """账账核对用净额口径：gross 漂移但 net 一致不误报；net 篡改必被检出（契约 2）。"""
    p1 = ctx["ids"]["period_id"]
    exp = ctx["ids"]["expense_account_id"]
    cash = ctx["ids"]["cash_account_id"]

    _post(ctx["s"], ctx["ids"], p1, [
        VoucherLine(line_no=1, account_id=exp, debit=Decimal("100.00"), credit=ZERO),
        VoucherLine(line_no=2, account_id=cash, debit=ZERO, credit=Decimal("100.00")),
    ], "记-0001", date(2026, 8, 27))

    # 干净账：无 PROJECTION_MISMATCH
    r = reconcile_ledger(ctx["s"], ctx["ids"]["ledger_set_id"], 2026, 8)
    assert r["ok"] is True
    assert not any(i["kind"] == "PROJECTION_MISMATCH" for i in r["issues"])

    # 模拟 P1-02 期末结转对净零损益行的清理：给某科目投影加等额借+贷
    # （gross 漂移，但 net 不变）。净额口径下不得误报。
    b_exp = _bal(ctx["s"], exp, p1)
    b_exp.debit_total += Decimal("100.00")
    b_exp.credit_total += Decimal("100.00")
    ctx["s"].flush()
    r2 = reconcile_ledger(ctx["s"], ctx["ids"]["ledger_set_id"], 2026, 8)
    assert not any(
        i["kind"] == "PROJECTION_MISMATCH" for i in r2["issues"]
    ), "净额口径：gross 漂移但 net 一致，不应误报 PROJECTION_MISMATCH"

    # 真正的 net 篡改：只加借不加贷 → net 失配必须被账账核对检出
    b_exp.debit_total += Decimal("100.00")
    ctx["s"].flush()
    r3 = reconcile_ledger(ctx["s"], ctx["ids"]["ledger_set_id"], 2026, 8)
    mm = [i for i in r3["issues"] if i["kind"] == "PROJECTION_MISMATCH"]
    assert mm, "net 失配必须被账账核对检出"
    assert mm[0]["account"] == ctx["accs"][exp].code
    assert mm[0]["from_vouchers_net"] == "100.00"
    assert mm[0]["projection_net"] == "200.00"


def test_period_net_is_ending_balance_basis(ctx):
    """期末余额 = Balance(期间).net：期初是期内 POSTED 凭证，故 Balance 已含期初（契约 3）。"""
    p1 = ctx["ids"]["period_id"]
    exp = ctx["ids"]["expense_account_id"]
    cash = ctx["ids"]["cash_account_id"]

    _post(ctx["s"], ctx["ids"], p1, [
        VoucherLine(line_no=1, account_id=exp, debit=Decimal("100.00"), credit=ZERO),
        VoucherLine(line_no=2, account_id=cash, debit=ZERO, credit=Decimal("100.00")),
    ], "记-0001", date(2026, 8, 27))

    b_exp = _bal(ctx["s"], exp, p1)
    # Balance(期间).net 即该科目此期间的发生额净额（期初结转依赖此不变量）
    assert b_exp.debit_total - b_exp.credit_total == Decimal("100.00")
