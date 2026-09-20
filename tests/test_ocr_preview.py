"""P2-03 C2（O9 统一预览-确认-执行）：ocr_preview 与 ocr_ingest_invoice 共用
决策矩阵与同一 build_lines 取数，保证「预览看到的分录 = 真入账分录」、
「预览处置 = 真实处置」——把票据这条最易盲入账的路径变成所见即所入账。

核心契约（对应 closing.py 的 ADR-002 单一直源范式）：
1. 合格发票：preview.disposition == ingest.disposition == ingested；
2. preview.proposed_voucher 的逐行 (line_no, account_code, debit, credit)
   必须逐字等于真入账凭证的分录；
3. preview 是只读的：调用前后不得新增任何 Voucher / Event；
4. flagged / duplicate 的预览处置也必须与真实处置完全一致。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters.registry import clear
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Voucher
from kernel.ocr import (
    PipelineError,
    StructuredExtractor,
    ingest_invoice,
    preview_invoice,
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
    return {
        "s": s,
        "ids": ids,
        "actor": {"type": "user", "id": ids["subject_id"]},
        "extractor": StructuredExtractor(),
    }


def _invoice(no="26120001", total="1010.00", net="1000.00", tax="10.00",
             category="办公费", seller="云服务商", conf=None,
             invoice_date="2026-08-28"):
    return {
        "invoice_no": no, "invoice_date": invoice_date,
        "seller_name": seller, "seller_tax_id": "91330106MA2XY1N234",
        "total_amount": total, "net_amount": net, "tax_amount": tax,
        "expense_category": category, "confidence": conf or {},
    }


def _voucher_lines(s, v) -> list[tuple[int, str, str, str]]:
    accs = {a.id: a for a in s.scalars(select(Account)).all()}
    return [
        (ln.line_no, accs[ln.account_id].code, str(ln.debit), str(ln.credit))
        for ln in v.lines
    ]


def _preview_lines(pv) -> list[tuple[int, str, str, str]]:
    return [
        (ln["line_no"], ln["account_code"], ln["debit"], ln["credit"])
        for ln in pv["proposed_voucher"]["lines"]
    ]


# ---------- 1. preview == execution：合格发票 ----------


def test_preview_ingested_and_lines_match_ingest(ctx):
    """合格发票：预览=ingested，且拟生成分录逐行等于真入账分录。"""
    s, ids = ctx["s"], ctx["ids"]
    pv = preview_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=_invoice(), extractor=ctx["extractor"])
    assert pv["disposition"] == "ingested"
    assert pv["proposed_voucher"]["balanced"] is True
    assert pv["proposed_voucher"]["debit"] == "1010.00"
    assert pv["proposed_voucher"]["credit"] == "1010.00"

    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=_invoice(), actor=ctx["actor"],
                         extractor=ctx["extractor"])
    s.commit()
    assert res["disposition"] == "ingested"
    v = s.get(Voucher, res["voucher"]["id"])
    assert _voucher_lines(s, v) == _preview_lines(pv)


def test_preview_dynamic_account_matches_ingest(ctx):
    """差旅费发票：预览与真入账都映射到 660203（动态科目，最易漂移点）。"""
    s, ids = ctx["s"], ctx["ids"]
    src = _invoice(no="26120004", category="差旅费",
                   total="2480.00", net="2455.45", tax="24.55")
    pv = preview_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=src, extractor=ctx["extractor"])
    assert pv["disposition"] == "ingested"
    assert pv["proposed_voucher"]["lines"][0]["account_code"] == "660203"

    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=src, actor=ctx["actor"],
                         extractor=ctx["extractor"])
    s.commit()
    v = s.get(Voucher, res["voucher"]["id"])
    assert _voucher_lines(s, v)[0][1] == "660203"


# ---------- 2. preview 是只读的 ----------


def test_preview_does_not_persist(ctx):
    """预览不得落库：调用前后 Voucher / Event 数量不变。"""
    s, ids = ctx["s"], ctx["ids"]
    n_v_before = len(s.scalars(select(Voucher)).all())
    n_e_before = len(s.scalars(select(Event)).all())

    preview_invoice(s, ledger_set_id=ids["ledger_set_id"],
                    source=_invoice(), extractor=ctx["extractor"])

    assert len(s.scalars(select(Voucher)).all()) == n_v_before
    assert len(s.scalars(select(Event)).all()) == n_e_before


# ---------- 3. flagged / duplicate 预览处置与真实对齐 ----------


def test_preview_flagged_matches_ingest(ctx):
    """价税勾稽不符：预览=flagged 且无拟生成凭证，真入账也=flagged、不入账。"""
    s, ids = ctx["s"], ctx["ids"]
    src = _invoice(no="26120002", total="1010.00",
                   net="900.00", tax="10.00")
    pv = preview_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=src, extractor=ctx["extractor"])
    assert pv["disposition"] == "flagged"
    assert pv["proposed_voucher"] is None
    assert any("价税勾稽" in p for p in pv["problems"])

    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=src, actor=ctx["actor"],
                         extractor=ctx["extractor"])
    s.commit()
    assert res["disposition"] == "flagged"
    assert s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).all() == []


def test_preview_low_confidence_matches_ingest(ctx):
    """低置信度字段：预览=flagged，真入账也=flagged。"""
    s, ids = ctx["s"], ctx["ids"]
    src = _invoice(no="26120003")
    src["confidence"] = {"total_amount": 0.42}

    pv = preview_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=src, extractor=ctx["extractor"])
    assert pv["disposition"] == "flagged"
    assert pv["low_confidence"] == ["total_amount"]

    res = ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=src, actor=ctx["actor"],
                         extractor=ctx["extractor"])
    s.commit()
    assert res["disposition"] == "flagged"
    assert res["low_confidence"] == ["total_amount"]


def test_preview_detects_duplicate(ctx):
    """已入账发票再次预览：duplicate=True、disposition=duplicate。"""
    s, ids = ctx["s"], ctx["ids"]
    ingest_invoice(s, ledger_set_id=ids["ledger_set_id"],
                   source=_invoice(), actor=ctx["actor"],
                   extractor=ctx["extractor"])
    s.commit()

    pv = preview_invoice(s, ledger_set_id=ids["ledger_set_id"],
                         source=_invoice(), extractor=ctx["extractor"])
    assert pv["duplicate"] is True
    assert pv["disposition"] == "duplicate"
    assert pv["proposed_voucher"] is None
