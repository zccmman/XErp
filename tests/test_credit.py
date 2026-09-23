"""Phase B 信用管理 + 智能催收契约测试（G2 / G3）。

核心保证（ADR-002 / 设计红线）：
- 授信额度是 Boss 配置（Party.credit_limit），不是账本余额投影；
- 敞口完全由 open_items（凭证 + arap_clearing 重建）派生，绝不复制配平/对账；
- credit_exposure / collections_draft 全部只读，不新增凭证/事件；
- 催收话术仅为草稿，XErp 不代发（HITL）。
"""
from __future__ import annotations

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters import clear, ingest_event
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Party, Period, Voucher, new_id
from kernel.reporting.credit import (
    CreditError, collections_draft, credit_exposure,
    get_credit_limit, set_credit_limit,
)
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


def _ensure_period(s, ls_id, yr, mo):
    existing = s.scalars(
        select(Period).where(
            Period.ledger_set_id == ls_id, Period.year == yr, Period.month == mo
        )
    ).first()
    if existing is None:
        s.add(Period(ledger_set_id=ls_id, year=yr, month=mo, status="OPEN"))


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


def test_exposure_no_limit_no_breach(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-1", customer="客户甲",
                total="1000.00", issued_at="2026-08-10")
    s.commit()
    exp = credit_exposure(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer")
    row = next(r for r in exp["rows"] if r["partner"] == "客户甲")
    assert row["exposure"] == "1000.00"
    assert row["credit_limit"] == "0.00"
    assert row["breach"] is False
    assert row["near_limit"] is False
    assert exp["breaches"] == []


def test_exposure_breach(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-2", customer="客户甲",
                total="1000.00", issued_at="2026-08-10")
    s.commit()
    set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                     partner="客户甲", limit="800.00", actor=ctx["actor"])
    s.commit()
    exp = credit_exposure(s, ledger_set_id=ids["ledger_set_id"])
    row = next(r for r in exp["rows"] if r["partner"] == "客户甲")
    assert row["exposure"] == "1000.00"
    assert row["credit_limit"] == "800.00"
    assert row["breach"] is True
    assert row["near_limit"] is True  # 1000/800 = 125% ≥ 80%
    assert exp["breaches"][0]["over_by"] == "200.00"


def test_exposure_near_limit_not_breach(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-3", customer="客户甲",
                total="900.00", issued_at="2026-08-10")
    s.commit()
    set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                     partner="客户甲", limit="1000.00", actor=ctx["actor"])
    s.commit()
    exp = credit_exposure(s, ledger_set_id=ids["ledger_set_id"])
    row = next(r for r in exp["rows"] if r["partner"] == "客户甲")
    assert row["exposure"] == "900.00"
    assert row["credit_limit"] == "1000.00"
    assert row["breach"] is False
    assert row["near_limit"] is True
    assert row["utilization"] == "90.0%"
    assert exp["breaches"] == []


def _ar_lines(s, ids, customer):
    """取该客户未清发票行(借)与回款行(贷)的 id（AR 科目 1122，方向 debit）。"""
    from kernel.db.models import Voucher, VoucherLine
    from sqlalchemy import select

    rows = s.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(Voucher.ledger_set_id == ids["ledger_set_id"])
    ).all()
    inv_id = pay_id = None
    for ln, v in rows:
        dims = ln.aux_dims or {}
        if dims.get("customer") != customer:
            continue
        if ln.debit and ln.debit > 0:
            inv_id = ln.id
        elif ln.credit and ln.credit > 0:
            pay_id = ln.id
    return inv_id, pay_id


def test_exposure_after_partial_payment(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-4", customer="客户甲",
                total="1000.00", issued_at="2026-08-10")
    _ar_payment(s, ids, ctx["actor"], event_id="PAY-4", customer="客户甲",
                amount="400.00", received_at="2026-08-20")
    s.commit()
    # 回款须经核销(Phase A)才减少未清敞口——这是 XErp 正确语义：
    # 未核销的收款仍是独立流水，发票仍全额未清。
    inv_id, pay_id = _ar_lines(s, ids, "客户甲")
    from kernel.reporting.arap import record_clearing

    record_clearing(
        s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
        partner="客户甲",
        assignments=[{"invoice_line_id": inv_id, "payment_line_id": pay_id,
                      "amount": "400.00"}], actor=ctx["actor"],
    )
    s.commit()
    set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                     partner="客户甲", limit="800.00", actor=ctx["actor"])
    s.commit()
    exp = credit_exposure(s, ledger_set_id=ids["ledger_set_id"])
    row = next(r for r in exp["rows"] if r["partner"] == "客户甲")
    assert row["exposure"] == "600.00"  # 1000 − 已核销 400
    assert row["breach"] is False
    assert exp["breaches"] == []


def test_exposure_aggregates_multiple_invoices(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-5", customer="客户甲",
                total="600.00", issued_at="2026-08-10")
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-6", customer="客户甲",
                total="500.00", issued_at="2026-08-12")
    s.commit()
    set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                     partner="客户甲", limit="1000.00", actor=ctx["actor"])
    s.commit()
    exp = credit_exposure(s, ledger_set_id=ids["ledger_set_id"])
    row = next(r for r in exp["rows"] if r["partner"] == "客户甲")
    assert row["exposure"] == "1100.00"
    assert row["breach"] is True
    assert exp["breaches"][0]["over_by"] == "100.00"


def test_set_credit_limit_creates_party(ctx):
    s, ids = ctx["s"], ctx["ids"]
    res = set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                           partner="客户甲", limit="500.00", actor=ctx["actor"])
    s.commit()
    assert res["credit_limit"] == "500.00"
    party = s.scalars(
        select(Party).where(Party.ledger_set_id == ids["ledger_set_id"],
                            Party.party_type == "customer", Party.name == "客户甲")
    ).first()
    assert party is not None
    assert Decimal(str(party.credit_limit)) == Decimal("500.00")
    assert get_credit_limit(s, ledger_set_id=ids["ledger_set_id"],
                            dim_key="customer", partner="客户甲") == Decimal("500.00")


def test_set_credit_limit_rejects_negative(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(CreditError):
        set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                         partner="客户甲", limit="-100.00", actor=ctx["actor"])


def test_exposure_is_readonly(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-7", customer="客户甲",
                total="1000.00", issued_at="2026-08-10")
    s.commit()
    before = s.scalars(select(Voucher)).all()
    credit_exposure(s, ledger_set_id=ids["ledger_set_id"])
    after = s.scalars(select(Voucher)).all()
    assert len(before) == len(after)


def test_collections_draft_levels(ctx):
    s, ids = ctx["s"], ctx["ids"]
    for (mo, day, eid) in ((8, 25, "INV-A"), (7, 20, "INV-B"), (6, 15, "INV-C")):
        _ensure_period(s, ids["ledger_set_id"], 2026, mo)
        _ar_invoice(s, ids, ctx["actor"], event_id=eid, customer="客户甲",
                    total="1000.00", issued_at=f"2026-{mo:02d}-{day:02d}")
    s.commit()
    draft = collections_draft(s, ledger_set_id=ids["ledger_set_id"],
                              as_of_date=date(2026, 9, 30))
    rows = [r for r in draft["rows"] if r["partner"] == "客户甲"]
    assert len(rows) == 1  # 同一客户所有逾期发票合并为一行
    r = rows[0]
    assert r["overdue_amount"] == "3000.00"
    assert r["oldest_days"] == 107  # 6-15 → 9-30
    # 升级级别取组内最老账龄（107 天 → L3），而非逐发票分别计级别
    assert r["level"] == "L3"
    assert len(r["items"]) == 3  # 三张发票合并为一个客户催收行
    assert "严重逾期" in r["draft_message"]
    # 单客户最老账龄决定整体级别：counts 仅 L3=1
    assert draft["counts"] == {"L1": 0, "L2": 0, "L3": 1}
    assert draft["totals"]["customers"] == 1


def test_collections_draft_current_not_overdue(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ensure_period(s, ids["ledger_set_id"], 2026, 9)
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-D", customer="客户甲",
                total="1000.00", issued_at="2026-09-25")  # 距 9-30 仅 5 天
    s.commit()
    draft = collections_draft(s, ledger_set_id=ids["ledger_set_id"],
                              as_of_date=date(2026, 9, 30))
    assert not any(r["partner"] == "客户甲" for r in draft["rows"])


def test_collections_draft_readonly(ctx):
    s, ids = ctx["s"], ctx["ids"]
    _ensure_period(s, ids["ledger_set_id"], 2026, 7)
    _ar_invoice(s, ids, ctx["actor"], event_id="INV-E", customer="客户甲",
                total="1000.00", issued_at="2026-07-01")
    s.commit()
    before = s.scalars(select(Voucher)).all()
    collections_draft(s, ledger_set_id=ids["ledger_set_id"], as_of_date=date(2026, 9, 30))
    after = s.scalars(select(Voucher)).all()
    assert len(before) == len(after)
