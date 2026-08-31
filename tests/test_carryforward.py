"""P1-06 TDD：期初结转（open_next_period）——月度运行的关键一环。

关账 → 期初结转 → 新期间资产负债表与上月期末一致；未关账拒绝；幂等。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.carryforward import open_next_period
from kernel.closing import close_period
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Subject, Voucher, VoucherLine
from kernel.opening import import_opening_balances
from kernel.posting import PostingError, post_voucher
from kernel.reporting.statements import balance_sheet, income_statement
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer"] = reviewer.id

    def book(no: str, d: str, summary: str, lines):
        accs = {a.code: a for a in s.scalars(select(Account)).all()}
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
            voucher_no=no, voucher_date=date.fromisoformat(d), status="DRAFT",
            summary=summary, created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=i + 1, account_id=accs[code].id,
                        debit=Decimal(dr or "0"), credit=Decimal(cr or "0"))
            for i, (code, dr, cr) in enumerate(lines)
        ]
        s.add(v)
        s.flush()
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED")
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer"]}, target="APPROVED")
        post_voucher(s, voucher_id=v.id,
                     actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
        return v.id

    import_opening_balances(
        s, ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=[
            {"account_code": "100201", "debit": "100000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "100000.00"},
        ],
    )
    book("记-A001", "2026-08-05", "课程收入", [
        ("100201", "10000.00", ""), ("6001", "", "10000.00")])
    book("记-A002", "2026-08-10", "办公费", [
        ("660202", "2000.00", ""), ("100201", "", "2000.00")])
    book("记-A003", "2026-08-12", "差旅", [
        ("660203", "500.00", ""), ("100201", "", "500.00")])
    s.commit()
    yield {"s": s, "ids": ids, "actor": {"type": "user", "id": ids["reviewer"]}}
    s.close()


def test_carryforward_to_next_period(ctx):
    """关账 → 期初结转 → 新期间资产负债表与上月期末一致。"""
    s, ids = ctx["s"], ctx["ids"]
    close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx["actor"])
    s.commit()
    aug = balance_sheet(s, ids["ledger_set_id"], 2026, 8)

    v = open_next_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                         actor=ctx["actor"])
    s.commit()
    assert v.status == "POSTED" and v.voucher_no.startswith("期初-202609-")

    sep = balance_sheet(s, ids["ledger_set_id"], 2026, 9)
    assert sep["assets"]["total"] == aug["assets"]["total"]        # 107,500
    assert sep["equity"]["total"] == aug["equity"]["total"]        # 100,000 + 7,500
    assert sep["balanced"] is True
    # 9 月利润表为空（尚无业务）
    inc = income_statement(s, ids["ledger_set_id"], 2026, 9)
    assert inc["net_profit"] == Decimal("0.00")


def test_carryforward_requires_closing_first(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(PostingError) as ei:
        open_next_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                         actor=ctx["actor"])
    assert ei.value.code == "PERIOD_NOT_CLOSED"


def test_carryforward_idempotent(ctx):
    s, ids = ctx["s"], ctx["ids"]
    close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx["actor"])
    s.commit()
    open_next_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx["actor"])
    s.commit()
    with pytest.raises(PostingError) as ei:
        open_next_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                         actor=ctx["actor"])
    assert ei.value.code == "ALREADY_OPENED"
