"""AI 决策留痕（P1-04）：把 Agent 的推理与工具调用写入事件账本。

设计取舍：
- 默认**只存 prompt 摘要哈希**（sha256），不落原文——避免把敏感上下文写进不可篡改账本
- 显式 `include_prompt=True` 时才存全文（调用方自负脱敏责任）
- 输出同样只存摘要（output_summary，建议 ≤500 字）
- 事件类型 agent.decision，与其他事件同链（verify_chain 可校验）
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from kernel.db.models import Event
from kernel.ledger import append_event

MAX_SUMMARY = 500


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _clip(text: str, limit: int = MAX_SUMMARY) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def log_agent_decision(
    session: Session,
    *,
    ledger_set_id: str,
    actor: dict,
    prompt: str = "",
    tool_calls: list[dict] | None = None,
    output_summary: str = "",
    include_prompt: bool = False,
    model: str = "",
) -> Event:
    """记录一次 AI 决策：prompt(哈希/可选全文)、工具调用、输出摘要。"""
    payload = {
        "prompt_sha256": _sha256(prompt),
        "prompt_chars": len(prompt or ""),
        "tool_calls": [
            {
                "tool": str(tc.get("tool", "")),
                "args_sha256": _sha256(str(tc.get("args", "") or "")),
                "result_summary": _clip(str(tc.get("result_summary", "") or "")),
            }
            for tc in (tool_calls or [])
        ],
        "output_summary": _clip(output_summary),
        "model": model,
    }
    if include_prompt:
        payload["prompt"] = _clip(prompt, 4000)
    return append_event(
        session,
        ledger_set_id=ledger_set_id,
        event_type="agent.decision",
        aggregate_id="agent:" + str(actor.get("id") or ""),
        payload=payload,
        actor=actor,
    )
