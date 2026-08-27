"""飞书长连接服务（方案 A 变体：lark-oapi WebSocket，本机无需公网回调）。

职责：
1. 绑定——用户给应用机器人发「绑定」，open_id 自动写入 .env
2. 卡片回调——批准/驳回按钮：写内核状态机；驳回意见入事件流（voucher.rejected）
3. 回执——处理结果 P2P 消息回发

运行: python scripts/feishu_ws.py （常驻；Ctrl+C 退出）
前提: 开放平台已开通「机器人」能力并可用范围=本人；im:message 权限
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp-server"))

import lark_oapi as lark  # noqa: E402
from ledgeros_mcp.feishu import get_tenant_access_token, load_env  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Voucher  # noqa: E402
from kernel.ledger import append_event  # noqa: E402
from kernel.posting import PostingError  # noqa: E402
from kernel.state import transition  # noqa: E402

ENV_PATH = REPO / ".env"
_engine = None


def repo_session() -> Session:
    """独立 engine 的 session 工厂（与 MCP server 同一演示库）。"""
    global _engine
    if _engine is None:
        import os

        url = os.environ.get("LEDGEROS_DB") or f"sqlite:///{REPO / 'ledgeros_dev.db'}"
        _engine = create_engine(url)
        Base.metadata.create_all(_engine)
    return Session(_engine)


def _reply(open_id: str, message_id: str | None, text: str) -> None:
    token = get_tenant_access_token()
    if message_id:
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        body = {"content": json.dumps({"text": text}, ensure_ascii=False)}
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


def handle_card_action(value: dict, operator_open_id: str) -> str:
    """卡片按钮 → 内核状态机；返回回执文案。"""
    act = value.get("action")
    voucher_id = value.get("voucher_id")
    actor = {"type": "user", "id": operator_open_id}
    if act not in ("approve", "reject") or not voucher_id:
        return "未知操作"
    try:
        with repo_session() as s:
            if act == "approve":
                transition(s, voucher_id=voucher_id, actor=actor, target="APPROVED")
                s.commit()
                return "✅ 已批准（审批人身份已记入审计链）"
            reason = str(value.get("reason") or "（卡片驳回，未填意见）")
            voucher = s.get(Voucher, voucher_id)
            if voucher is None or voucher.status != "PUSHED":
                return "❌ 仅待审（PUSHED）凭证可驳回"
            from_status = voucher.status
            voucher.status = "DRAFT"
            append_event(
                s,
                ledger_set_id=voucher.ledger_set_id,
                event_type="voucher.rejected",
                aggregate_id=voucher.id,
                payload={
                    "voucher_no": voucher.voucher_no,
                    "from": from_status,
                    "to": "DRAFT",
                    "reason": reason,
                },
                actor=actor,
            )
            s.commit()
            return f"↩️ 已驳回并退回草稿。意见：{reason}（已入审计链）"
    except PostingError as e:
        return f"❌ {e.message_zh}"


def _on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        open_id = data.event.sender.sender_id.open_id
        if msg.message_type != "text":
            return
        text = (json.loads(msg.content).get("text") or "").strip()
        if text == "绑定":
            _save_env_key("FEISHU_RECEIVE_OPEN_ID", open_id)
            _reply(open_id, msg.message_id, "已绑定当前飞书账号为审批接收人 ✅（open_id 已写入 .env）")
        else:
            _reply(open_id, msg.message_id, "我是 LedgerOS 审批机器人，发送「绑定」完成关联。")
    except Exception as e:  # noqa: BLE001
        print(f"[feishu_ws] message 异常: {e}")


def _on_card(data) -> str:
    try:
        value = dict(getattr(data.event.action, "value", None) or {})
        operator = getattr(getattr(data.event, "operator", None), "open_id", "") or "feishu-user"
        return handle_card_action(value, operator)
    except Exception as e:  # noqa: BLE001
        print(f"[feishu_ws] card 异常: {e}")
        return f"处理异常: {e}"


class _Dispatch:
    """lark-oapi ws 按 do_xxx 命名回调；未覆盖事件静默忽略。"""

    def do_p2_im_message_receive_v1(self, data: P2ImMessageReceiveV1) -> None:
        _on_message(data)

    def do_p2_card_action_trigger(self, data) -> str:
        return _on_card(data)


def main() -> None:
    env = load_env()
    app_id = env.get("FEISHU_APP_ID")
    app_secret = env.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请先配置 .env")
        sys.exit(1)
    with repo_session() as s:
        Base.metadata.create_all(s.bind)
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=_Dispatch(),
        log_level=lark.LogLevel.INFO,
    )
    print("[feishu_ws] 长连接已启动：等待「绑定」消息与审批卡片按钮…")
    client.start()


if __name__ == "__main__":
    main()
