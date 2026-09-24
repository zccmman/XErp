"""WB 原生审批闭环内核助手（P0-4 / P0-3）。

设计铁律（见 XErp-设计-WB审批闭环.md）：
- 审批终态只允许走 kernel.state.transition；本模块只做「通知 + 身份解析 + 路由留痕」，
  绝不自己改 Voucher.status。
- WB 通道 = 通知层：把 PENDING 凭证的审批请求投到 WB 项目协作（tag 审批人 + 深链 +
  批准/驳回动词）；审批人在 WB 内回复后，智能体把「WB 成员」解析为 XErp 人主体，
  再调 approve_voucher/reject_voucher 回流内核（内核守卫自动派生 actor.type，红线复用）。
- 本模块零 WorkBuddy 依赖：只 import kernel 内部模块，可独立跑（L0 逃生舱同一套）。

「外部通道 → 内核人主体」解析：
- Subject.external_ref 存 WB user id / 飞书 open_id / 企微 userid；
- 解析顺序：wb_member_ref 命中 external_ref → fallback 候选（reviewer 在前、admin 在后）
  → 都不中抛 NO_REVIEWER。真正的「制单≠审批 / Agent 禁批」红线由 state.transition 落账时执行。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Subject, Voucher, utcnow
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import PostingError

# WB 原生审批深链协议（约定，具体解析由 WB 宿主完成；内核只产出占位）
VOUCHER_DEEPLINK_TMPL = "xerp://voucher/{voucher_id}"

__all__ = [
    "bind_subject_external_ref",
    "resolve_reviewer",
    "build_approval_notice",
    "route_to_workbuddy",
    "VOUCHER_DEEPLINK_TMPL",
]


def bind_subject_external_ref(
    session: Session, *, subject_id: str, external_ref: str
) -> Subject:
    """绑定外部通道身份键（WB user id / 飞书 open_id / 企微 userid）到内核人主体。

    幂等：已绑定同值直接返回；换值覆盖。仅作解析键，不参与授权判定。
    """
    subj = session.get(Subject, subject_id)
    if subj is None:
        raise PostingError("SUBJECT_NOT_FOUND", f"主体 {subject_id} 不存在")
    subj.external_ref = external_ref or ""
    session.flush()
    return subj


def resolve_reviewer(
    session: Session,
    *,
    ledger_set_id: str,
    wb_member_ref: str | None = None,
    fallback_subject_ids: list[str] | None = None,
) -> Subject:
    """解析「该账套的审批人主体」（P0-4 缺省策略）。

    优先级：
    1. wb_member_ref 命中某 Subject.external_ref（WB 通道手动指定审批人）；
    2. fallback_subject_ids 有序候选（调用方传入：reviewer 在前、admin 在后，
       由集成层经 authz.list_role_members 构造）；
    3. 都不中 → 抛 NO_REVIEWER。

    本函数不直接读 casbin（保持内核极简 + L0 直连可用）；真正的角色/红线校验由
    state.transition 在落账时执行并复用。
    """
    if wb_member_ref:
        hit = session.scalars(
            select(Subject).where(Subject.external_ref == wb_member_ref)
        ).first()
        if hit is not None:
            return hit
    for sid in (fallback_subject_ids or []):
        subj = session.get(Subject, sid)
        if subj is not None:
            return subj
    raise PostingError(
        "NO_REVIEWER",
        "未指定审批人且账套无兜底身份（reviewer/admin），无法路由审批",
        {"ledger_set_id": ledger_set_id},
    )


def build_approval_notice(
    session: Session, *, voucher: Voucher, reviewer: Subject
) -> dict:
    """生成 WB 审批通知摘要（只读，不改任何状态）。

    返回可直接序列化给 WB 通道渲染的结构：凭证头、分录、深链、批准/驳回动词占位。
    """
    line_ids = [ln.account_id for ln in voucher.lines]
    cmap = (
        {a.id: a for a in session.scalars(
            select(Account).where(Account.id.in_(line_ids))
        )}
        if line_ids
        else {}
    )
    lines = [
        {
            "account_code": cmap[ln.account_id].code if ln.account_id in cmap else "?",
            "account_name": cmap[ln.account_id].name if ln.account_id in cmap else "?",
            "debit": str(ln.debit),
            "credit": str(ln.credit),
        }
        for ln in voucher.lines
    ]
    return {
        "voucher_id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "summary": voucher.summary or "",
        "status": voucher.status,
        "reviewer_id": reviewer.id,
        "reviewer_name": reviewer.display_name,
        "deeplink": VOUCHER_DEEPLINK_TMPL.format(voucher_id=voucher.id),
        "lines": lines,
        "verbs": ["approve", "reject"],  # WB 通道渲染为批准/驳回按钮
    }


def route_to_workbuddy(
    session: Session,
    *,
    voucher_id: str,
    actor: dict,
    channel: str = "workbuddy",
    wb_member_ref: str | None = None,
    fallback_subject_ids: list[str] | None = None,
) -> dict:
    """把待审凭证经某外部通道送达审批人（通知层，不改 voucher.status）。

    动作：
    1. 校验 voucher 处于 PUSHED（仅待审可路由）；
    2. resolve_reviewer 解析审批人；
    3. 写 VOUCHER_ROUTED 事件留痕（append-only，记录经哪条通道、送达谁、external_ref）；
    4. 返回 {reviewer, notice, routed} 供 WB 通道展示。

    终态审批（批准/驳回）仍由审批人在 WB 内回复后，经 state.transition 回流内核执行——
    本函数绝不替审批人做决定，红线（NO_SELF_APPROVAL / AGENT_APPROVAL_FORBIDDEN）全部复用。
    """
    v = session.get(Voucher, voucher_id)
    if v is None:
        raise PostingError("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
    if v.status != "PUSHED":
        raise PostingError(
            "INVALID_TRANSITION",
            f"仅待审（PUSHED）凭证可路由审批，当前 {v.status}",
        )
    reviewer = resolve_reviewer(
        session,
        ledger_set_id=v.ledger_set_id,
        wb_member_ref=wb_member_ref,
        fallback_subject_ids=fallback_subject_ids,
    )
    notice = build_approval_notice(session, voucher=v, reviewer=reviewer)
    append_event(
        session,
        ledger_set_id=v.ledger_set_id,
        event_type=E.VOUCHER_ROUTED,
        aggregate_id=v.id,
        payload={
            "voucher_no": v.voucher_no,
            "channel": channel,
            "reviewer_id": reviewer.id,
            "reviewer_external_ref": reviewer.external_ref or "",
            "wb_member_ref": wb_member_ref or "",
            "occurred_at_hint": utcnow().isoformat(),
        },
        actor=actor,
    )
    session.flush()
    return {"reviewer": reviewer, "notice": notice, "routed": True}
