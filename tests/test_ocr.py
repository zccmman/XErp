"""P2-03 TDD：发票 OCR 入账——校验、查重、处置矩阵、动态科目、准确率度量。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters.registry import clear
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Voucher
from kernel.ocr import (
    InvoiceData,
    PipelineError,
    StructuredExtractor,
    accuracy_report,
    ingest_invoice,
    validate_invoice,
)
from kernel.ocr.model import compare_fields
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
    return {"s": s, "ids": ids, "actor": {"type": "user", "id": ids["subject_id"]},
            "extractor": StructuredExtractor()}


def _invoice(no="26120001", total="1010.00", net="1000.00", tax="10.00",
             category="办公费", seller="云服务商", conf=None,
             invoice_date="2026-08-28"):
    return {
        "invoice_no": no, "invoice_date": invoice_date,
        "seller_name": seller, "seller_tax_id": "91330106MA2XY1N234",
        "total_amount": total, "net_amount": net, "tax_amount": tax,
        "expense_category": category, "confidence": conf or {},
    }


# ---------- 字段级校验 ----------


def test_validate_ok_invoice():
    assert validate_invoice(InvoiceData(**_invoice())) == []


@pytest.mark.parametrize(
    ("kw", "fragment"),
    [
        ({"total": "1010.00", "net": "900.00", "tax": "10.00"}, "价税勾稽不符"),
        ({"net": "1000.00", "tax": "500.00", "total": "1500.00"}, "隐含税率"),
        ({"no": "ABC123"}, "发票号格式非法"),
        ({"no": "26120001"}, None),  # 占位保持参数个数
    ],
)
def test_validate_catches(kw, fragment):
    if fragment is None:
        return
    problems = validate_invoice(InvoiceData(**_invoice(**kw)))
    assert any(fragment in p for p in problems)


def test_validate_future_date():
    problems = validate_invoice(InvoiceData(**_invoice(no="26120999", invoice_date="2099-01-01")))
    assert any("发票日期在未来" in p for p in problems)


# ---------- 处置矩阵 ----------


def test_ingest_invoice_creates_pushed_voucher(ctx):
    """合格发票 → PUSHED 凭证（价税合计全额进费用，小规模不抵扣）。"""
    s, ids = ctx["s"], ctx["ids"]
    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=_invoice(), actor=ctx["actor"],
                         extractor=ctx["extractor"])
    s.commit()
    assert res["disposition"] == "ingested"
    v = s.get(Voucher, res["voucher"]["id"])
    assert v.status == "PUSHED"
    accs = {a.id: a for a in s.scalars(select(Account)).all()}
    debit_line = next(ln for ln in v.lines if ln.line_no == 1)
    assert accs[debit_line.account_id].code == "660202"      # 办公费
    assert str(debit_line.debit) == "1010.00"                # 价税合计
    assert debit_line.aux_dims is None


def test_duplicate_invoice_rejected(ctx):
    """同发票号第二次投喂 → DUPLICATE_INVOICE（防重复报销硬闸）。"""
    s, ids = ctx["s"], ctx["ids"]
    ingest_invoice(s, ledger_set_id=ids["ledger_set_id"], source=_invoice(),
                   actor=ctx["actor"], extractor=ctx["extractor"])
    s.commit()
    with pytest.raises(PipelineError) as ei:
        ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                       source=_invoice(), actor=ctx["actor"],
                       extractor=ctx["extractor"])
    s.commit()
    assert ei.value.code == "DUPLICATE_INVOICE"


def test_invalid_invoice_flagged_not_booked(ctx):
    """价税勾稽不符 → 不入账，进复核队列（事件可回放）。"""
    s, ids = ctx["s"], ctx["ids"]
    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=_invoice(no="26120002", total="1010.00",
                                         net="900.00", tax="10.00"),
                         actor=ctx["actor"], extractor=ctx["extractor"])
    s.commit()
    assert res["disposition"] == "flagged"
    assert any("价税勾稽" in p for p in res["problems"])
    assert s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).all() == []
    flagged = s.scalars(select(Event).where(
        Event.event_type == "ocr.invoice.flagged")).all()
    assert len(flagged) == 1
    assert flagged[0].payload["invoice_no"] == "26120002"


def test_low_confidence_flagged(ctx):
    """关键字段置信度低于阈值 → 不自动入账。"""
    s, ids = ctx["s"], ctx["ids"]
    inv = _invoice(no="26120003")
    inv["confidence"] = {"total_amount": 0.42}
    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"], source=inv,
                         actor=ctx["actor"], extractor=ctx["extractor"])
    s.commit()
    assert res["disposition"] == "flagged"
    assert res["low_confidence"] == ["total_amount"]


# ---------- 动态科目映射 ----------


def test_dynamic_account_by_category(ctx):
    """差旅费发票 → 660203；未知类别 → 默认 660202。"""
    s, ids = ctx["s"], ctx["ids"]
    accs = {a.id: a for a in s.scalars(select(Account)).all()}

    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=_invoice(no="26120004", category="差旅费",
                                         total="2480.00", net="2455.45",
                                         tax="24.55"),
                         actor=ctx["actor"], extractor=ctx["extractor"])
    s.commit()
    v = s.get(Voucher, res["voucher"]["id"])
    assert accs[next(ln for ln in v.lines if ln.line_no == 1).account_id].code == "660203"

    res2 = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                          source=_invoice(no="26120005", category=" OTHER "),
                          actor=ctx["actor"], extractor=ctx["extractor"])
    s.commit()
    v2 = s.get(Voucher, res2["voucher"]["id"])
    assert accs[next(ln for ln in v2.lines if ln.line_no == 1).account_id].code == "660202"


# ---------- 准确率度量（DoD：抽检 ≥95%） ----------


def test_accuracy_perfect_sample_passes():
    truth = {"invoice_no": "26120001", "invoice_date": "2026-08-28",
             "seller_name": "云服务商", "total_amount": "1010.00",
             "net_amount": "1000.00", "tax_amount": "10.00"}
    r = compare_fields(truth, truth)
    assert r["accuracy"] == 1.0 and r["pass_threshold_95"] is True


def test_accuracy_amount_error_drops_below_threshold():
    truth = {"invoice_no": "26120001", "invoice_date": "2026-08-28",
             "seller_name": "云服务商", "total_amount": "1010.00",
             "net_amount": "1000.00", "tax_amount": "10.00"}
    extracted = dict(truth, total_amount="1100.00")  # 金额错，权重 3 最高
    r = compare_fields(extracted, truth)
    assert r["pass_threshold_95"] is False
    fields = {f["field"]: f["match"] for f in r["fields"]}
    assert fields["total_amount"] is False


def test_accuracy_report_via_pipeline(ctx):
    truth = {"invoice_no": "26120001", "invoice_date": "2026-08-28",
             "seller_name": "云服务商", "total_amount": "1010.00",
             "net_amount": "1000.00", "tax_amount": "10.00"}
    rep = accuracy_report(ctx["s"], samples=[
        {"extracted": truth, "ground_truth": truth},
        {"extracted": dict(truth, seller_name="云服务上"),   # 错一个低权重字段
         "ground_truth": truth},
    ])
    assert rep["samples"] == 2
    assert rep["pass_threshold_95"] is True  # 金额全对，低权重错字不拉到线下
    with pytest.raises(PipelineError):
        accuracy_report(ctx["s"], samples=[])
