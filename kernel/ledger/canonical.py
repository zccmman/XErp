"""canonical_json — 跨语言可验证的确定性序列化（ADR-002）。

规则：键排序、无空白分隔符、ensure_ascii=False、UTF-8。
金额在 payload 中必须已是字符串（ADR-003 decimal-string），此处不做数值转换。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC as _UTC
from datetime import datetime
from typing import Any

GENESIS = "0" * 64


def canonical_json(obj: Any) -> str:
    if isinstance(obj, datetime):
        obj = _norm_dt(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _norm_dt(dt: datetime) -> str:
    """哈希用时间规范：统一转 UTC 后去 tzinfo 再 isoformat。

    原因：SQLite 往返会剥离 tzinfo，PG timestamptz 返回 aware——
    以 naive-UTC 为唯一规范可保证跨库重算一致。
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(_UTC).replace(tzinfo=None)
    return dt.isoformat()


def compute_event_hash(
    prev_hash: str,
    *,
    ledger_set_id: str,
    event_type: str,
    aggregate_id: str,
    payload: dict,
    actor: dict,
    occurred_at: datetime,
) -> str:
    body = canonical_json(
        {
            "ledger_set_id": ledger_set_id,
            "event_type": event_type,
            "aggregate_id": aggregate_id,
            "payload": payload,
            "actor": actor,
            "occurred_at": _norm_dt(occurred_at),
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()
