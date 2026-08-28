"""飞书长连接服务（回复式审批，lark-oapi WebSocket EVENT 通道）。

重要约束（实测）：lark-oapi 的 ws 通道只分发 EVENT，卡片按钮 CARD 帧被客户端
丢弃（ws/client.py: `elif message_type == MessageType.CARD: return`，1.7.3 亦然），
因此审批采用「回复指令」模式：
    同意 记-0001            → PUSHED→APPROVED
    驳回 记-0001 差旅超标    → PUSHED→DRAFT，意见入 voucher.rejected 事件
    绑定                    → 记录 open_id 到 .env

运行: python scripts/feishu_ws.py （常驻；Ctrl+C 退出）
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp-server"))

import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from xerp_mcp.feishu import get_tenant_access_token, load_env  # noqa: E402

from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Voucher  # noqa: E402
from kernel.ledger import append_event  # noqa: E402
from kernel.posting import PostingError  # noqa: E402
from kernel.state import transition  # noqa: E402

ENV_PATH = REPO / ".env"
_engine = None
_RE_OK = re.compile(r"^(同意|批准)\s+(\S+)\s*$")
_RE_REJECT = re.compile(r"^驳回\s+(\S+)(?:\s+(.+))?$")


def repo_session() -> Session:
    global _engine
    if _engine is None:
        import os

        url = os.environ.get("XERP_DB") or f"sqlite:///{REPO / 'ledgeros_dev.db'}"
        _engine = create_engine(url)
        Base.metadata.create_all(_engine)
    return Session(_engine)


def _reply(open_id: str, message_id: str | None, text: str) -> None:
    token = get_tenant_access_token()
    if message_id:
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        body: dict = {"content": json.dumps({"text": text}, ensure_ascii=False)}
    else:
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        body = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        print(f"[feishu_ws] 回复发送失败: {e}")


def _save_env_key(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _find_by_no(s: Session, voucher_no: str) -> Voucher | None:
    return s.scalars(
        select(Voucher).where(Voucher.voucher_no == voucher_no)
    ).first()


def handle_text(open_id: str, text: str, message_id: str | None) -> None:
    """解析审批指令并驱动状态机；回执 P2P 发回。"""
    actor = {"type": "user", "id": open_id}
    text = text.strip()
    try:
        if text == "绑定":
            _save_env_key("FEISHU_RECEIVE_OPEN_ID", open_id)
            _reply(open_id, message_id, "已绑定当前飞书账号为审批接收人 ✅（open_id 已写入 .env）")
            return

        m = _RE_OK.match(text)
        if m:
            with repo_session() as s:
                v = _find_by_no(s, m.group(2))
                if v is None:
                    _reply(open_id, message_id, f"❌ 未找到凭证 {m.group(2)}")
                    return
                transition(s, voucher_id=v.id, actor=actor, target="APPROVED")
                s.commit()
                _reply(
                    open_id,
                    message_id,
                    f"✅ 已批准 {m.group(2)}（审批人身份已入审计链），可执行过账",
                )
            return

        m = _RE_REJECT.match(text)
        if m:
            voucher_no, reason = m.group(1), (m.group(2) or "（未填意见）").strip()
            with repo_session() as s:
                v = _find_by_no(s, voucher_no)
                if v is None:
                    _reply(open_id, message_id, f"❌ 未找到凭证 {voucher_no}")
                    return
                if v.status != "PUSHED":
                    _reply(open_id, message_id, f"❌ 仅待审（PUSHED）凭证可驳回，当前 {v.status}")
                    return
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
                _reply(
                    open_id,
                    message_id,
                    f"↩️ 已驳回 {voucher_no} 并退回草稿。意见：{reason}（已入审计链）",
                )
            return

        if text in ("帮助", "help"):
            _reply(
                open_id,
                message_id,
                "XErp 审批指令：\n绑定 → 关联飞书账号\n"
                "同意 凭证号 → 批准\n驳回 凭证号 意见 → 驳回",
            )
            return

        _reply(
            open_id,
            message_id,
            "未识别指令。可用：绑定 / 同意 凭证号 / 驳回 凭证号 意见 / 帮助",
        )
    except PostingError as e:
        _reply(open_id, message_id, f"❌ {e.message_zh}")
    except Exception as e:  # noqa: BLE001
        print(f"[feishu_ws] 处理异常: {e}")
        _reply(open_id, message_id, f"❌ 处理异常：{e}")


def _on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        open_id = data.event.sender.sender_id.open_id
        if msg.message_type != "text":
            return
        text = (json.loads(msg.content).get("text") or "").strip()
        handle_text(open_id, text, msg.message_id)
    except Exception as e:  # noqa: BLE001
        print(f"[feishu_ws] message 异常: {e}")


def _on_bot_added(data) -> None:
    """机器人被拉进群：记录群 id 并打招呼（群内审批入口）。"""
    try:
        chat_id = data.event.chat_id
        _save_env_key("FEISHU_LAST_CHAT_ID", chat_id)
        _reply(chat_id, None, "XErp 已入群 ✅ 回复「帮助」查看审批指令")
    except Exception as e:  # noqa: BLE001
        print(f"[feishu_ws] bot_added 异常: {e}")


def main() -> None:
    env = load_env()
    app_id = env.get("FEISHU_APP_ID")
    app_secret = env.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请先配置 .env")
        sys.exit(1)
    with repo_session() as s:
        Base.metadata.create_all(s.bind)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_im_chat_member_bot_added_v1(_on_bot_added)
        .build()
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    print("[feishu_ws] 长连接已启动（回复式审批）：绑定 / 同意 凭证号 / 驳回 凭证号 意见")
    client.start()


if __name__ == "__main__":
    main()
