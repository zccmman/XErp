"""事件适配器消费引擎（P2-01）。

把第三方业务事件按声明式规则转成凭证，**零核心改动**：

- 只依赖内核公开 API（ORM 模型 / ``state.transition`` / ``ledger.append_event``）；
- 不触碰记账引擎、状态机、事件链的既有实现；
- 第三方新增接入 = 新增一条 JSON 规则，不改任何内核代码。

幂等
----
``{adapter}:{event_type}:{event_id}`` 作为 ``vouchers.idempotency_key``（唯一约束）。
重复投喂同一事件返回 ``replayed=True``，不会重复入账——这是外部系统重试友好的前提。

可追溯
------
每条消费都追加 ``adapter.event.consumed`` 事件，payload 保留来源事件 id 与生成的分录，
可在审计链上回放「哪个外部事件生成了哪张凭证」。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.adapters.registry import RuleNotFoundError, get_rule
from kernel.adapters.spec import (
    ZERO,
    EventFieldError,
    RuleError,
    get_field,
    render_summary,
    resolve_amount,
)
from kernel.db.models import Account, Period, Voucher, VoucherLine
from kernel.events import E
from kernel.ledger import append_event
from kernel.state import transition

# 单条规则允许的最大分录数，防止规则配置失控
MAX_LINES = 40


class AdapterError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _idempotency_key(adapter: str, event_type: str, event_id: str) -> str:
    return f"{adapter}:{event_type}:{event_id}"[:64]


def _resolve_event_id(event: dict, explicit: str | None) -> str:
    """确定外部事件标识：显式传入 > 事件自带 event_id > 事件内容哈希。

    内容哈希兜底让「不带 id 的事件」同样具备幂等性——相同内容重复投喂只入账一次。
    """
    if explicit:
        return str(explicit)
    if "event_id" in event:
        return str(event["event_id"])
    payload = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
    return "h:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse_date(raw: Any, field: str) -> date:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    raise EventFieldError("BAD_DATE", f"字段 {field} 不是合法日期：{raw!r}")


def _pick_period(session: Session, ledger_set_id: str, d: date) -> Period:
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == d.year,
            Period.month == d.month,
        )
    ).first()
    if period is None:
        raise AdapterError(
            "PERIOD_NOT_FOUND",
            f"账套不存在 {d.year}-{d.month:02d} 期间，请先开账",
            {"year": d.year, "month": d.month},
        )
    return period


def _next_voucher_no(session: Session, ledger_set_id: str) -> str:
    existing = session.scalars(
        select(Voucher.voucher_no).where(Voucher.ledger_set_id == ledger_set_id)
    ).all()
    seq = 0
    for no in existing:
        if no.startswith("记-") and no[2:].isdigit():
            seq = max(seq, int(no[2:]))
    return f"记-{seq + 1:04d}"


def _resolve_account(spec: dict, event: dict) -> str:
    """解析行科目：静态 account，或按事件字段查 account_map（未命中走 default）。"""
    if spec.get("account"):
        return str(spec["account"])
    value = str(get_field(event, spec["account_from"]))
    code = spec["account_map"].get(value) or spec.get("default_account")
    if not code:
        raise AdapterError(
            "ACCOUNT_MAP_MISS",
            f"科目映射未命中且无默认科目：{spec['account_from']}={value!r}",
            {"value": value, "known": sorted(spec["account_map"])},
        )
    return str(code)


def build_lines(
    session: Session, ledger_set_id: str, rule: dict, event: dict
) -> list[VoucherLine]:
    """按规则把事件翻译成分录行（金额已量化到分）。"""
    lines = rule["lines"]
    if len(lines) > MAX_LINES:
        raise RuleError("TOO_MANY_LINES", f"分录数超过上限 {MAX_LINES}")

    codes = {_resolve_account(ln, event) for ln in lines}
    accounts = {
        a.code: a
        for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id, Account.code.in_(codes)
            )
        ).all()
    }
    missing = codes - accounts.keys()
    if missing:
        raise AdapterError(
            "ACCOUNT_NOT_FOUND",
            f"账套缺少科目：{'、'.join(sorted(missing))}",
            {"missing": sorted(missing)},
        )

    # 适配器比内核更严格：只允许记到叶子科目。
    # 内核记账引擎目前不校验 is_leaf（父/子科目混用会让明细账分散到两级），
    # 外部事件自动入账必须落在最明细科目上，否则无人能发现。
    resolved_codes = [_resolve_account(ln, event) for ln in lines]
    non_leaf = sorted(
        code for code in resolved_codes if not accounts[code].is_leaf
    )
    if non_leaf:
        raise AdapterError(
            "ACCOUNT_NOT_LEAF",
            f"科目不是最明细科目（不允许自动入账）：{'、'.join(non_leaf)}",
            {"non_leaf": non_leaf},
        )

    out: list[VoucherLine] = []
    total_debit = ZERO
    total_credit = ZERO
    for idx, spec in enumerate(lines, start=1):
        amount = resolve_amount(spec["amount"], event)
        if amount < ZERO:
            raise AdapterError(
                "NEGATIVE_AMOUNT",
                f"第 {idx} 条分录金额为负：{amount}",
                {"line_no": idx, "account": spec["account"]},
            )
        is_debit = spec["side"] == "debit"
        if is_debit:
            total_debit += amount
        else:
            total_credit += amount
        aux = _resolve_aux_dims(spec, accounts[_resolve_account(spec, event)], event, idx)
        out.append(
            VoucherLine(
                line_no=idx,
                account_id=accounts[_resolve_account(spec, event)].id,
                debit=amount if is_debit else ZERO,
                credit=ZERO if is_debit else amount,
                aux_dims=aux,
            )
        )

    if total_debit != total_credit:
        raise AdapterError(
            "ADAPTER_UNBALANCED",
            f"规则生成的凭证不平衡：借 {total_debit} ≠ 贷 {total_credit}",
            {"debit": str(total_debit), "credit": str(total_credit)},
        )
    return out


def _resolve_aux_dims(
    spec: dict, account: Account, event: dict, line_idx: int
) -> dict | None:
    """把规则的 partner 配置解析成 aux_dims。

    校验两层：科目声明支持该维度（aux_dim_defs）、事件确实提供了往来单位。
    往来明细不建子科目、只挂维度——这是 Ontology 设计的核心约束。
    """
    partner = spec.get("partner")
    if partner is None:
        return None
    dim = str(partner["dim"])
    declared = set(account.aux_dim_defs or [])
    if dim not in declared:
        raise AdapterError(
            "DIM_NOT_DECLARED",
            f"科目 {account.code} 未声明辅助维度 {dim}"
            f"（该科目支持：{'、'.join(sorted(declared)) or '无'}）",
            {"account": account.code, "dim": dim, "declared": sorted(declared)},
        )
    value = get_field(event, partner["from"])
    if not isinstance(value, str) or not value.strip():
        raise EventFieldError(
            "BAD_PARTNER_VALUE",
            f"第 {line_idx} 条分录往来单位取值非法：{value!r}",
            {"field": partner["from"]},
        )
    return {dim: value.strip()}


def ingest_event(
    session: Session,
    *,
    ledger_set_id: str,
    adapter: str,
    event_type: str,
    event: dict,
    actor: dict,
    event_id: str | None = None,
) -> dict:
    """消费一个外部业务事件，按规则生成凭证。

    返回 ``{"voucher": {...}, "replayed": bool, "event_id": str, "lines": [...]}``。
    凭证状态由规则的 ``target_status`` 决定（默认 PUSHED，待人审）。
    """
    if not isinstance(event, dict):
        raise AdapterError("BAD_EVENT", "event 必须是对象")

    rule = get_rule(adapter, event_type)
    if rule is None:
        raise RuleNotFoundError(adapter, event_type)

    ext_id = _resolve_event_id(event, event_id)
    idem = _idempotency_key(adapter, event_type, ext_id)

    prior = session.scalars(
        select(Voucher).where(Voucher.idempotency_key == idem)
    ).first()
    if prior is not None:
        return {
            "voucher": {"id": prior.id, "voucher_no": prior.voucher_no,
                        "status": prior.status},
            "replayed": True,
            "event_id": ext_id,
            "lines": [],
        }

    d = _parse_date(get_field(event, rule["date_field"]), rule["date_field"])
    period = _pick_period(session, ledger_set_id, d)
    lines = build_lines(session, ledger_set_id, rule, event)
    summary = render_summary(rule.get("summary", ""), event)

    voucher = Voucher(
        ledger_set_id=ledger_set_id,
        period_id=period.id,
        voucher_no=_next_voucher_no(session, ledger_set_id),
        voucher_date=d,
        status="DRAFT",
        summary=summary,
        created_by=str(actor.get("id") or ""),
        idempotency_key=idem,
        lines=lines,
    )
    session.add(voucher)
    session.flush()

    target = rule.get("target_status", "PUSHED")
    if target == "PUSHED":
        transition(session, voucher_id=voucher.id, actor=actor, target="PUSHED")

    append_event(
        session,
        ledger_set_id=ledger_set_id,
        event_type=E.ADAPTER_EVENT_CONSUMED,
        aggregate_id=voucher.id,
        payload={
            "adapter": adapter,
            "event_type": event_type,
            "external_event_id": ext_id,
            "rule_version": rule["version"],
            "voucher_no": voucher.voucher_no,
            "lines": [
                {
                    "line_no": ln.line_no,
                    "debit": str(ln.debit),
                    "credit": str(ln.credit),
                }
                for ln in lines
            ],
        },
        actor=actor,
    )
    session.flush()

    return {
        "voucher": {
            "id": voucher.id,
            "voucher_no": voucher.voucher_no,
            "status": voucher.status,
            "summary": summary,
            "date": d.isoformat(),
        },
        "replayed": False,
        "event_id": ext_id,
        "lines": [
            {"line_no": ln.line_no, "debit": str(ln.debit), "credit": str(ln.credit)}
            for ln in lines
        ],
    }


def preview(rule: dict, event: dict) -> dict:
    """不落库地预览规则产出（供 UI/调试与规则编辑器使用）。"""
    lines = []
    total_debit = ZERO
    total_credit = ZERO
    for idx, spec in enumerate(rule["lines"], start=1):
        amount: Decimal = resolve_amount(spec["amount"], event)
        is_debit = spec["side"] == "debit"
        if is_debit:
            total_debit += amount
        else:
            total_credit += amount
        lines.append(
            {
                "line_no": idx,
                "account": spec["account"],
                "side": spec["side"],
                "amount": str(amount),
            }
        )
    return {
        "summary": render_summary(rule.get("summary", ""), event),
        "lines": lines,
        "debit": str(total_debit),
        "credit": str(total_credit),
        "balanced": total_debit == total_credit,
    }
