"""Phase C AI 收款自动匹配（G4）：propose_receipt_match / unmatched_receipts 契约测试。

核心保证（ADR-002 + HITL）：
- propose_receipt_match 全程只读，不改账、不落 arap_clearing；
- 多信号匹配（备注发票号 / 金额精确 / 名称模糊 / 部分-多付 / FIFO 兜底）均带可解释
  confidence 与 rationale；
- 匹配结果可直接喂 record_clearing（HITL）落库，XErp 不自动核销；
- unmatched_receipts 与 open_items 同口径，只读派生待匹配回款清单；
- 终态动作（核销）必须由 Boss 确认，推送 ≠ 执行。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SessionForTest

from kernel.adapters import clear, ingest_event
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, ArapClearing, Voucher, VoucherLine
from kernel.reporting.arap import (
    ArapError,
    open_items,
    propose_receipt_match,
    record_clearing,
    unmatched_receipts,
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


def _ar_line_ids(s, lse, customer):
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


AS_OF = date(2026, 12, 31)


# —— 1) 已入账回款行 + 金额精确匹配 ——
def test_payment_line_id_exact_match(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-E1", customer="客户甲",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-E1", customer="客户甲",
                amount="1000.00", received_at="2026-08-20")
    s.commit()
    _, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户甲")
    rep = propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"],
                                payment_line_id=pay[0], as_of_date=AS_OF)
    assert rep["partner"] == "客户甲"
    assert len(rep["proposals"]) == 1
    p = rep["proposals"][0]
    assert p["confidence"] == 0.95
    assert "exact_amount" in p["signals"]
    assert p["amount"] == "1000.00"
    assert rep["matched_amount"] == "1000.00"
    assert rep["unmatched_amount"] == "0.00"
    assert rep["overpayment"] is False


# —— 2) 备注发票号命中（最高置信）——
def test_receipt_reference_parse(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-2026-001", customer="客户乙",
                total="1000.00", issued_at="2026-08-01")
    s.commit()
    rep = propose_receipt_match(
        s, ledger_set_id=ids["ledger_set_id"],
        receipt={"amount": "1000.00", "date": "2026-08-25",
                 "reference": "汇款 INV-2026-001 谢谢", "payer": None},
        as_of_date=AS_OF,
    )
    assert rep["needs_recording"] is True  # 自由文本收款需先入账
    assert len(rep["proposals"]) == 1
    p = rep["proposals"][0]
    assert p["confidence"] == 0.99
    assert "reference_exact" in p["signals"]
    assert p["amount"] == "1000.00"


# —— 3) 多张发票合计精确匹配 ——
def test_multi_invoice_sum_match(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-S1", customer="客户丙",
                total="600.00", issued_at="2026-08-01")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-S2", customer="客户丙",
                total="400.00", issued_at="2026-08-02")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-S1", customer="客户丙",
                amount="1000.00", received_at="2026-08-20")
    s.commit()
    _, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户丙")
    rep = propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"],
                                payment_line_id=pay[0], as_of_date=AS_OF)
    assert len(rep["proposals"]) == 2
    assert all("exact_sum" in p["signals"] for p in rep["proposals"])
    assert rep["matched_amount"] == "1000.00"
    assert rep["confidence_overall"] == 0.9


# —— 4) 部分核销（回款 < 发票额），FIFO 兜底 ——
def test_partial_payment_fifo(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-P1", customer="客户丁",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-P1", customer="客户丁",
                amount="300.00", received_at="2026-08-20")
    s.commit()
    _, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户丁")
    rep = propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"],
                                payment_line_id=pay[0], as_of_date=AS_OF)
    assert len(rep["proposals"]) == 1
    assert rep["proposals"][0]["amount"] == "300.00"
    assert rep["proposals"][0]["confidence"] == 0.6
    assert "fifo" in rep["proposals"][0]["signals"]
    assert rep["matched_amount"] == "300.00"
    assert rep["unmatched_amount"] == "0.00"  # 回款已全部分配
    assert rep["overpayment"] is False


# —— 5) 多付预警（回款 > 全部未清合计）——
def test_overpayment_flag(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-O1", customer="客户戊",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-O1", customer="客户戊",
                amount="1500.00", received_at="2026-08-20")
    s.commit()
    _, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户戊")
    rep = propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"],
                                payment_line_id=pay[0], as_of_date=AS_OF)
    assert rep["matched_amount"] == "1000.00"
    assert rep["unmatched_amount"] == "500.00"
    assert rep["overpayment"] is True
    assert "overpay_risk" in rep["proposals"][0]["signals"]


# —— 6) 付款方名称模糊匹配收敛候选客户 ——
def test_fuzzy_payer_resolves_partner(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-F1",
                customer="北京示例科技有限公司", total="800.00",
                issued_at="2026-08-01")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-F2",
                customer="上海测试贸易有限公司", total="800.00",
                issued_at="2026-08-02")
    s.commit()
    bj_inv, _ = _ar_line_ids(s, ids["ledger_set_id"], "北京示例科技有限公司")
    rep = propose_receipt_match(
        s, ledger_set_id=ids["ledger_set_id"],
        receipt={"amount": "800.00", "date": "2026-08-25",
                 "reference": None, "payer": "示例科技"},
        as_of_date=AS_OF,
    )
    assert rep["partner"] == "北京示例科技有限公司"
    assert len(rep["proposals"]) == 1
    assert rep["proposals"][0]["invoice_line_id"] == bj_inv[0]


# —— 7) 自由文本收款未入账：needs_recording + 无 payment_line_id ——
def test_free_text_receipt_needs_recording(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-R1", customer="客户己",
                total="500.00", issued_at="2026-08-01")
    s.commit()
    rep = propose_receipt_match(
        s, ledger_set_id=ids["ledger_set_id"],
        receipt={"amount": "500.00", "date": "2026-08-25",
                 "reference": None, "payer": "客户己"},
        as_of_date=AS_OF,
    )
    assert rep["needs_recording"] is True
    assert rep["proposals"][0]["payment_line_id"] is None


# —— 8) 无未清发票：返回空匹配 + 全额未匹配 ——
def test_receipt_no_open_invoices(ctx):
    s, ids = ctx["s"], ctx["ids"]
    rep = propose_receipt_match(
        s, ledger_set_id=ids["ledger_set_id"],
        receipt={"amount": "999.00", "date": "2026-08-25",
                 "reference": None, "payer": "不存在的客户"},
        as_of_date=AS_OF,
    )
    assert rep["proposals"] == []
    assert rep["unmatched_amount"] == "999.00"
    assert rep["overpayment"] is False


# —— 9) 缺参报错 ——
def test_need_receipt_error(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(ArapError) as ex:
        propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"], as_of_date=AS_OF)
    assert ex.value.code == "NEED_RECEIPT"


# —— 10) unmatched_receipts 只读派生待匹配回款 ——
def test_unmatched_receipts_count(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-U1", customer="客户庚",
                total="2000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-U1", customer="客户庚",
                amount="500.00", received_at="2026-08-20")
    s.commit()
    rec = unmatched_receipts(s, ledger_set_id=ids["ledger_set_id"],
                            dim_key="customer", as_of_date=AS_OF)
    assert rec["totals"]["count"] == 1
    assert rec["totals"]["remaining"] == "500.00"
    assert rec["items"][0]["partner"] == "客户庚"


# —— 11) 只读不变量：propose 不新增 arap_clearing ——
def test_propose_is_readonly(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-RD", customer="客户辛",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-RD", customer="客户辛",
                amount="1000.00", received_at="2026-08-20")
    s.commit()
    before = s.scalars(
        select(ArapClearing).where(ArapClearing.ledger_set_id == ids["ledger_set_id"])
    ).first()
    assert before is None
    _, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户辛")
    propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"],
                          payment_line_id=pay[0], as_of_date=AS_OF)
    after = s.scalars(
        select(ArapClearing).where(ArapClearing.ledger_set_id == ids["ledger_set_id"])
    ).first()
    assert after is None, "propose_receipt_match 必须只读、不得落 arap_clearing"


# —— 12) 闭环验证：propose → 人工确认 → record_clearing 落库（HITL）——
def test_propose_then_apply_clearing(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-C1", customer="客户壬",
                total="1000.00", issued_at="2026-08-01")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-C1", customer="客户壬",
                amount="1000.00", received_at="2026-08-20")
    s.commit()
    inv, pay = _ar_line_ids(s, ids["ledger_set_id"], "客户壬")
    rep = propose_receipt_match(s, ledger_set_id=ids["ledger_set_id"],
                                payment_line_id=pay[0], as_of_date=AS_OF)
    p = rep["proposals"][0]
    rows = record_clearing(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        partner="客户壬",
        assignments=[{"invoice_line_id": p["invoice_line_id"],
                     "payment_line_id": p["payment_line_id"],
                     "amount": p["amount"]}],
        source="ai_proposed", actor=ctx["actor"],
    )
    assert len(rows) == 1
    # 确认后未清项归零
    oi = open_items(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                    partner="客户壬", as_of_date=AS_OF)
    assert oi["totals"]["balance"] == "0.00"
