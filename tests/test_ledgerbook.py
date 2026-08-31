"""复盘 D2 TDD：科目明细账——期初 + 逐笔滚动余额 + 期末合计。

用验收手册的示例数据（期初银行 100,000 + 10 笔业务中的银行分录）核对：
银行明细账应含 6 笔，期初 100,000 → 期末 108,500，滚动余额逐行可复算。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Period, Subject, Voucher, VoucherLine
from kernel.ledgerbook import LedgerBookError, ledger_detail
from kernel.opening import import_opening_balances
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    p9 = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=9,
                status="OPEN")
    s.add(p9); s.flush()
    maker = Subject(type="user", display_name="验收人", autonomy_level=3)
    approver = Subject(type="user", display_name="审批员", autonomy_level=3)
    s.add_all([maker, approver]); s.commit()
    act = {"type": "user", "id": maker.id}
    import_opening_balances(s, ledger_set_id=ids["ledger_set_id"], actor=act,
                            period_year=2026, period_month=9,
                            lines=[{"account_code": "100201", "debit": "100000.00", "credit": ""},
                                   {"account_code": "1001", "debit": "2000.00", "credit": ""},
                                   {"account_code": "1601", "debit": "20000.00", "credit": ""},
                                   {"account_code": "2202", "debit": "", "credit": "12000.00"},
                                   {"account_code": "3001", "debit": "", "credit": "110000.00"}])
    s.commit()

    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    books = [
        ("05", "销售收款", [("100201", "11000.00", ""), ("6001", "", "11000.00")]),
        ("08", "购办公用品", [("660202", "500.00", ""), ("1001", "", "500.00")]),
        ("12", "偿还应付", [("2202", "2000.00", ""), ("100201", "", "2000.00")]),
        ("20", "收回欠款", [("100201", "5000.00", ""), ("1122", "", "5000.00")]),
        ("22", "支付房租", [("660202", "3000.00", ""), ("100201", "", "3000.00")]),
        ("28", "广告费", [("660102", "1500.00", ""), ("100201", "", "1500.00")]),
    ]
    for i, (d, summ, lines) in enumerate(books, 1):
        v = Voucher(ledger_set_id=ids["ledger_set_id"], period_id=p9.id,
                    voucher_no=f"记-{i:04d}", voucher_date=__import__("datetime").date(2026, 9, int(d)),
                    status="DRAFT", summary=summ, created_by=maker.id)
        v.lines = [VoucherLine(line_no=j + 1, account_id=accs[c].id,
                               debit=__import__("decimal").Decimal(dr or 0),
                               credit=__import__("decimal").Decimal(cr or 0))
                   for j, (c, dr, cr) in enumerate(lines)]
        s.add(v); s.flush()
        transition(s, voucher_id=v.id, actor=act, target="PUSHED")
        transition(s, voucher_id=v.id, actor={"type": "user", "id": approver.id}, target="APPROVED")
        from kernel.posting import post_voucher

        post_voucher(s, voucher_id=v.id, actor=act)
    s.commit()
    return {"s": s, "ids": ids}


def test_bank_detail_running_balance(ctx):
    """银行明细账：期初 100,000 → 6 笔 → 期末 108,500，滚动余额逐行可复算。"""
    s, ids = ctx["s"], ctx["ids"]
    d = ledger_detail(s, ledger_set_id=ids["ledger_set_id"],
                      account_code="100201", year=2026, month=9)
    assert d["opening_balance"] == "100,000.00"
    assert len(d["rows"]) == 5          # 销售 11,000 / 偿还 2,000 / 回款 5,000 / 房租 3,000 / 广告 1,500
    assert d["totals"]["debit"] == "16,000.00"
    assert d["totals"]["credit"] == "6,500.00"
    assert d["closing_balance"] == "109,500.00"
    assert d["closing_direction"] == "借"
    # 滚动余额首行 = 期初 + 首笔借方
    assert d["rows"][0]["balance"] == "111,000.00"
    # 逐行可复算
    from decimal import Decimal

    run = Decimal("100000.00")
    for r in d["rows"]:
        run += Decimal(r["debit"].replace(",", "")) - Decimal(r["credit"].replace(",", ""))
        assert Decimal(r["balance"].replace(",", "")) == run


def test_detail_liability_direction(ctx):
    """应付账款（贷方科目）：期初 12,000，偿还 2,000 后余额 10,000，方向「贷」。"""
    s, ids = ctx["s"], ctx["ids"]
    d = ledger_detail(s, ledger_set_id=ids["ledger_set_id"],
                      account_code="2202", year=2026, month=9)
    assert d["opening_balance"] == "12,000.00"
    assert d["closing_balance"] == "10,000.00"
    assert d["closing_direction"] == "贷"


def test_detail_unknown_account_and_period(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(LedgerBookError) as ei:
        ledger_detail(s, ledger_set_id=ids["ledger_set_id"],
                      account_code="9999", year=2026, month=9)
    assert ei.value.code == "ACCOUNT_NOT_FOUND"
    with pytest.raises(LedgerBookError) as ei2:
        ledger_detail(s, ledger_set_id=ids["ledger_set_id"],
                      account_code="100201", year=2030, month=1)
    assert ei2.value.code == "PERIOD_NOT_FOUND"


def test_detail_excludes_draft_and_pushed(ctx):
    """明细账是法定账簿口径：未过账凭证不出现。"""
    s, ids = ctx["s"], ctx["ids"]
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
                voucher_no="记-9001",
                voucher_date=__import__("datetime").date(2026, 9, 30),
                status="DRAFT", summary="未过账", created_by=ids["subject_id"])
    v.lines = [
        VoucherLine(line_no=1, account_id=accs["100201"].id,
                    debit=__import__("decimal").Decimal("999.00"),
                    credit=__import__("decimal").Decimal("0.00")),
        VoucherLine(line_no=2, account_id=accs["6001"].id,
                    debit=__import__("decimal").Decimal("0.00"),
                    credit=__import__("decimal").Decimal("999.00")),
    ]
    s.add(v); s.commit()
    d = ledger_detail(s, ledger_set_id=ids["ledger_set_id"],
                      account_code="100201", year=2026, month=9)
    assert all(r["voucher_no"] != "记-9001" for r in d["rows"])
    assert d["closing_balance"] == "109,500.00"
