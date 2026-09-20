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
from kernel.events import E
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
    for etype in (E.INVOICE_RECORDED, E.INVOICE_FLAGGED):
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
            event_type=E.INVOICE_FLAGGED, aggregate_id=inv.invoice_no,
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
        event_type=E.INVOICE_RECORDED, aggregate_id=res["voucher"]["id"],
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


def preview_invoice(
    session: Session,
    *,
    ledger_set_id: str,
    source: Any,
    extractor: InvoiceExtractor,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> dict:
    """只读预览：一张发票若现在入账，会发生什么（与 ingest_invoice 共用决策与取数）。

    与 :func:`ingest_invoice` 的唯一差异是「不落库」：抽取→查重→校验→
    按同一 ``ocr/invoice.received`` 规则 ``build_lines``（即真执行写入的分录）。
    因此**预览的 proposed_voucher 与实际入账凭证逐行一致**（除并发写入外）、
    **预览的 disposition 与真实处置矩阵完全对齐**。

    这正是 O9「统一预览-确认-执行」在票据识别这条风险最高、最易盲入账的
    路径上的落地——把"点下去会发生什么"在入账前摊开给人看，复用了与
    closing.py 完全一致的单一直源范式（预览与执行共用同一取数 helper，
    而非复制一份逻辑）。

    返回：
        {invoice_no, extracted, problems, low_confidence, duplicate,
         disposition(ingested/flagged/duplicate), proposed_voucher(可能 None),
         build_error(可能 None), note}
    """
    try:
        inv = extractor.extract(source)
    except ExtractError as e:
        raise PipelineError(e.code, e.message_zh) from e

    duplicate = _invoice_no_seen(session, inv.invoice_no) is not None
    problems = validate_invoice(inv)
    low_conf = low_confidence_fields(inv, confidence_threshold)

    proposed_voucher = None
    build_error = None
    if not duplicate and not problems and not low_conf:
        # 与 ingest_invoice 真路径共用同一个 build_lines（单一真源，ADR-002）
        from kernel.adapters.engine import AdapterError, build_lines
        from kernel.adapters.registry import RuleNotFoundError, get_rule
        from kernel.adapters.spec import ZERO, render_summary
        from kernel.db.models import Account

        try:
            rule = get_rule("ocr", "invoice.received")
            if rule is None:
                raise RuleNotFoundError("ocr", "invoice.received")
            lines = build_lines(
                session, ledger_set_id=ledger_set_id,
                rule=rule, event=inv.to_event(),
            )
            total_debit = ZERO
            total_credit = ZERO
            out_lines = []
            for ln in lines:
                acc = session.get(Account, ln.account_id)
                total_debit += ln.debit
                total_credit += ln.credit
                out_lines.append({
                    "line_no": ln.line_no,
                    "account_code": acc.code if acc else None,
                    "account_name": acc.name if acc else None,
                    "debit": str(ln.debit),
                    "credit": str(ln.credit),
                })
            proposed_voucher = {
                "summary": render_summary(
                    rule.get("summary", ""), inv.to_event()),
                "lines": out_lines,
                "debit": str(total_debit),
                "credit": str(total_credit),
                "balanced": total_debit == total_credit,
            }
        except (AdapterError, RuleNotFoundError) as e:
            build_error = {"code": e.code, "message_zh": e.message_zh}

    if duplicate:
        disposition = "duplicate"
    elif problems or low_conf:
        disposition = "flagged"
    else:
        disposition = "ingested"

    notes = {
        "duplicate": "发票号已处理过，入账将报 DUPLICATE_INVOICE（防重复报销）",
        "flagged": "校验不过或存在低置信度字段，将进人工复核队列（不入账）",
        "ingested": "校验通过，将自动生成 PUSHED 凭证待人审（绝不自动过账）",
    }
    return {
        "invoice_no": inv.invoice_no,
        "extracted": asdict(inv),
        "problems": problems,
        "low_confidence": low_conf,
        "duplicate": duplicate,
        "disposition": disposition,
        "proposed_voucher": proposed_voucher,
        "build_error": build_error,
        "note": notes.get(disposition, ""),
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
