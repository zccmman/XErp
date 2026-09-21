"""AR/AP 深化契约测试：对账单 + 账龄（P 阶段，复用 partner_balances 单一真源）。

核心保证（ADR-002）：
- 对账单期末余额 == partner_balances 同客户/同科目余额；
- 账龄未结清余额之和 == partner_balances 同口径余额；
- FIFO 配比正确（回款冲减最早发票）；
- 预付/超额回款 → 余额为负、buckets 全零；
- 两报表均为只读查询，不新增任何凭证/事件。
"""

from __future__ import annotations

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters import clear, ingest_event
from kernel.adapters.partners import partner_balances
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Voucher
from kernel.reporting.arap import aging_analysis, statement_of_account
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    clear()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    return {"s": s, "ids": ids, "actor": {"type": "user", "id": ids["subject_id"]}}


def _ar_invoice(s, ids, actor, *, event_id, customer, total, issued_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="invoice.issued",
        event={
            "event_id": event_id, "invoice_no": event_id,
            "customer": customer, "issued_at": issued_at,
            "net_amount": f"{Decimal(total) - Decimal('10.00'):.2f}",
            "tax_amount": "10.00", "total_amount": total,
        },
        actor=actor,
    )


def _ar_payment(s, ids, actor, *, event_id, customer, amount, received_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="payment.received",
        event={"event_id": event_id, "customer": customer,
               "received_at": received_at, "amount": amount},
        actor=actor,
    )


def _ap_invoice(s, ids, actor, *, event_id, supplier, amount, received_at):
    # 应付采购发票规则注册在 adapter="ocr" / event_type="invoice.received"
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ocr",
        event_type="invoice.received",
        event={"event_id": event_id, "invoice_no": event_id,
               "supplier": supplier, "invoice_date": received_at,
               "expense_category": "办公费",
               "net_amount": amount, "tax_amount": "0.00",
               "total_amount": amount},
        actor=actor,
    )


def _ap_payment(s, ids, actor, *, event_id, supplier, amount, paid_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ap",
        event_type="payment.made",
        event={"event_id": event_id, "supplier": supplier,
               "paid_at": paid_at, "amount": amount},
        actor=actor,
    )


def _pb_receivable(pb, partner):
    for r in pb["receivables"]:
        if r["partner"] == partner:
            return Decimal(r["balance"].replace(",", ""))
    return Decimal("0")


def _pb_payable(pb, partner):
    for r in pb["payables"]:
        if r["partner"] == partner:
            return Decimal(r["balance"].replace(",", ""))
    return Decimal("0")


def test_statement_closing_equals_partner_balance(ctx):
    """对账单期末 == partner_balances 同客户应收余额（单一真源的结构性保证）。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-S1", customer="客户甲",
                total="1010.00", issued_at="2026-08-10")
    s.commit()
    pb = partner_balances(s, ids["ledger_set_id"])
    assert _pb_receivable(pb, "客户甲") == Decimal("1010.00")

    stmt = statement_of_account(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        partner="客户甲", as_of_date=date(2026, 9, 30),
    )
    assert Decimal(stmt["closing_balance"]) == _pb_receivable(pb, "客户甲")
    assert stmt["accounts"] == ["1122"]
    assert len(stmt["lines"]) == 1
    assert stmt["lines"][0]["voucher_no"]
    assert stmt["lines"][0]["balance"] == "1010.00"


def test_statement_from_date_opening(ctx):
    """带 from_date：期初=该日前累计净欠款，期末=期初+期内流水。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-A", customer="客户甲",
                total="1000.00", issued_at="2026-08-05")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-A", customer="客户甲",
                amount="400.00", received_at="2026-08-20")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-B", customer="客户甲",
                total="600.00", issued_at="2026-08-25")
    s.commit()

    # 期初切在 2026-08-15：INV-A(1000) 在期初，PAY-A/INV-B 落在期内
    stmt = statement_of_account(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        partner="客户甲", as_of_date=date(2026, 8, 31),
        from_date=date(2026, 8, 15),
    )
    assert stmt["opening_balance"] == "1000.00"  # 仅 INV-A
    assert len(stmt["lines"]) == 2  # PAY-A + INV-B
    assert stmt["closing_balance"] == "1200.00"  # 1000 - 400 + 600


def test_statement_readonly(ctx):
    """对账单是纯只读查询，不新增任何凭证/事件。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-R", customer="客户甲",
                total="500.00", issued_at="2026-08-01")
    s.commit()
    before = s.scalars(select(Voucher)).all()
    statement_of_account(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        partner="客户甲", as_of_date=date(2026, 9, 1),
    )
    after = s.scalars(select(Voucher)).all()
    assert [v.id for v in before] == [v.id for v in after]


def test_aging_fifo_fully_offset(ctx):
    """开票 1010 + 回款 1010 → 截至日后未结清为 0（FIFO 完整冲减）。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-F", customer="客户甲",
                total="1010.00", issued_at="2026-08-10")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-F", customer="客户甲",
                amount="1010.00", received_at="2026-08-20")
    s.commit()
    age = aging_analysis(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        as_of_date=date(2026, 9, 30),
    )
    item = next(i for i in age["items"] if i["partner"] == "客户甲")
    assert item["outstanding"] == "0.00"
    assert item["balance"] == "0.00"
    assert sum(Decimal(b) for b in item["buckets"].values()) == 0


def test_aging_partial_payment_buckets_by_invoice_date(ctx):
    """开票 2000，回款 500，未结清 1500 仍以原开票日（8-1）入 60-90 桶。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-P", customer="客户甲",
                total="2000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-P", customer="客户甲",
                amount="500.00", received_at="2026-08-20")
    s.commit()
    age = aging_analysis(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        as_of_date=date(2026, 10, 15),  # 开票日 8-1 距今 75 天
    )
    item = next(i for i in age["items"] if i["partner"] == "客户甲")
    assert item["outstanding"] == "1500.00"
    # 75 天落在 60-90 桶（<=90 且 >60）
    assert item["buckets"]["b60_90"] == "1500.00"
    assert item["buckets"]["b0_30"] == "0.00"


def test_aging_outstanding_sum_equals_partner_balance(ctx):
    """账龄未结清之和 == partner_balances 同口径应收余额（单一真源）。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-X", customer="客户乙",
                total="2000.00", issued_at="2026-08-01")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-Y", customer="客户丙",
                total="500.00", issued_at="2026-08-15")
    s.commit()
    pb = partner_balances(s, ids["ledger_set_id"])
    age = aging_analysis(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        as_of_date=date(2026, 9, 30),
    )
    outstanding_sum = sum(Decimal(i["outstanding"]) for i in age["items"])
    partner_sum = (
        _pb_receivable(pb, "客户乙") + _pb_receivable(pb, "客户丙")
    )
    assert outstanding_sum == partner_sum == Decimal("2500.00")


def test_aging_supplier_prepay_negative(ctx):
    """应付方向：无发票直接付款形成预付 → 余额为负、buckets 全零。"""
    s, ids = ctx["s"], ctx["ids"]
    _ap_payment(s, ids, ctx["actor"], event_id="PAY-S", supplier="云服务商",
                amount="3600.00", paid_at="2026-08-15")
    s.commit()
    age = aging_analysis(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="supplier",
        as_of_date=date(2026, 9, 30),
    )
    item = next(i for i in age["items"] if i["partner"] == "云服务商")
    assert item["balance"] == "-3600.00"
    assert item["outstanding"] == "0.00"
    assert all(Decimal(b) == 0 for b in item["buckets"].values())


def test_aging_ap_invoice_buckets(ctx):
    """应付方向：收到供应商发票 5000，未付 → 未结清 5000 入账龄桶。"""
    s, ids = ctx["s"], ctx["ids"]
    _ap_invoice(s, ids, ctx["actor"], event_id="AP-INV", supplier="供应商乙",
                amount="5000.00", received_at="2026-08-01")
    s.commit()
    age = aging_analysis(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="supplier",
        as_of_date=date(2026, 10, 15),  # 75 天 → 60-90
    )
    item = next(i for i in age["items"] if i["partner"] == "供应商乙")
    assert item["outstanding"] == "5000.00"
    assert item["buckets"]["b60_90"] == "5000.00"


def test_aging_bad_dim_rejected(ctx):
    """非法 dim_key 被拒绝而不是静默返回空。"""
    from kernel.reporting.arap import ArapError

    s, ids = ctx["s"], ctx["ids"]
    try:
        aging_analysis(
            s, ledger_set_id=ids["ledger_set_id"], dim_key="department",
            as_of_date=date(2026, 9, 30),
        )
        assert False, "应抛出 ArapError"
    except ArapError as e:
        assert e.code == "BAD_DIM"
