"""期末结转预览（P1 剩余项）：把"点下去会发生什么"在结账前摊开给人看。

最核心的一条契约：**预览看到的净利润 == 真执行后的净利润**。
这不是附会——close_period 与 preview_closing 共用 _collect_pl_rows /
_build_closing_lines 两个 helper，本测试用真实账套把这条一致性钉死，
一旦将来有人只改了一侧，这里立刻红。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.closing import close_period, preview_closing
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Period, Subject, Voucher, VoucherLine
from kernel.opening import import_opening_balances
from kernel.posting import PostingError, post_voucher
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
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED")
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer"]}, target="APPROVED")
        post_voucher(s, voucher_id=v.id,
                     actor={"type": "user", "id": ids["subject_id"]})
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
    # 业务：收入 10,000；办公费 2,000；差旅 500 → 净利润 7,500
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


def test_preview_shows_pl_lines_and_net_profit(ctx):
    """预览摊开损益明细：收入结出、费用结出、净利润进 3103。"""
    s, ids = ctx["s"], ctx["ids"]
    p = preview_closing(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8)

    assert p["already_closed"] is False
    assert p["nothing_to_close"] is False
    assert p["profit_account"] == "3103"
    assert p["will_generate"] == "结转-202608-001"
    assert p["net_profit"] == "7500.00"
    assert p["total_income"] == "10000.00"
    assert p["total_expense"] == "2500.00"

    codes = [ln["account_code"] for ln in p["lines"]]
    assert codes == ["6001", "660202", "660203", "3103"]
    # 收入借方结出、费用贷方结出
    by_code = {ln["account_code"]: ln for ln in p["lines"]}
    assert by_code["6001"]["direction"] == "借"
    assert by_code["6001"]["amount"] == "10000.00"
    assert by_code["660202"]["direction"] == "贷"
    assert by_code["660202"]["amount"] == "2000.00"
    assert by_code["3103"]["side"] == "profit"
    assert by_code["3103"]["amount"] == "7500.00"
    assert "结转-202608-001" in p["hint"]


def test_preview_matches_real_execution(ctx):
    """铁律：预览的净利润 == 真执行后 3103 上的金额。"""
    s, ids = ctx["s"], ctx["ids"]
    p = preview_closing(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8)
    v = close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx["actor"])
    s.commit()

    accs = {a.id: a.code for a in s.scalars(select(Account)).all()}
    profit_line = [ln for ln in v.lines if accs[ln.account_id] == "3103"][0]
    assert str(profit_line.credit) == p["net_profit"]
    # 行数一致（预览 4 行：3 个损益科目 + 本年利润）
    assert len(v.lines) == len(p["lines"])


def test_preview_after_closed_is_replay(ctx):
    """已结转后预览变成历史回放，并给出已结转凭证号。"""
    s, ids = ctx["s"], ctx["ids"]
    v = close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx["actor"])
    s.commit()
    p = preview_closing(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8)
    assert p["already_closed"] is True
    assert p["closing_voucher_no"] == v.voucher_no
    assert p["will_generate"] is None
    assert "已执行过期末结转" in p["hint"]
    # 回放必须来自结转凭证本身：结转后损益投影已清零，若拿余额重算会得 0
    assert p["net_profit"] == "7500.00"
    assert [ln["account_code"] for ln in p["lines"]] == [
        "6001", "660202", "660203", "3103"]


def test_preview_nothing_to_close(ctx):
    """本期无损益发生额：明确告知无需结转，而不是给一张空表。"""
    s, ids = ctx["s"], ctx["ids"]
    # 新期间（无业务）
    s.add(Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=9,
                 status="OPEN"))
    s.commit()
    p = preview_closing(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=9)
    assert p["nothing_to_close"] is True
    assert p["will_generate"] is None
    assert p["net_profit"] == "0"
    assert "无需结转" in p["hint"]


def test_preview_period_not_found(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(PostingError) as ei:
        preview_closing(s, ledger_set_id=ids["ledger_set_id"], year=2099, month=1)
    assert ei.value.code == "PERIOD_NOT_FOUND"
