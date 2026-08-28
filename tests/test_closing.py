"""P1-02 TDD：期末结转引擎。

DoD：损益结转声明式规则 → 关账自动执行 → 可回放（closing.executed 事件 +
利润表改由凭证分录取数，结转后历史利润表不丢）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.closing import close_period
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Event, Period, Subject, Voucher, VoucherLine
from kernel.ledger import verify_chain
from kernel.opening import import_opening_balances
from kernel.posting import post_voucher
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
    reviewer = Subject(type="user", display_name="结转审批人", autonomy_level=3)
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
        transition(
            s, voucher_id=v.id,
            actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED",
        )
        transition(
            s, voucher_id=v.id,
            actor={"type": "user", "id": ids["reviewer"]}, target="APPROVED",
        )
        post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
        return v.id

    # 期初：银行 100,000 = 实收资本 100,000
    import_opening_balances(
        s, ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=[
            {"account_code": "100201", "debit": "100000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "100000.00"},
        ],
    )
    # 业务：收入 10,000；办公费 2,000；差旅 500
    book("记-A001", "2026-08-05", "课程收入", [
        ("100201", "10000.00", ""), ("6001", "", "10000.00")])
    book("记-A002", "2026-08-10", "办公费", [
        ("660202", "2000.00", ""), ("100201", "", "2000.00")])
    book("记-A003", "2026-08-12", "差旅", [
        ("660203", "500.00", ""), ("100201", "", "500.00")])
    s.commit()
    yield {"s": s, "ids": ids, "book": book,
           "actor": {"type": "user", "id": ids["reviewer"]}}
    s.close()


def test_close_period_creates_closing_voucher_and_zeroes_pl(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx["actor"])
    s.commit()
    assert v.status == "POSTED" and v.voucher_no.startswith("结转-202608-")
    # 损益科目投影清零（零行删除）
    bals = s.scalars(select(Balance).where(Balance.period_id == ids["period_id"])).all()
    accs = {a.id: a.code for a in s.scalars(select(Account)).all()}
    pl_left = [accs[b.account_id] for b in bals if accs[b.account_id].startswith(("6", "5"))]
    assert pl_left == []
    # 3103 本年利润 = 净利润 7,500
    profit = [b for b in bals if accs[b.account_id] == "3103"]
    assert profit and profit[0].credit_total == Decimal("7500.00")
    # 事件流含 closing.executed，链完整
    evts = s.scalars(select(Event).where(Event.aggregate_id == v.id)).all()
    assert [e.event_type for e in evts] == ["closing.executed"]
    ok, problem = verify_chain(s, ids["ledger_set_id"])
    assert ok and problem is None


def test_close_period_idempotent(ctx):
    s, ids = ctx["s"], ctx["ids"]
    close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx["actor"])
    s.commit()
    from kernel.posting import PostingError

    with pytest.raises(PostingError) as ei:
        close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx["actor"])
    assert ei.value.code == "ALREADY_CLOSED"


def test_income_statement_preserved_after_closing(ctx):
    """可回放核心：结转后历史利润表不丢（分录取数，排除结转凭证）。"""
    s, ids = ctx["s"], ctx["ids"]
    close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx["actor"])
    s.commit()
    inc = income_statement(s, ids["ledger_set_id"], 2026, 8)
    assert inc["revenue"] == Decimal("10000.00")
    assert inc["net_profit"] == Decimal("7500.00")


def test_balance_sheet_after_closing_no_plug_and_balanced(ctx):
    s, ids = ctx["s"], ctx["ids"]
    close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx["actor"])
    s.commit()
    bs = balance_sheet(s, ids["ledger_set_id"], 2026, 8)
    plug = [i for i in bs["equity"]["items"] if "未结转" in i["group"]]
    assert plug == []                                   # 插值项消失
    assert bs["balanced"] is True                       # 表仍平衡（3103 已含利润）
    assert bs["assets"]["total"] == Decimal("107500.00")  # 银行 107,500（设备无）
    assert bs["equity"]["total"] == Decimal("107500.00")


def test_close_empty_period_rejected(ctx):
    s, ids = ctx["s"], ctx["ids"]
    s.add(Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=9, status="OPEN"))
    s.commit()
    from kernel.posting import PostingError

    with pytest.raises(PostingError) as ei:
        close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=9,
                     actor=ctx["actor"])
    assert ei.value.code == "NOTHING_TO_CLOSE"
