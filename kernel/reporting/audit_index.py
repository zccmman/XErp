"""审计索引（E · 寄生 WB 架构 · 审计索引 Cloud DB）。

把 `audit_trail`（ADR-002 只读报告）升级为**可检索**的审计索引——支撑
"审计索引（Cloud DB）"向量：审计事件既能本地全文/结构化检索，又能**镜像**
到 WorkBuddy 云端 DB 做持久化、跨运行时的审计索引。

设计铁律（守 ADR-002 / R2 单一真源）：
- 索引是**事件账本的派生视图**，绝不另存一份事实；重建永远来自 `events` 表。
- 审计索引**只读检索**，绝不修改事件链、绝不制单/过账/结账。
- 云端镜像为**尽力而为**（best-effort），任何云调用失败都不影响本地检索——
  桥/云是纯增值信息，绝不阻塞业务（与算子信号桥同口径）。

后端可插拔：
- `LocalAuditIndex`：SQLite FTS5 全文 + 结构化检索（本地，已验证可用）。
- `CloudAuditIndex`：包裹 Local 做本地检索，并把新增事件**镜像**到 WB 云端 DB
  （`XERP_AUDIT_CLOUD_URL` 配置即启用；未配置或失败则静默降级到本地）。
- `get_audit_index()`：按环境变量返回合适的后端。
"""

from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime

from sqlalchemy import text

from kernel.events import DESCRIPTIONS, E


# 审计意义最强的 payload 键，按优先级抽取摘要
_SUMMARY_KEYS = ("voucher_no", "voucher_id", "period", "amount", "reason",
                 "invoice_no", "source", "breaker", "decision")


def _event_label(event_type: str) -> str:
    try:
        return DESCRIPTIONS.get(E(event_type), event_type)
    except ValueError:
        return event_type


def _load(v):
    """JSON 列在原生 text() 查询下可能回读为字符串，统一反序列化为对象。"""
    if v is None or isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def _coerce_dt(v):
    """DateTime 列在原生 text() 查询下可能回读为字符串，统一转为 datetime。"""
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _actor_name(actor) -> str:
    actor = _load(actor)
    if not actor:
        return "系统"
    if isinstance(actor, dict):
        return str(actor.get("display_name") or actor.get("name")
                   or actor.get("id") or "未知")
    return str(actor)


def _payload_summary(payload) -> str:
    payload = _load(payload)
    if not payload or not isinstance(payload, dict):
        return ""
    for key in _SUMMARY_KEYS:
        val = payload.get(key)
        if val not in (None, ""):
            return f"{key}={val}"
    items = list(payload.items())[:1]
    return f"{items[0][0]}={items[0][1]}" if items else ""


def _payload_text(payload) -> str:
    payload = _load(payload)
    if not payload:
        return ""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class AuditIndexError(RuntimeError):
    """审计索引内部错误（FTS5 不可用等）。"""


class AuditIndex(ABC):
    """审计索引后端抽象。

    所有后端共享同一检索语义：``search`` 返回时间倒序的审计命中列表
    ``[{event_type, label, actor, summary, payload_text, occurred_at}]``。
    """

    @abstractmethod
    def search(
        self,
        session,
        ledger_set_id: str,
        *,
        q: str | None = None,
        actor: str | None = None,
        event_type: str | None = None,
        year: int = 0,
        month: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        ...


class LocalAuditIndex(AuditIndex):
    """本地 SQLite FTS5 审计索引（派生自 events 表）。

    影子表 ``audit_fts``（FTS5 虚拟表）+ ``audit_index_meta``（增量同步位点）。
    二者均落同一账库文件，跨重启持久；索引丢失可随时从 events 重建。
    """

    def _ensure(self, session) -> None:
        session.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS audit_fts USING fts5("
            "  ledger_set_id UNINDEXED,"
            "  event_type,"
            "  label,"
            "  actor,"
            "  summary,"
            "  payload_text,"
            "  occurred_at UNINDEXED,"
            "  year UNINDEXED,"
            "  month UNINDEXED"
            ")"
        ))
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS audit_index_meta ("
            "  ledger_set_id TEXT PRIMARY KEY,"
            "  last_event_id INTEGER NOT NULL"
            ")"
        ))
        session.flush()

    def _sync(self, session, ledger_set_id: str) -> None:
        """增量同步：把 events 表里 id > 上次位点 的新事件灌入 FTS 索引。"""
        self._ensure(session)
        row = session.execute(
            text("SELECT last_event_id FROM audit_index_meta WHERE ledger_set_id=:ls"),
            {"ls": ledger_set_id},
        ).first()
        last = row[0] if row else 0
        new_rows = session.execute(
            text(
                "SELECT id, event_type, actor, payload, occurred_at "
                "FROM events WHERE ledger_set_id=:ls AND id>:last ORDER BY id"
            ),
            {"ls": ledger_set_id, "last": last},
        ).all()
        if not new_rows:
            return
        for r in new_rows:
            occurred: datetime | None = _coerce_dt(r.occurred_at)
            session.execute(
                text(
                    "INSERT INTO audit_fts("
                    "  ledger_set_id, event_type, label, actor, summary,"
                    "  payload_text, occurred_at, year, month"
                    ") VALUES(:ls, :et, :label, :actor, :summary,"
                    "  :ptext, :occ, :year, :month)"
                ),
                {
                    "ls": ledger_set_id,
                    "et": r.event_type,
                    "label": _event_label(r.event_type),
                    "actor": _actor_name(r.actor),
                    "summary": _payload_summary(r.payload),
                    "ptext": _payload_text(r.payload),
                    "occ": occurred.isoformat() if occurred else "",
                    "year": occurred.year if occurred else 0,
                    "month": occurred.month if occurred else 0,
                },
            )
        newest = new_rows[-1].id
        session.execute(
            text(
                "INSERT INTO audit_index_meta(ledger_set_id, last_event_id) "
                "VALUES(:ls, :v) "
                "ON CONFLICT(ledger_set_id) DO UPDATE SET last_event_id=:v"
            ),
            {"ls": ledger_set_id, "v": newest},
        )
        session.flush()

    def rebuild(self, session, ledger_set_id: str) -> int:
        """全量重建（索引损坏/迁移后）：清空本账套索引并从 events 重灌。返回命中数。"""
        self._ensure(session)
        session.execute(
            text("DELETE FROM audit_fts WHERE ledger_set_id=:ls"), {"ls": ledger_set_id}
        )
        session.execute(
            text(
                "INSERT OR REPLACE INTO audit_index_meta(ledger_set_id, last_event_id) "
                "VALUES(:ls, 0)"
            ),
            {"ls": ledger_set_id},
        )
        session.flush()
        self._sync(session, ledger_set_id)
        cnt = session.execute(
            text("SELECT count(*) FROM audit_fts WHERE ledger_set_id=:ls"),
            {"ls": ledger_set_id},
        ).scalar()
        return int(cnt or 0)

    def search(
        self,
        session,
        ledger_set_id: str,
        *,
        q: str | None = None,
        actor: str | None = None,
        event_type: str | None = None,
        year: int = 0,
        month: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        self._sync(session, ledger_set_id)
        where = ["ledger_set_id = :ls"]
        params: dict = {"ls": ledger_set_id}
        if q:
            # FTS5 短语检索：双引号包裹 + 内部双引号转义，避免注入 MATCH 语法
            where.append("audit_fts MATCH :q")
            params["q"] = '"' + q.replace('"', '""') + '"'
        if actor:
            where.append("actor = :actor")
            params["actor"] = actor
        if event_type:
            where.append("event_type = :et")
            params["et"] = event_type
        if year:
            where.append("year = :year")
            params["year"] = year
            if month:
                where.append("month = :month")
                params["month"] = month
        params["lim"] = limit
        rows = session.execute(
            text(
                "SELECT ledger_set_id, event_type, label, actor, summary,"
                " payload_text, occurred_at "
                "FROM audit_fts WHERE " + " AND ".join(where) +
                " ORDER BY occurred_at DESC LIMIT :lim"
            ),
            params,
        ).all()
        return [
            {
                "ledger_set_id": r.ledger_set_id,
                "event_type": r.event_type,
                "label": r.label,
                "actor": r.actor,
                "summary": r.summary,
                "payload_text": r.payload_text,
                "occurred_at": r.occurred_at,
            }
            for r in rows
        ]


class _CloudMirror:
    """云端 DB 镜像客户端（WB 云端 Database 表，REST 写入）。

    接口契约（文档化）：``POST {XERP_AUDIT_CLOUD_URL}``，body 为审计事件 JSON，
    Authorization: Bearer {XERP_AUDIT_CLOUD_TOKEN}。任何异常都被吞掉——镜像失败
    绝不阻塞本地检索。
    """

    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token

    def mirror(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            method="POST",
        )
        # 超时短、异常全吞：云端镜像纯增值信息
        urllib.request.urlopen(req, timeout=3).close()


class CloudAuditIndex(AuditIndex):
    """云端 DB 审计索引：本地检索 + 增量镜像到 WB 云端 DB（best-effort）。

    检索仍走本地 FTS5（保证离线/云挂时可用）；每次同步新增事件时并行镜像到云端，
    使审计索引在云端持久化、可跨运行时检索。云端调用失败静默降级。
    """

    def __init__(self, cloud: _CloudMirror | None = None):
        self._local = LocalAuditIndex()
        self._cloud = cloud

    def _mirror_new(self, session, ledger_set_id: str) -> None:
        if self._cloud is None:
            return
        # 镜像进度用独立表 audit_cloud_meta，与本地索引增量位点（audit_index_meta）
        # 解耦，避免索引同步推进位点后镜像漏掉新增事件。
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS audit_cloud_meta ("
            "  ledger_set_id TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL"
            ")"
        ))
        session.flush()
        row = session.execute(
            text("SELECT last_event_id FROM audit_cloud_meta WHERE ledger_set_id=:ls"),
            {"ls": ledger_set_id},
        ).first()
        last = row[0] if row else 0
        new_rows = session.execute(
            text(
                "SELECT id, event_type, actor, payload, occurred_at, aggregate_id "
                "FROM events WHERE ledger_set_id=:ls AND id>:last ORDER BY id"
            ),
            {"ls": ledger_set_id, "last": last},
        ).all()
        for r in new_rows:
            try:
                self._cloud.mirror({
                    "event_id": r.id,
                    "ledger_set_id": ledger_set_id,
                    "event_type": r.event_type,
                    "aggregate_id": r.aggregate_id,
                    "actor": _load(r.actor),
                    "payload": _load(r.payload),
                    "occurred_at": (_coerce_dt(r.occurred_at).isoformat()
                                    if _coerce_dt(r.occurred_at) else None),
                })
            except Exception:
                # 云端镜像失败：静默降级，绝不抛
                pass
        if new_rows:
            newest = new_rows[-1].id
            session.execute(
                text(
                    "INSERT INTO audit_cloud_meta(ledger_set_id, last_event_id) "
                    "VALUES(:ls, :v) "
                    "ON CONFLICT(ledger_set_id) DO UPDATE SET last_event_id=:v"
                ),
                {"ls": ledger_set_id, "v": newest},
            )
            session.flush()

    def search(
        self,
        session,
        ledger_set_id: str,
        *,
        q: str | None = None,
        actor: str | None = None,
        event_type: str | None = None,
        year: int = 0,
        month: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        # 先本地同步（同时推进增量位点），再把新增事件镜像到云端
        self._local._sync(session, ledger_set_id)  # noqa: SLF001（包内复用）
        self._mirror_new(session, ledger_set_id)
        return self._local.search(
            session, ledger_set_id, q=q, actor=actor,
            event_type=event_type, year=year, month=month, limit=limit,
        )


def get_audit_index() -> AuditIndex:
    """按环境变量选择后端：配置 ``XERP_AUDIT_CLOUD_URL`` 走云端镜像，否则纯本地。"""
    url = os.environ.get("XERP_AUDIT_CLOUD_URL", "")
    if url:
        token = os.environ.get("XERP_AUDIT_CLOUD_TOKEN", "")
        return CloudAuditIndex(_CloudMirror(url, token))
    return LocalAuditIndex()
