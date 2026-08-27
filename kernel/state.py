"""凭证状态跃迁原语与 HITL 门禁（ADR-004）。

正向：DRAFT → PUSHED → APPROVED（post 由 posting 负责，APPROVED→POSTED）
补偿：POSTED → DRAFT（cancel_post_voucher，仅未结账期间；追加事件不改历史）

门禁：
- Agent 不能审批凭证（AGENT_APPROVAL_FORBIDDEN）——审批必须由人执行
- Agent 撤销记账须 L3 自治等级（AUTONOMY_DENIED），人不受限
- 制单人与审批人不能是同一主体（NO_SELF_APPROVAL）
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import (
    Account,
    Balance,
    Period,
    Subject,
    Voucher,
    utcnow,
)
from kernel.ledger import append_event
from kernel.posting import PostingError, _dims_key

ALLOWED: dict[tuple[str, str], str] = {
    ("DRAFT", "PUSHED"): "voucher.pushed",
    ("PUSHED", "APPROVED"): "voucher.approved",
}


def _subject_type(session: Session, actor: dict) -> tuple[str, Subject | None]:
    sid = str(actor.get("id") or "")
    subject = session.get(Subject, sid)
    if subject is not None:
        return subject.type, subject
    return (actor.get("type") or "user"), subject


def transition(session: Session, *, voucher_id: str, actor: dict, target: str) -> Voucher:
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise PostingError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    allowed_type = ALLOWED.get((voucher.status, target))
    if allowed_type is None:
        raise PostingError(
            "INVALID_TRANSITION",
            f"不允许从 {voucher.status} 跃迁到 {target}",
            {"from": voucher.status, "to": target},
        )
    if target == "APPROVED":
        if str(actor.get("id")) == str(voucher.created_by):
            raise PostingError("NO_SELF_APPROVAL", "制单人与审批人不能是同一主体")
        actor_type, _ = _subject_type(session, actor)
        if actor_type == "agent":
            raise PostingError(
                "AGENT_APPROVAL_FORBIDDEN",
                "审批必须由人执行，Agent 不能审批凭证",
                {"agent_id": actor.get("id")},
            )

    from_status = voucher.status
    voucher.status = target
    append_event(
        session,
        ledger_set_id=voucher.ledger_set_id,
        event_type=allowed_type,
        aggregate_id=voucher.id,
        payload={
            "voucher_no": voucher.voucher_no,
            "from": from_status,
            "to": target,
            "occurred_at_hint": utcnow().isoformat(),
        },
        actor=actor,
    )
    session.flush()
    return voucher


def cancel_post_voucher(session: Session, *, voucher_id: str, actor: dict) -> Voucher:
    """撤销记账：POSTED → DRAFT（ADR-004 补偿事务）。

    - 仅未结账（OPEN）期间可撤销；已 CLOSED 一律拒绝
    - 追加 voucher.cancelled 事件，原 POSTED 事件不修改（append-only）
    - balances 投影同步回冲，归零行删除
    - Actor 为 Agent 时须 L3 自治等级；人为审批人/复核岗不受限
    """
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise PostingError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    if voucher.status != "POSTED":
        raise PostingError(
            "INVALID_TRANSITION",
            f"仅已记账（POSTED）凭证可以撤销，当前状态 {voucher.status}",
            {"status": voucher.status},
        )

    actor_type, subject = _subject_type(session, actor)
    if actor_type == "agent":
        level = subject.autonomy_level if subject is not None else 0
        if level < 3:
            raise PostingError(
                "AUTONOMY_DENIED",
                f"撤销记账需要 L3 自治等级，当前主体为 L{level}",
                {"agent_id": actor.get("id"), "level": level},
            )

    period = session.get(Period, voucher.period_id)
    if period is None or period.status != "OPEN":
        status = period.status if period else "MISSING"
        raise PostingError(
            "PERIOD_CLOSED",
            f"期间状态为 {status}，已结账期间不可撤销记账",
            {"period_status": status},
        )

    cmap = {
        a.id: a.code
        for a in session.scalars(
            select(Account).where(Account.id.in_([ln.account_id for ln in voucher.lines]))
        )
    }
    append_event(
        session,
        ledger_set_id=voucher.ledger_set_id,
        event_type="voucher.cancelled",
        aggregate_id=voucher.id,
        payload={
            "voucher_no": voucher.voucher_no,
            "reversal_of": "voucher.posted",
            "lines": [
                {
                    "account_code": cmap.get(ln.account_id, "?"),
                    "debit": str(ln.debit),
                    "credit": str(ln.credit),
                    "aux_dims": ln.aux_dims or {},
                }
                for ln in voucher.lines
            ],
            "occurred_at_hint": utcnow().isoformat(),
        },
        actor=actor,
    )

    # 回冲余额投影（零余额行删除，保持投影紧凑）
    for line in list(voucher.lines):
        key = _dims_key(line.aux_dims)
        bal = session.scalars(
            select(Balance).where(
                Balance.ledger_set_id == voucher.ledger_set_id,
                Balance.period_id == voucher.period_id,
                Balance.account_id == line.account_id,
                Balance.dims_key == key,
            )
        ).first()
        if bal is None:
            continue
        bal.debit_total -= Decimal(str(line.debit))
        bal.credit_total -= Decimal(str(line.credit))
        if bal.debit_total == 0 and bal.credit_total == 0:
            session.delete(bal)

    voucher.status = "DRAFT"
    voucher.posted_at = None
    session.flush()
    return voucher
