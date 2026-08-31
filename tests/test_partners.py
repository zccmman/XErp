"""P2-02 TDD：应收/应付往来适配器——分录自动挂客户/供应商维度 + 往来余额表。

覆盖：partner 维度挂账、科目维度声明校验、维度缺失拒绝、往来余额查询
（谁欠我/我欠谁）、未挂维度余额单列、幂等与预览兼容。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters import (
    RuleError,
    clear,
    ingest_event,
    preview,
    register,
)
from kernel.adapters.engine import AdapterError
from kernel.adapters.partners import partner_balances
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Subject, Voucher
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    clear()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    return {"s": s, "ids": ids, "actor": {"type": "user", "id": ids["subject_id"]},
            "reviewer": {"type": "user", "id": reviewer.id}}


def _invoice(s, ids, actor, **kw):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="invoice.issued",
        event={
            "event_id": kw.get("event_id", "INV-P2"),
            "invoice_no": kw.get("invoice_no", "INV-P2"),
            "customer": kw.get("customer", "客户甲"),
            "issued_at": kw.get("issued_at", "2026-08-10"),
            "net_amount": kw.get("net_amount", "1000.00"),
            "tax_amount": kw.get("tax_amount", "10.00"),
            "total_amount": kw.get("total_amount", "1010.00"),
        },
        actor=actor,
    )


def test_invoice_attaches_customer_dimension(ctx):
    """开票事件的应收分录自动挂 customer 维度，收入/税金分录不挂。"""
    s, ids = ctx["s"], ctx["ids"]
    res = _invoice(s, ids, ctx["actor"])
    s.commit()
    v = s.get(Voucher, res["voucher"]["id"])
    by_line = {ln.line_no: ln for ln in v.lines}
    assert by_line[1].aux_dims == {"customer": "客户甲"}
    assert by_line[2].aux_dims is None
    assert by_line[3].aux_dims is None


def test_payment_received_offset_same_customer(ctx):
    """回款冲减同一客户的应收：开票 1010 + 回款 1010 → 该客户余额归零。"""
    s, ids = ctx["s"], ctx["ids"]
    _invoice(s, ids, ctx["actor"], event_id="INV-A", invoice_no="INV-A")
    s.commit()
    ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="payment.received",
        event={"event_id": "PAY-A", "customer": "客户甲",
               "received_at": "2026-08-20", "amount": "1010.00"},
        actor=ctx["actor"],
    )
    s.commit()
    report = partner_balances(s, ids["ledger_set_id"])
    assert report["receivables"] == []
    assert report["untracked_total"] == "0.00"


def test_partner_balances_shows_who_owes(ctx):
    """两个客户分别开票 → 往来余额按客户列出，谁欠多少一目了然。"""
    s, ids = ctx["s"], ctx["ids"]
    _invoice(s, ids, ctx["actor"], event_id="INV-B1", invoice_no="INV-B1",
             customer="客户乙", total_amount="2000.00",
             net_amount="1980.20", tax_amount="19.80")
    _invoice(s, ids, ctx["actor"], event_id="INV-C1", invoice_no="INV-C1",
             customer="客户丙", total_amount="500.00",
             net_amount="495.05", tax_amount="4.95")
    s.commit()
    report = partner_balances(s, ids["ledger_set_id"])
    assert report["period"] is not None
    by_partner = {r["partner"]: r for r in report["receivables"]}
    assert by_partner["客户乙"]["balance"] == "2,000.00"
    assert by_partner["客户丙"]["balance"] == "500.00"
    assert by_partner["客户乙"]["account"] == "1122"


def test_payment_made_attaches_supplier_dimension(ctx):
    """付款事件的应付分录自动挂 supplier 维度。

    无应付余额时直接付款形成借方 2202（预付性质），净额为负是正确语义。
    """
    s, ids = ctx["s"], ctx["ids"]
    ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ap",
        event_type="payment.made",
        event={"event_id": "PAY-S1", "supplier": "云服务商",
               "paid_at": "2026-08-15", "amount": "3600.00"},
        actor=ctx["actor"],
    )
    s.commit()
    v = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).one()
    debit_line = next(ln for ln in v.lines if ln.line_no == 1)
    assert debit_line.aux_dims == {"supplier": "云服务商"}
    report = partner_balances(s, ids["ledger_set_id"])
    by_partner = {r["partner"]: r for r in report["payables"]}
    assert by_partner["云服务商"]["balance"] == "-3,600.00"  # 负数 = 预付


def test_partner_on_dimensionless_account_rejected(ctx):
    """收入科目没声明 customer 维度 → 规则挂账被拒绝。"""
    s, ids = ctx["s"], ctx["ids"]
    register({
        "adapter": "bad", "event_type": "z", "version": "v1",
        "date_field": "d", "summary": "维度未声明",
        "lines": [
            {"side": "debit", "account": "1122",
             "amount": {"from": "amount"}, "partner": {"dim": "project",
                                                        "from": "project"}},
            {"side": "credit", "account": "6001", "amount": {"from": "amount"}},
        ],
    })
    with pytest.raises(AdapterError) as ei:
        ingest_event(s, ledger_set_id=ids["ledger_set_id"], adapter="bad",
                     event_type="z",
                     event={"d": "2026-08-10", "amount": "10.00", "project": "P1"},
                     actor=ctx["actor"])
    assert ei.value.code == "DIM_NOT_DECLARED"


def test_partner_missing_value_rejected(ctx):
    """事件缺往来单位字段 → 拒绝而不是静默挂空维度。"""
    from kernel.adapters.spec import EventFieldError

    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(EventFieldError) as ei:
        ingest_event(
            s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
            event_type="invoice.issued",
            event={"event_id": "X", "invoice_no": "X", "issued_at": "2026-08-10",
                   "net_amount": "1.00", "tax_amount": "0.00",
                   "total_amount": "1.00"},  # 无 customer 字段
            actor=ctx["actor"])
    assert ei.value.code == "FIELD_MISSING"


def test_rule_validation_rejects_bad_partner(ctx):
    bad_dim = {
        "adapter": "x", "event_type": "y", "version": "v1", "date_field": "d",
        "lines": [
            {"side": "debit", "account": "1122", "amount": {"const": "1"},
             "partner": {"dim": "_class", "from": "c"}},
            {"side": "credit", "account": "6001", "amount": {"const": "1"}},
        ],
    }
    with pytest.raises(RuleError) as ei:
        register(bad_dim)
    assert ei.value.code == "BAD_PARTNER_DIM"

    no_from = dict(bad_dim, lines=[
        {"side": "debit", "account": "1122", "amount": {"const": "1"},
         "partner": {"dim": "customer"}},
        {"side": "credit", "account": "6001", "amount": {"const": "1"}},
    ])
    with pytest.raises(RuleError) as ei2:
        register(no_from)
    assert ei2.value.code == "BAD_PARTNER_FIELD"


def test_preview_compatible_with_partner_rules(ctx):
    """升级后的内置规则在预览（不落库）下照常工作。"""
    from kernel.adapters import get_rule

    out = preview(get_rule("ar", "invoice.issued"), {
        "invoice_no": "INV-Z", "customer": "客户丁",
        "issued_at": "2026-08-10", "net_amount": "100.00",
        "tax_amount": "1.00", "total_amount": "101.00",
    })
    assert out["balanced"] is True
    assert ctx["s"].scalars(select(Voucher)).all() == []


def test_idempotent_with_partner_dims(ctx):
    """挂维度后幂等照常：同一事件重复投喂不重复入账。"""
    s, ids = ctx["s"], ctx["ids"]
    first = _invoice(s, ids, ctx["actor"], event_id="INV-IDEM")
    s.commit()
    again = _invoice(s, ids, ctx["actor"], event_id="INV-IDEM")
    s.commit()
    assert first["voucher"]["id"] == again["voucher"]["id"]
    assert again["replayed"] is True
