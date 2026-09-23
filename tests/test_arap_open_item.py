"""Phase A 未清项核销（G1）：open_items / record_clearing / propose_clearing 契约测试。

核心保证（ADR-002）：
- 核销是新增不可变记录，不改写凭证/余额投影；未清项 = 发票额 − 已核销，可重建；
- open_items / propose_clearing 只读不改账；record_clearing 仅新增 arap_clearing 行；
- 启用核销后账龄改用未清项口径（修正 FIFO 漂移）；无核销记录时回退 FIFO（向后兼容）；
- 超额核销 / 超额使用回款 被拒绝；未清总额 == partner_balances 同口径净额。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

from kernel.adapters import clear, ingest_event
from kernel.adapters.partners import partner_balances
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, ArapClearing, Voucher, VoucherLine
from kernel.reporting.arap import (
    ArapError,
    aging_analysis,
    open_items,
    propose_clearing,
    record_clearing,
)
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    clear()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = SessionForTest(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    return {"s": s, "ids": ids, "actor": {"type": "user", "id": ids["subject_id"]}}


# 复用 test_arap 的构造器（保持事件口径一致）
def _ar_invoice(s, ids, actor, *, event_id, customer, total, issued_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar", event_type="invoice.issued",
        event={"event_id": event_id, "invoice_no": event_id, "customer": customer,
               "issued_at": issued_at,
               "net_amount": f"{Decimal(total) - Decimal('10.00'):.2f}",
               "tax_amount": "10.00", "total_amount": total},
        actor=actor,
    )


def _ar_payment(s, ids, actor, *, event_id, customer, amount, received_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar", event_type="payment.received",
        event={"event_id": event_id, "customer": customer, "received_at": received_at,
               "amount": amount}, actor=actor,
    )


def _ap_invoice(s, ids, actor, *, event_id, supplier, amount, received_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ocr", event_type="invoice.received",
        event={"event_id": event_id, "invoice_no": event_id, "supplier": supplier,
               "invoice_date": received_at, "expense_category": "办公费",
               "net_amount": amount, "tax_amount": "0.00", "total_amount": amount},
        actor=actor,
    )


def _ap_payment(s, ids, actor, *, event_id, supplier, amount, paid_at):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ap", event_type="payment.made",
        event={"event_id": event_id, "supplier": supplier, "paid_at": paid_at,
               "amount": amount}, actor=actor,
    )


def _ar_line_ids(s, lse, customer):
    """返回 (invoice_line_ids, payment_line_ids)：按科目方向判定发票侧/回款侧。"""
    acc = s.scalars(select(Account).where(
        Account.ledger_set_id == lse, Account.code == "1122")).first()
    rows = s.execute(select(VoucherLine, Voucher)
                     .join(Voucher, VoucherLine.voucher_id == Voucher.id)
                     .where(VoucherLine.account_id == acc.id,
                            Voucher.status.in_(("PUSHED", "APPROVED", "POSTED")))).all()
    inv, pay = [], []
    for l, v in rows:
        dims = l.aux_dims or {}
        if dims.get("customer") != customer:
            continue
        if l.debit > 0:
            inv.append(l.id)
        else:
            pay.append(l.id)
    return inv, pay


def _ap_line_ids(s, lse, supplier):
    acc = s.scalars(select(Account).where(
        Account.ledger_set_id == lse, Account.code == "2202")).first()
    rows = s.execute(select(VoucherLine, Voucher)
                     .join(Voucher, VoucherLine.voucher_id == Voucher.id)
                     .where(VoucherLine.account_id == acc.id,
                            Voucher.status.in_(("PUSHED", "APPROVED", "POSTED")))).all()
    inv, pay = [], []
    for l, v in rows:
        dims = l.aux_dims or {}
        if dims.get("supplier") != supplier:
            continue
        if l.credit > 0:
            inv.append(l.id)
        else:
            pay.append(l.id)
    return inv, pay


from sqlalchemy.orm import Session as SessionForTest  # noqa: E402


def test_open_items_partial_clearing(ctx):
    """开票 2000 + 回款 500 核销 → 未清 1500，账龄未清==1500。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-1", customer="客户甲",
                total="2000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-1", customer="客户甲",
                amount="500.00", received_at="2026-08-20")
    s.commit()
    inv, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户甲")
    record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户甲", assignments=[{"invoice_line_id": inv[0],
                    "payment_line_id": pay[0], "amount": "500.00"}], source="manual",
                    actor=ctx["actor"])
    s.commit()

    oi = open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer", partner="客户甲",
                    as_of_date=date(2026, 10, 15))
    item = next(i for i in oi["items"] if i["partner"] == "客户甲")
    assert item["open_amount"] == "1500.00"
    assert item["cleared_amount"] == "500.00"
    assert item["original_amount"] == "2000.00"

    age = aging_analysis(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                         as_of_date=date(2026, 10, 15))
    aitem = next(i for i in age["items"] if i["partner"] == "客户甲")
    assert aitem["outstanding"] == "1500.00"


def test_open_items_outstanding_equals_partner_balances(ctx):
    """未清总额 == partner_balances 同口径应收净额（单一真源）。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-2", customer="客户乙",
                total="3000.00", issued_at="2026-08-05")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-2", customer="客户乙",
                amount="1000.00", received_at="2026-08-20")
    s.commit()
    inv, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户乙")
    record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户乙", assignments=[{"invoice_line_id": inv[0],
                    "payment_line_id": pay[0], "amount": "1000.00"}], source="manual",
                    actor=ctx["actor"])
    s.commit()

    pb = partner_balances(s, ids["ledger_set_id"])
    pb_recv = next(r["balance"] for r in pb["receivables"] if r["partner"] == "客户乙")
    pb_recv = Decimal(pb_recv.replace(",", ""))

    oi = open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    as_of_date=date(2026, 9, 30))
    oi_total = Decimal(oi["totals"]["outstanding"].replace(",", ""))
    assert oi_total == pb_recv == Decimal("2000.00")


def test_record_clearing_residual(ctx):
    """开票 1000 + 回款 1500，核销 1000（全额发票）→ 未清 0；溢付反映在 partner_balances。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-3", customer="客户丙",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-3", customer="客户丙",
                amount="1500.00", received_at="2026-08-20")
    s.commit()
    inv, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户丙")
    record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户丙", assignments=[{"invoice_line_id": inv[0],
                    "payment_line_id": pay[0], "amount": "1000.00"}], source="manual",
                    actor=ctx["actor"])
    s.commit()
    oi = open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户丙", as_of_date=date(2026, 9, 30))
    assert oi["totals"]["outstanding"] == "0.00"
    # 溢付 500 在 partner_balances 体现为负应收（预收）
    pb = partner_balances(s, ids["ledger_set_id"])
    pb_recv = Decimal(next(r["balance"] for r in pb["receivables"]
                           if r["partner"] == "客户丙").replace(",", ""))
    assert pb_recv == Decimal("-500.00")


def test_record_clearing_over_clear_invoice(ctx):
    """超额核销发票被拒绝。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-4", customer="客户丁",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-4", customer="客户丁",
                amount="1500.00", received_at="2026-08-20")
    s.commit()
    inv, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户丁")
    with pytest.raises(ArapError) as e:
        record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                        partner="客户丁", assignments=[{"invoice_line_id": inv[0],
                        "payment_line_id": pay[0], "amount": "1500.00"}],
                        actor=ctx["actor"])
    assert e.value.code == "OVER_CLEAR_INVOICE"


def test_record_clearing_over_apply_payment(ctx):
    """同一回款跨多张发票超额使用被拒绝。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-5", customer="客户戊",
                total="1000.00", issued_at="2026-08-01")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-6", customer="客户戊",
                total="1000.00", issued_at="2026-08-02")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-5", customer="客户戊",
                amount="1500.00", received_at="2026-08-20")
    s.commit()
    inv, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户戊")
    # 先用 1000 清 INV-5，再企图用同一笔 1500 的回款再清 1000（剩余仅 500）→ 拒绝
    with pytest.raises(ArapError) as e:
        record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                        partner="客户戊", assignments=[
                            {"invoice_line_id": inv[0], "payment_line_id": pay[0], "amount": "1000.00"},
                            {"invoice_line_id": inv[1], "payment_line_id": pay[0], "amount": "1000.00"}],
                        actor=ctx["actor"])
    assert e.value.code == "OVER_APPLY_PAYMENT"


def test_propose_then_apply_closes_correctly(ctx):
    """propose（只读）给出 FIFO 兜底匹配；apply 后未清收敛到 1300。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-7", customer="客户己",
                total="2000.00", issued_at="2026-08-01")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-8", customer="客户己",
                total="1500.00", issued_at="2026-08-15")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-6", customer="客户己",
                amount="1000.00", received_at="2026-08-21")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-7", customer="客户己",
                amount="1200.00", received_at="2026-08-22")
    s.commit()
    prop = propose_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                           partner="客户己")
    assert len(prop["proposals"]) > 0
    total_proposed = sum(Decimal(p["amount"]) for p in prop["proposals"])
    assert total_proposed == Decimal("2200.00")  # min(3500 发票, 2200 回款)
    # propose 不改账
    assert s.scalars(select(ArapClearing)).all() == []

    record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户己", assignments=prop["proposals"], source="ai_proposed",
                    actor=ctx["actor"])
    s.commit()
    oi = open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户己", as_of_date=date(2026, 9, 30))
    assert oi["totals"]["outstanding"] == "1300.00"  # 3500 - 2200


def test_open_items_and_propose_readonly(ctx):
    """open_items 与 propose_clearing 只读，不新增任何凭证或核销记录。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-9", customer="客户庚",
                total="800.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-8", customer="客户庚",
                amount="300.00", received_at="2026-08-20")
    s.commit()
    before_clearing = len(s.scalars(select(ArapClearing)).all())
    open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer", partner="客户庚")
    propose_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer", partner="客户庚")
    after_clearing = len(s.scalars(select(ArapClearing)).all())
    assert before_clearing == after_clearing == 0


def test_aging_falls_back_to_fifo_when_no_clearing(ctx):
    """无核销记录时账龄仍用 FIFO（向后兼容，历史数据不受 Phase A 影响）。"""
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-10", customer="客户辛",
                total="2000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-9", customer="客户辛",
                amount="500.00", received_at="2026-08-20")
    s.commit()
    # 不做任何核销
    age = aging_analysis(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                         as_of_date=date(2026, 10, 15))
    aitem = next(i for i in age["items"] if i["partner"] == "客户辛")
    assert aitem["outstanding"] == "1500.00"  # FIFO 仍把回款抵减


def test_ap_open_items_and_clearing(ctx):
    """应付方向：发票 5000 + 付款 2000 核销 → 未清 3000。"""
    s, ids = ctx["s"], ctx["ids"]
    _ap_invoice(s, ids, ctx["actor"], event_id="AP-1", supplier="供应商甲",
                amount="5000.00", received_at="2026-08-01")
    _ap_payment(s, ids, ctx["actor"], event_id="AP-P1", supplier="供应商甲",
                amount="2000.00", paid_at="2026-08-20")
    s.commit()
    inv, pay = _ap_line_ids(s, ids["ledger_set_id"], "供应商甲")
    record_clearing(s, ledger_set_id=ids["ledger_set_id"], dim_key="supplier",
                    partner="供应商甲", assignments=[{"invoice_line_id": inv[0],
                    "payment_line_id": pay[0], "amount": "2000.00"}], source="manual",
                    actor=ctx["actor"])
    s.commit()
    oi = open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="supplier",
                    partner="供应商甲", as_of_date=date(2026, 10, 15))
    item = next(i for i in oi["items"] if i["partner"] == "供应商甲")
    assert item["open_amount"] == "3000.00"
