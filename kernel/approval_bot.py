"""渠道无关的审批指令内核（P4-W1：企微/飞书共用）。

从 scripts/feishu_ws.py 抽取状态机驱动逻辑，使审批意图不绑定任何 IM 渠道：
    同意|批准 <凭证号>      → PUSHED→APPROVED
    驳回 <凭证号> [意见]    → PUSHED→DRAFT，意见入 voucher.rejected 事件
    绑定                    → 回调 on_bind 记录接收人（各通道自行落库）
    帮助|help               → 指令说明

通道适配器（scripts/feishu_ws.py、kernel/webapp.py /wecom/callback）只负责：
收消息 → 调本函数 → 把回执文本发回。审计链 actor 由通道以真实用户标识传入。
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from kernel.db.models import Voucher  # noqa: F401  (类型标注)
from kernel.ledger import append_event
from kernel.state import transition

_RE_OK = re.compile(r"^(同意|批准)\s+(\S+)\s*$")
_RE_REJECT = re.compile(r"^驳回\s+(\S+)(?:\s+(.+))?$")


def find_by_no(s: Session, voucher_no: str) -> Voucher | None:
    return s.scalars(
        select_voucher_no(voucher_no)
    ).first()


def select_voucher_no(voucher_no: str):
    from sqlalchemy import select

    return select(Voucher).where(Voucher.voucher_no == voucher_no)


def handle_approval_command(
    s: Session,
    text: str,
    *,
    actor: dict,
    channel_label: str,
    channel_user_id: str,
    on_bind,
) -> str:
    """解析审批指令并驱动 HITL 状态机，返回回执文本。

    actor: {"type": "user", "id": <渠道用户标识>}，入审计链。
    on_bind(channel_user_id): 「绑定」指令回调，由通道决定落库位置（如 .env 键）。
    抛 PostingError 及其余异常由通道适配器决定如何展示。
    """
    text = text.strip()

    if text == "绑定":
        on_bind(channel_user_id)
        return f"已绑定当前{channel_label}账号为审批接收人 ✅（已写入 .env）"

    m = _RE_OK.match(text)
    if m:
        v = find_by_no(s, m.group(2))
        if v is None:
            return f"❌ 未找到凭证 {m.group(2)}"
        transition(s, voucher_id=v.id, actor=actor, target="APPROVED")
        s.commit()
        return f"✅ 已批准 {m.group(2)}（审批人身份已入审计链），可执行过账"

    m = _RE_REJECT.match(text)
    if m:
        voucher_no, reason = m.group(1), (m.group(2) or "（未填意见）").strip()
        v = find_by_no(s, voucher_no)
        if v is None:
            return f"❌ 未找到凭证 {voucher_no}"
        if v.status != "PUSHED":
            return f"❌ 仅待审（PUSHED）凭证可驳回，当前 {v.status}"
        from_status = v.status
        v.status = "DRAFT"
        append_event(
            s,
            ledger_set_id=v.ledger_set_id,
            event_type="voucher.rejected",
            aggregate_id=v.id,
            payload={
                "voucher_no": v.voucher_no,
                "from": from_status,
                "to": "DRAFT",
                "reason": reason,
            },
            actor=actor,
        )
        s.commit()
        return f"↩️ 已驳回 {voucher_no} 并退回草稿。意见：{reason}（已入审计链）"

    if text in ("帮助", "help"):
        return (
            "XErp 审批指令：\n绑定 → 关联当前渠道账号\n"
            "同意 凭证号 → 批准\n驳回 凭证号 意见 → 驳回"
        )

    return "未识别指令。可用：绑定 / 同意 凭证号 / 驳回 凭证号 意见 / 帮助"
