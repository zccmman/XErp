"""多级签字「签字位」原语（D7 本体债务修复）。

设计（见 docs/REVIEW-ontology.md D7）：签字位 = Subject × 角色 × 凭证 的签署事件。
现金凭证需出纳签字、可选主管签字——对一人公司无感，对多角色客户是内控硬需求。

语义契约（单一真源，禁止漂移）：
- 凭证可声明 ``required_signers``（角色列表）。空 / None = 传统单层审批
  （APPROVED 一跳，行为不变，已为所有既有测试/调用方覆盖）。
- 每个签字位由一位*人*主体签署：
    * Agent 不能签字——与「Agent 不能审批」同一铁律（ADR-004 门禁延伸）；
    * 签署人不能是制单人——「制单 ≠ 审批」延伸到签字。
- 全部 ``required_signers`` 签署 ``approved`` → 自动跃迁 APPROVED；
  任一签字位 ``rejected`` → 凭证退回 DRAFT（VOUCHER_REJECTED，须填原因）。
- 签字落 VOUCHER_SIGNED 事件（append-only）；状态机跃迁仍由 state.transition 统一守护，
  因此 NO_SELF_APPROVAL / AGENT_APPROVAL_FORBIDDEN / PENDING_SIGNATURES 等门禁全部复用。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from kernel.db.models import Subject, Voucher
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import PostingError
from kernel.state import transition

# 角色常量（与调用方约定的角色名；多角色客户可在配置侧扩展更多角色）
SIGN_ROLE_CASHIER = "cashier"  # 出纳
SIGN_ROLE_MANAGER = "manager"  # 主管


def _subject_type(session: Session, actor: dict) -> tuple[str, Subject | None]:
    sid = str(actor.get("id") or "")
    subject = session.get(Subject, sid)
    if subject is not None:
        return subject.type, subject
    return (actor.get("type") or "user"), subject


def pending_signers(voucher: Voucher) -> list[str]:
    """尚未获得 approved 签字的角色列表（驱动状态机守卫与 UI 提示）。"""
    required = list(voucher.required_signers or [])
    if not required:
        return []
    approved = {
        sig.get("slot")
        for sig in (voucher.signatures or [])
        if sig.get("decision") == "approved"
    }
    return [r for r in required if r not in approved]


def signing_status(voucher: Voucher) -> dict:
    """签字进度快照（供 MCP/Web/审批卡片展示）。"""
    sigs = list(voucher.signatures or [])
    approved = [s for s in sigs if s.get("decision") == "approved"]
    return {
        "is_multilevel": bool(voucher.required_signers),
        "required": list(voucher.required_signers or []),
        "signed": [s.get("slot") for s in sigs],
        "approved": [s.get("slot") for s in approved],
        "pending": pending_signers(voucher),
    }


def sign_voucher(
    session: Session,
    *,
    voucher_id: str,
    slot: str,
    actor: dict,
    decision: str = "approved",
    reason: str = "",
) -> Voucher:
    """签署一个签字位（D7 核心动作）。

    decision: ``"approved"`` | ``"rejected"``。
    - 全部 required_signers 签署 approved → 自动 APPROVED（复用 state.transition）；
    - 任一 rejected → 凭证退回 DRAFT（VOUCHER_REJECTED，reason 必填）。

    抛 PostingError：VOUCHER_NOT_FOUND / NO_SIGN_SLOTS / SIGN_SLOT_UNKNOWN /
    INVALID_TRANSITION / AGENT_APPROVAL_FORBIDDEN / NO_SELF_APPROVAL /
    SLOT_ALREADY_SIGNED / SIGN_DECISION_INVALID。
    """
    if decision not in ("approved", "rejected"):
        raise PostingError("SIGN_DECISION_INVALID", f"签字决定非法: {decision!r}")

    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise PostingError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")

    required = list(voucher.required_signers or [])
    if not required:
        raise PostingError(
            "NO_SIGN_SLOTS",
            "该凭证未声明签字位（required_signers 为空），请改用 approve_voucher 单层审批",
        )
    if slot not in required:
        raise PostingError(
            "SIGN_SLOT_UNKNOWN",
            f"签字位 {slot!r} 不在该凭证要求的签字位内",
            {"required": required},
        )
    if voucher.status != "PUSHED":
        raise PostingError(
            "INVALID_TRANSITION",
            f"仅待审（PUSHED）凭证可签字，当前 {voucher.status}",
            {"status": voucher.status},
        )

    # 铁律：Agent 不能签字；签署人不能是制单人
    actor_type, subject = _subject_type(session, actor)
    if actor_type == "agent":
        raise PostingError(
            "AGENT_APPROVAL_FORBIDDEN",
            "签字必须由人执行，Agent 不能签署凭证",
            {"agent_id": actor.get("id")},
        )
    if str(actor.get("id")) == str(voucher.created_by):
        raise PostingError("NO_SELF_APPROVAL", "制单人与签字人不能是同一主体")
    signer_name = subject.display_name if subject is not None else str(actor.get("id"))

    # 该签字位是否已被签署——不允许覆盖/重复
    existing = [s for s in (voucher.signatures or []) if s.get("slot") == slot]
    if existing:
        prior = existing[0]
        if (
            prior.get("decision") == "approved"
            and decision == "approved"
            and prior.get("signer_id") == str(actor.get("id"))
        ):
            # 同一人重复签署同一 approved 位：幂等放行，不重复落事件
            return voucher
        raise PostingError(
            "SLOT_ALREADY_SIGNED",
            f"签字位 {slot!r} 已被签署，不能重复签署",
            {"slot": slot},
        )

    record = {
        "slot": slot,
        "signer_id": str(actor.get("id")),
        "signer_name": str(signer_name),
        "signed_at": _now_iso(),
        "decision": decision,
    }
    if decision == "rejected":
        record["reason"] = (reason or "").strip() or "签字驳回"
    voucher.signatures = list(voucher.signatures or []) + [record]

    append_event(
        session,
        ledger_set_id=voucher.ledger_set_id,
        event_type=E.VOUCHER_SIGNED,
        aggregate_id=voucher.id,
        payload={
            "voucher_no": voucher.voucher_no,
            "slot": slot,
            "signer_id": record["signer_id"],
            "signer_name": record["signer_name"],
            "decision": decision,
            "reason": record.get("reason"),
            "occurred_at_hint": _now_iso(),
        },
        actor=actor,
    )

    if decision == "rejected":
        # 任一签字位驳回 → 退回制单人（state.transition 按非制单人判定为 VOUCHER_REJECTED）
        transition(
            session,
            voucher_id=voucher.id,
            actor=actor,
            target="DRAFT",
            reason=record.get("reason") or "签字驳回",
        )
        session.flush()
        return voucher

    # approved：是否全部签完 → 自动 APPROVED
    if not pending_signers(voucher):
        transition(
            session,
            voucher_id=voucher.id,
            actor=actor,
            target="APPROVED",
        )
    session.flush()
    return voucher


def _now_iso() -> str:
    from kernel.db.models import utcnow

    return utcnow().isoformat()
