"""异常侦测与断路器（P3-02）：规则 + LLM 双通道，异常冻结 Agent 自治。

设计红线
--------
- **断路器只冻结自治主体（type=agent）**，人类用户永不受影响——
  自动化出问题时被停机的是自动化本身，不是人的操作自由；
- 冻结/解除都是事件（``agent.breaker.tripped`` / ``agent.breaker.released``），
  状态 = 事件流的最终态，可回放、可审计；
- 解除必须由人类执行（MCP anomaly_release，admin 鉴权），Agent 不能自解。

双通道
------
- **规则通道**（确定性，默认启用）：大额、非常规时间、罕见科目、当日频率激增；
- **LLM 通道**（可插拔）：摘要交给 openai 兼容接口判断可疑度，env 未配置自动跳过
  （``XERP_LLM_BASE_URL`` / ``XERP_LLM_API_KEY`` / ``XERP_LLM_MODEL``）。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Event, Subject, Voucher, VoucherLine, utcnow
from kernel.events import E
from kernel.ledger import append_event

# ---------- 规则阈值（默认值，可参数覆盖） ----------
DEFAULTS: dict[str, Any] = {
    "large_amount": Decimal("10000.00"),   # 单凭证借方合计
    "off_hours_start": 22,                 # 非常规时间窗 [22:00, 06:00)
    "off_hours_end": 6,
    "rare_days": 90,                       # 科目近 N 天未出现 = 罕见
    "freq_daily_limit": 20,                # 单主体单日创建凭证数上限
}


class AnomalyError(ValueError):
    def __init__(self, code: str, message_zh: str):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh


@dataclass
class Finding:
    rule: str
    severity: str            # info | warn | critical
    message_zh: str
    details: dict = field(default_factory=dict)


# ---------- 断路器状态（事件流最终态） ----------


def breaker_is_open(session: Session, agent_subject_id: str) -> dict | None:
    """断路器状态：最后一次 tripped/release 谁更新。开 → 返回跳闸事件信息。"""
    last = None
    for e in session.scalars(
        select(Event).where(
            Event.event_type.in_((E.BREAKER_TRIPPED,
                                  E.BREAKER_RELEASED))
        ).order_by(Event.id.desc())
    ):
        if (e.payload or {}).get("subject_id") != agent_subject_id:
            continue
        last = e
        break
    if last is not None and last.event_type == E.BREAKER_TRIPPED:
        return {
            "tripped_at": last.occurred_at.isoformat() if last.occurred_at else None,
            "reasons": (last.payload or {}).get("reasons", []),
        }
    return None


def check_breaker(session: Session, actor_id: str) -> None:
    """自治闸门：type=agent 且断路器开 → 拒绝（供 create_voucher 前调用）。"""
    subject = session.scalars(
        select(Subject).where(Subject.id == actor_id)
    ).first()
    if subject is None or subject.type != "agent":
        return  # 人类永不受断路器影响
    state = breaker_is_open(session, actor_id)
    if state:
        raise AnomalyError(
            "BREAKER_OPEN",
            "该 Agent 自治已被断路器冻结（检出异常待人工复核）："
            + "；".join(state["reasons"])[:200],
        )


def trip_breaker(session: Session, *, subject_id: str, reasons: list[str],
                 actor: dict) -> None:
    append_event(
        session, ledger_set_id="__breaker__",
        event_type=E.BREAKER_TRIPPED, aggregate_id=subject_id,
        payload={"subject_id": subject_id, "reasons": reasons},
        actor=actor,
    )


def release_breaker(session: Session, *, subject_id: str, actor: dict,
                    note: str = "") -> None:
    append_event(
        session, ledger_set_id="__breaker__",
        event_type=E.BREAKER_RELEASED, aggregate_id=subject_id,
        payload={"subject_id": subject_id, "note": note}, actor=actor,
    )


# ---------- 规则通道 ----------


def _voucher_amount(v: Voucher) -> Decimal:
    return sum((Decimal(str(ln.debit)) for ln in v.lines), Decimal("0"))


def _is_off_hours(created_at: datetime, thresholds: dict) -> bool:
    hour = created_at.hour
    start, end = thresholds["off_hours_start"], thresholds["off_hours_end"]
    if start <= hour or hour < end:      # 跨午夜窗口
        return True
    return created_at.weekday() >= 5     # 周末


def rule_scan(session: Session, v: Voucher, *,
              thresholds: dict | None = None) -> list[Finding]:
    th = {**DEFAULTS, **(thresholds or {})}
    findings: list[Finding] = []
    total = _voucher_amount(v)
    created = v.created_at or utcnow()

    # 1) 大额
    if total >= th["large_amount"]:
        findings.append(Finding(
            "large_amount", "warn",
            f"凭证金额 {total:,.2f} 达到大额阈值 {th['large_amount']:,.2f}",
            {"total": str(total)},
        ))

    # 2) 非常规时间（创建时刻）
    if _is_off_hours(created, th):
        findings.append(Finding(
            "off_hours", "info",
            f"创建于非常规时间：{created.isoformat()[:19]}（周末或夜间）",
        ))

    # 3) 罕见科目：近 N 天该账套其他凭证未使用过该科目。
    #    前置：账套需有一定历史密度（近 N 天 ≥5 张其他凭证）——
    #    新账套/低流量下"没用过"是常态而非异常，否则必然误报。
    codes = {ln.account_id for ln in v.lines}
    if codes:
        since = created - timedelta(days=th["rare_days"])
        recent = list(session.execute(
            select(VoucherLine, Voucher)
            .join(Voucher, VoucherLine.voucher_id == Voucher.id)
            .where(
                Voucher.ledger_set_id == v.ledger_set_id,
                Voucher.id != v.id,
                Voucher.created_at >= since,
            )
        ))
        seen: set[str] = {ln2.account_id for ln2, _v2 in recent}
        if len({_v2.id for _l2, _v2 in recent}) >= 5:
            acc_map = {a.id: a for a in session.scalars(
                select(Account).where(
                    Account.ledger_set_id == v.ledger_set_id)).all()}
            rare = [acc_map[aid].code for aid in codes
                    if aid not in seen and aid in acc_map]
            if rare and len(rare) == len(codes):
                findings.append(Finding(
                    "rare_account", "info",
                    f"全部科目近 {th['rare_days']} 天未出现：{'、'.join(rare)}",
                    {"accounts": rare},
                ))

    # 4) 当日频率激增（同创建主体当日 created 事件数）
    creator = v.created_by
    if creator:
        today = created.date()
        count = 0
        for e in session.scalars(
            select(Event).where(Event.event_type == E.VOUCHER_CREATED)
        ):
            if (e.actor or {}).get("id") != creator:
                continue
            if e.occurred_at and e.occurred_at.date() == today:
                count += 1
        if count > th["freq_daily_limit"]:
            findings.append(Finding(
                "freq_spike", "warn",
                f"当日已创建 {count} 张凭证，超过阈值 {th['freq_daily_limit']}",
                {"count": count},
            ))
    return findings


# ---------- LLM 通道（可插拔） ----------


def llm_scan(v: Voucher, lines_detail: list[dict]) -> list[Finding] | None:
    """摘要级可疑度判断。未配置 LLM 环境变量 → 返回 None（跳过）。"""
    base_url = (os.environ.get("XERP_LLM_BASE_URL") or "").rstrip("/")
    api_key = os.environ.get("XERP_LLM_API_KEY") or ""
    model = os.environ.get("XERP_LLM_MODEL") or ""
    if not (base_url and api_key and model):
        return None
    prompt = (
        "你是财务异常侦测引擎。判断以下凭证是否可疑（欺诈/误操作/异常模式），"
        '只输出 JSON：{"suspicious": bool, "reason": "..."}。\n'
        f"摘要：{v.summary or ''}\n分录：{json.dumps(lines_detail, ensure_ascii=False)}"
    )
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0}
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        content = payload["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}") + 1
        data = json.loads(content[start:end])
    except Exception:  # noqa: BLE001 —— LLM 通道失败不阻塞主流程，静默跳过
        return None
    if data.get("suspicious"):
        return [Finding("llm_suspicious", "warn",
                        f"LLM 判定可疑：{data.get('reason', '')}")]
    return []


# ---------- 扫描入口 ----------


def scan_voucher(
    session: Session, v: Voucher, *, actor: dict,
    thresholds: dict | None = None, trip_on: tuple[str, ...] = ("large_amount",
                                                                "freq_spike"),
) -> list[Finding]:
    """对单张凭证扫描；命中 trip_on 规则 → 断路器跳闸（仅冻结 agent 主体）。"""
    lines_detail = []
    acc_map = {a.id: a for a in session.scalars(
        select(Account).where(Account.ledger_set_id == v.ledger_set_id)).all()}
    for ln in v.lines:
        acc = acc_map.get(ln.account_id)
        lines_detail.append({
            "account": acc.code if acc else "?",
            "debit": str(ln.debit), "credit": str(ln.credit),
        })

    findings = rule_scan(session, v, thresholds=thresholds)
    llm = llm_scan(v, lines_detail)
    if llm:
        findings.extend(llm)

    if findings:
        append_event(
            session, ledger_set_id=v.ledger_set_id,
            event_type=E.AGENT_ANOMALY_DETECTED, aggregate_id=v.id,
            payload={
                "voucher_no": v.voucher_no,
                "findings": [{"rule": f.rule, "severity": f.severity,
                              "message": f.message_zh} for f in findings],
            },
            actor=actor,
        )

        hit = [f for f in findings if f.rule in trip_on]
        if hit and v.created_by:
            subject = session.scalars(
                select(Subject).where(Subject.id == v.created_by)
            ).first()
            if subject is not None and subject.type == "agent":
                trip_breaker(
                    session, subject_id=subject.id,
                    reasons=[f"{f.rule}: {f.message_zh}" for f in hit],
                    actor=actor,
                )
    session.flush()
    return findings
