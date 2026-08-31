"""发票入账管线（P2-03）：提取 → 校验 → 查重 → 凭证草稿。

处置矩阵（宁缺毋滥）：
- 校验通过 + 查重通过 + 置信度达标 → 自动生成凭证（PUSHED 待人审）；
- 校验不通过 或 存在低置信度字段 → **不入账**，追加 ``ocr.invoice.flagged``
  事件进人工复核队列（flag 原因随 payload 可回放）；
- 发票号已处理过 → ``DUPLICATE_INVOICE``，防重复报销。

查重依据：``invoice.recorded`` / ``ocr.invoice.flagged`` 事件 payload 里的
invoice_no——两张凭证、两条事件链，同一张发票只能走一条。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.adapters.engine import ingest_event
from kernel.db.models import Event
from kernel.ledger import append_event
from kernel.ocr.extractors import ExtractError, InvoiceExtractor
from kernel.ocr.model import low_confidence_fields, validate_invoice

CONFIDENCE_THRESHOLD = 0.85


class PipelineError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _invoice_no_seen(session: Session, invoice_no: str) -> Event | None:
    for etype in ("invoice.recorded", "ocr.invoice.flagged"):
        for e in session.scalars(
            select(Event).where(Event.event_type == etype)
        ):
            if (e.payload or {}).get("invoice_no") == invoice_no:
                return e
    return None


def ingest_invoice(
    session: Session,
    *,
    ledger_set_id: str,
    source: Any,
    actor: dict,
    extractor: InvoiceExtractor,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> dict:
    """一张发票的完整入账流程。返回处置结果（ingested / flagged / duplicate）。"""
    try:
        inv = extractor.extract(source)
    except ExtractError as e:
        raise PipelineError(e.code, e.message_zh) from e

    seen = _invoice_no_seen(session, inv.invoice_no)
    if seen is not None:
        raise PipelineError(
            "DUPLICATE_INVOICE",
            f"发票 {inv.invoice_no} 已于 "
            f"{(seen.occurred_at or '').isoformat()[:19] if seen.occurred_at else '此前'}"
            " 处理过（防重复报销）",
            {"invoice_no": inv.invoice_no, "event_type": seen.event_type},
        )

    problems = validate_invoice(inv)
    low_conf = low_confidence_fields(inv, confidence_threshold)
    payload = {
        "invoice_no": inv.invoice_no,
        "problems": problems,
        "low_confidence": low_conf,
        "data": asdict(inv),
    }

    if problems or low_conf:
        append_event(
            session, ledger_set_id=ledger_set_id,
            event_type="ocr.invoice.flagged", aggregate_id=inv.invoice_no,
            payload=payload, actor=actor,
        )
        session.flush()
        return {
            "disposition": "flagged",
            "invoice_no": inv.invoice_no,
            "problems": problems,
            "low_confidence": low_conf,
            "note": "未入账，已进人工复核队列（ocr.invoice.flagged 事件可回放）",
        }

    res = ingest_event(
        session, ledger_set_id=ledger_set_id, adapter="ocr",
        event_type="invoice.received", event=inv.to_event(),
        actor=actor, event_id=f"INV-{inv.invoice_no}",
    )

    # 回填消耗事件与凭证号（入账成功才记 recorded，保证查重与账一致）
    append_event(
        session, ledger_set_id=ledger_set_id,
        event_type="invoice.recorded", aggregate_id=res["voucher"]["id"],
        payload={**payload, "voucher_no": res["voucher"]["voucher_no"]},
        actor=actor,
    )
    session.flush()
    return {
        "disposition": "ingested",
        "invoice_no": inv.invoice_no,
        "voucher": res["voucher"],
        "replayed": res["replayed"],
    }


def accuracy_report(
    session: Session,
    *,
    samples: list[dict],
) -> dict:
    """字段级准确率抽检报告（DoD：抽检 ≥95%）。

    samples 形如 ``[{"extracted": {...}, "ground_truth": {...}}, ...]``——
    extracted 是提取器输出，ground_truth 是人工标注真值。逐样本
    ``compare_fields`` 后汇总加权正确率，低于阈值给出复核建议。
    """
    from kernel.ocr.model import compare_fields

    if not samples:
        raise PipelineError("NO_SAMPLES", "抽检样本为空")
    reports = [
        compare_fields(s["extracted"], s["ground_truth"]) for s in samples
    ]
    total_fields = sum(r["sample_size"] for r in reports)
    weighted = [
        r["accuracy"] * r["sample_size"] for r in reports
    ]
    overall = sum(weighted) / total_fields if total_fields else 0.0
    return {
        "samples": len(reports),
        "fields_total": total_fields,
        "accuracy": round(overall, 4),
        "pass_threshold_95": overall >= 0.95,
        "per_sample": [
            {"accuracy": r["accuracy"], "fields": r["fields"]} for r in reports
        ],
    }
