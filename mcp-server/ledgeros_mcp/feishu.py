"""飞书集成（P0-11）：tenant_access_token 管理 + 审批卡片构建与推送。

长连接事件/卡片回调见 scripts/feishu_ws.py（lark-oapi WebSocket，无需公网回调）。
凭据从仓库根 .env 读取（不入库）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

_cache: dict = {"token": None, "expire_at": 0.0}


def load_env() -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE，# 注释）。"""
    env_path = _REPO_ROOT / ".env"
    out: dict[str, str] = {}
    if env_path.exists():
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip()
    return out


class FeishuError(RuntimeError):
    """飞书 API 失败（message_zh 可直接展示）。"""


def get_tenant_access_token(force: bool = False) -> str:
    if not force and _cache["token"] and time.time() < _cache["expire_at"]:
        return _cache["token"]
    env = load_env()
    app_id = os.environ.get("FEISHU_APP_ID") or env.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or env.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise FeishuError("缺少飞书凭据：请在仓库根 .env 配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise FeishuError(
            f"获取 tenant_access_token 失败: {data.get('msg')} (code={data.get('code')})"
        )
    _cache["token"] = data["tenant_access_token"]
    _cache["expire_at"] = time.time() + int(data.get("expire", 7200)) - 300
    return _cache["token"]


def build_approval_card(*, voucher_no: str, status: str, summary: str,
                        lines: list[dict], voucher_id: str) -> dict:
    """审批卡片蓝图：分录明细 + 批准/驳回按钮（value 回传给卡片回调）。"""
    entries = "\n".join(
        f"{ln['account_code']} {ln['account_name']}　"
        f"借 {ln['debit']}　贷 {ln['credit']}"
        for ln in lines
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"LedgerOS 审批请求 · {voucher_no}"},
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**凭证号**\n{voucher_no}"},
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**状态**\n{status}"},
                    },
                ],
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**摘要** {summary or '（无）'}"},
            },
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": entries}},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "type": "primary",
                        "value": {"action": "approve", "voucher_id": voucher_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "驳回"},
                        "type": "danger",
                        "value": {"action": "reject", "voucher_id": voucher_id},
                    },
                ],
            },
        ],
    }


def send_card(*, receive_id_type: str, receive_id: str, card: dict) -> dict:
    """推送交互卡片。receive_id_type: open_id | user_id | union_id | chat_id | email。"""
    token = get_tenant_access_token()
    body = json.dumps(
        {"receive_id": receive_id, "msg_type": "interactive",
         "content": json.dumps(card, ensure_ascii=False)}
    ).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    req = urllib.request.Request(
        f"{_MSG_URL}?receive_id_type={receive_id_type}", data=body, headers=headers
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise FeishuError(f"卡片发送失败: {data.get('msg')} (code={data.get('code')})")
    return data
