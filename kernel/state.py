"""凭证状态跃迁原语（ADR-004）：DRAFT→PUSHED→APPROVED（post 由 posting 负责）。

P0-08 将扩展补偿事务（cancel_post）与自治额度门禁；本模块保持最小正确。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from kernel.db.models import Voucher, utcnow
from kernel.ledger import append_event
from kernel.posting import PostingError

ALLOWED: dict[tuple[str, str], str] = {
    ("DRAFT", "PUSHED"): "voucher.pushed",
    ("PUSHED", "APPROVED"): "voucher.approved",
}


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
    if target == "APPROVED" and str(actor.get("id")) == str(voucher.created_by):
        raise PostingError(
            "NO_SELF_APPROVAL", "制单人与审批人不能是同一主体"
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
