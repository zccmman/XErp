"""企业微信集成（P4-W1）：自建应用作为审核与交互端。

能力：
- access_token 缓存（corpid + corpsecret）
- 主动推送：text / markdown / 模板卡片（button_interaction，批准/驳回按钮）
- 按钮回调后更新卡片（message/update_template_card，消费回调 ResponseCode）
- 回调加解密：与官方 WXBizMsgCrypt 等价的实现
  （sha1 签名校验 + AES-256-CBC + PKCS7(block=32) + corp_id 尾部校验）

回调端点见 kernel/webapp.py 的 /wecom/callback（GET 验证 URL + POST 事件分发）。
凭据从仓库根 .env 读取（不入库）：
    WECOM_CORP_ID          企业 ID
    WECOM_CORP_SECRET      自建应用 Secret
    WECOM_AGENT_ID         自建应用 AgentId
    WECOM_TOKEN            回调配置 Token
    WECOM_ENCODING_AES_KEY 回调配置 EncodingAESKey（43 位）
    WECOM_RECEIVE_USER     「绑定」指令写入的审批接收人 userid
配置步骤与验收见 docs/WECOM.md。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
_CARD_UPDATE_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/update_template_card"

_TOKEN_TTL_SECONDS = 7200

_cache: dict = {"token": None, "expire_at": 0.0}


class WecomError(RuntimeError):
    """企业微信集成失败（message 可直接展示）。"""


def load_env() -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE，# 注释），与 xerp_mcp.feishu.load_env 一致。"""
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


def _cfg(key: str) -> str:
    v = os.environ.get(key) or load_env().get(key) or ""
    if not v:
        raise WecomError(f"缺少企微凭据：请在仓库根 .env 配置 {key}（见 docs/WECOM.md）")
    return v


def _agent_id() -> int:
    try:
        return int(_cfg("WECOM_AGENT_ID"))
    except ValueError as e:
        raise WecomError("WECOM_AGENT_ID 必须为整数") from e


# ---------- access token ----------

def get_access_token(force: bool = False) -> str:
    if not force and _cache["token"] and time.time() < _cache["expire_at"]:
        return _cache["token"]
    qs = urllib.parse.urlencode(
        {"corpid": _cfg("WECOM_CORP_ID"), "corpsecret": _cfg("WECOM_CORP_SECRET")}
    )
    req = urllib.request.Request(f"{_TOKEN_URL}?{qs}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("errcode") not in (0, None):
        raise WecomError(
            f"获取 access_token 失败: {data.get('errmsg')} (errcode={data.get('errcode')})"
        )
    _cache["token"] = data["access_token"]
    _cache["expire_at"] = time.time() + int(data.get("expires_in", _TOKEN_TTL_SECONDS)) - 300
    return _cache["token"]


def _post_api(url: str, payload: dict) -> dict:
    token = get_access_token()
    qs = urllib.parse.urlencode({"access_token": token})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url}?{qs}", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("errcode") not in (0, None):
        raise WecomError(f"企微 API 失败: {data.get('errmsg')} (errcode={data.get('errcode')})")
    return data


# ---------- 主动推送 ----------

def default_user() -> str:
    """绑定的审批接收人（「绑定」指令写入）。"""
    return _cfg("WECOM_RECEIVE_USER")


def send_text(user: str, content: str) -> dict:
    return _post_api(
        _SEND_URL,
        {
            "touser": user,
            "msgtype": "text",
            "agentid": _agent_id(),
            "text": {"content": content},
        },
    )


def send_markdown(user: str, content: str) -> dict:
    return _post_api(
        _SEND_URL,
        {
            "touser": user,
            "msgtype": "markdown",
            "agentid": _agent_id(),
            "markdown": {"content": content},
        },
    )


def build_approval_card(
    *, voucher_no: str, status: str, summary: str, lines: list[dict], voucher_id: str
) -> dict:
    """模板卡片（button_interaction）：批准/驳回按钮 key 回传给卡片回调。

    task_id 用 voucher_id——同一凭证重复推送会覆盖旧卡片，天然幂等。
    """
    return {
        "card_type": "button_interaction",
        "source": {"desc": "XErp 智能审批"},
        "main_title": {"title": f"审批请求 · {voucher_no}"},
        "sub_title_text": f"摘要 {summary or '（无）'} · 状态 {status}",
        "horizontal_content_list": [
            {"keyname": f"{ln['account_code']} {ln['account_name']}",
             "value": f"借 {ln['debit']} / 贷 {ln['credit']}"}
            for ln in lines
        ],
        "button_list": [
            {"text": "批准", "style": 2, "key": f"approve:{voucher_id}"},
            {"text": "驳回", "style": 3, "key": f"reject:{voucher_id}"},
        ],
        "task_id": voucher_id,
    }


def _finished_card(voucher_no: str, result_text: str, voucher_id: str) -> dict:
    """按钮处理完成后的卡片更新（保持 card_type 不变，按钮降级为已完成的 noop）。"""
    return {
        "card_type": "button_interaction",
        "source": {"desc": "XErp 智能审批"},
        "main_title": {"title": f"{result_text} · {voucher_no}"},
        "sub_title_text": "该凭证已处理，如需驳回请回复：驳回 凭证号 意见",
        "button_list": [
            {"text": "已处理 ✅", "style": 1, "key": f"noop:{voucher_id}"},
        ],
        "task_id": voucher_id,
    }


def update_card(user: str, response_code: str, voucher_id: str, result_text: str,
                voucher_no: str) -> dict:
    """按钮点击后整体替换同 task_id 卡片（消费回调 ResponseCode，一次性、72h 有效）。

    必须传 response_code（来自回调事件 <ResponseCode> 字段）——否则企微无法定位
    要更新的按钮，卡片不会变化、可被重复点击。
    """
    return _post_api(
        _CARD_UPDATE_URL,
        {
            "userids": [user],
            "agentid": _agent_id(),
            "response_code": response_code,
            "template_card": _finished_card(voucher_no, result_text, voucher_id),
        },
    )


def send_approval_card(user: str, card: dict) -> dict:
    return _post_api(
        _SEND_URL, {"touser": user, "msgtype": "template_card",
                    "agentid": _agent_id(), "template_card": card}
    )


def send_finished_card(user: str, voucher_no: str, result_text: str,
                       voucher_id: str) -> dict:
    """推送完成态展示卡片（无交互按钮，仅展示凭证最终处理结果，如已批准/已驳回）。

    与待审交互卡片（task_id=voucher_id）互补：企微 message/send 的 task_id 一旦发过
    即被永久占用（42014），且仅允许 数字/字母/_/-/@（冒号 : 非法，同样报 42014）。
    故完成态用独立 task_id ``finish{voucher_id}`` 规避。
    """
    card = _finished_card(voucher_no, result_text, voucher_id)
    card["task_id"] = f"finish{voucher_id}"
    return send_approval_card(user, card)


# ---------- 回调加解密（WXBizMsgCrypt 等价实现） ----------

_PKCS7_BLOCK = 32  # 企微约定块长 32 字节（非标准 16）


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """sha1(sort(token, timestamp, nonce, encrypt))。"""
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()


def verify_signature(token: str, msg_signature: str, timestamp: str, nonce: str,
                     encrypt: str) -> bool:
    return signature(token, timestamp, nonce, encrypt) == msg_signature


def _aes_key() -> bytes:
    key43 = _cfg("WECOM_ENCODING_AES_KEY")
    try:
        return base64.b64decode(key43 + "=")
    except Exception as e:  # noqa: BLE001
        raise WecomError("WECOM_ENCODING_AES_KEY 无效（应为 43 位 Base64）") from e


def _pkcs7_pad(data: bytes) -> bytes:
    pad = _PKCS7_BLOCK - len(data) % _PKCS7_BLOCK
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    pad = data[-1]
    if pad < 1 or pad > _PKCS7_BLOCK:
        raise WecomError("解密失败：PKCS7 填充非法")
    if data[-pad:] != bytes([pad]) * pad:
        raise WecomError("解密失败：PKCS7 填充非法")
    return data[:-pad]


def encrypt_message(plain: str, *, corp_id: str | None = None, aes_key: bytes | None = None) -> str:
    """明文 → AES-256-CBC 加密 → Base64。明文结构：16B 随机串 + 4B 网络序长度 + msg + corp_id。"""
    corp_id = corp_id or _cfg("WECOM_CORP_ID")
    aes_key = aes_key or _aes_key()
    msg = plain.encode("utf-8")
    raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + corp_id.encode("utf-8")
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    enc = cipher.encryptor()
    return base64.b64encode(enc.update(_pkcs7_pad(raw)) + enc.finalize()).decode()


def decrypt_message(encrypt_b64: str, *, corp_id: str | None = None,
                    aes_key: bytes | None = None) -> str:
    """Base64 密文 → 校验 corp_id → 返回明文。"""
    corp_id = corp_id or _cfg("WECOM_CORP_ID")
    aes_key = aes_key or _aes_key()
    try:
        ciphertext = base64.b64decode(encrypt_b64)
    except Exception as e:  # noqa: BLE001
        raise WecomError("回调密文不是合法 Base64") from e
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    dec = cipher.decryptor()
    raw = _pkcs7_unpad(dec.update(ciphertext) + dec.finalize())
    msg_len = struct.unpack(">I", raw[16:20])[0]
    msg = raw[20:20 + msg_len]
    if raw[20 + msg_len:].decode("utf-8", "replace") != corp_id:
        raise WecomError("解密失败：receiveid 与 WECOM_CORP_ID 不一致")
    return msg.decode("utf-8")


def parse_encrypt_xml(xml_body: bytes) -> str:
    """回调 POST body XML → Encrypt 字段。"""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as e:
        raise WecomError("回调 body 不是合法 XML") from e
    enc = root.findtext("Encrypt")
    if not enc:
        raise WecomError("回调 XML 缺少 Encrypt 字段")
    return enc


def build_reply_xml(reply_plain: str, *, token: str | None = None, timestamp: str | None = None,
                    nonce: str | None = None) -> str:
    """构造被动回复信封：明文回复 → 加密 → XML(Encrypt/MsgSignature/TimeStamp/Nonce)。"""
    token = token or _cfg("WECOM_TOKEN")
    nonce = nonce or os.urandom(8).hex()
    timestamp = timestamp or str(int(time.time()))
    encrypt = encrypt_message(reply_plain)
    sig = signature(token, timestamp, nonce, encrypt)
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )


def build_text_reply_xml(content: str, *, to_user: str, msg_id: int | None = None,
                         token: str | None = None, timestamp: str | None = None,
                         nonce: str | None = None) -> str:
    """被动回复一条文本消息（加密封装）。"""
    from_user = _cfg("WECOM_CORP_ID")
    msg_id = msg_id if msg_id is not None else int(time.time() * 1000) % (1 << 62)
    inner = (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{_agent_id()}</AgentID>"
        "</xml>"
    )
    return build_reply_xml(inner, token=token, timestamp=timestamp, nonce=nonce)


# ---------- 回调事件分发 ----------

def bind_user(user_id: str) -> None:
    """「绑定」指令：写 WECOM_RECEIVE_USER 到 .env。"""
    env_path = _REPO_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for i, ln in enumerate(lines):
        if ln.strip().startswith("WECOM_RECEIVE_USER="):
            lines[i] = f"WECOM_RECEIVE_USER={user_id}"
            break
    else:
        lines.append(f"WECOM_RECEIVE_USER={user_id}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def handle_text_command(s, content: str, from_user: str) -> str:
    """文本指令 → 渠道无关审批内核，返回回执文本。"""
    from kernel.approval_bot import handle_approval_command

    return handle_approval_command(
        s,
        content,
        actor={"type": "user", "id": from_user},
        channel_label="企业微信",
        channel_user_id=from_user,
        on_bind=bind_user,
    )


def handle_card_event(s, event_key: str, from_user: str) -> str:
    """模板卡片按钮回调：approve:{vid} / reject:{vid} / noop:{vid}。返回回执文本。

    状态机驱动与文本指令同一红线：仅 PUSHED 可批/驳，审批人身份入审计链。
    """
    from kernel.db.models import Voucher
    from kernel.ledger import append_event
    from kernel.state import transition

    if event_key.startswith("noop:"):
        return ""
    action, _, vid = event_key.partition(":")
    if action not in ("approve", "reject") or not vid:
        return f"❌ 未知按钮事件 {event_key}"
    actor = {"type": "user", "id": from_user}
    v = s.get(Voucher, vid)
    if v is None:
        return f"❌ 未找到凭证 {vid}"
    if action == "approve":
        if v.status != "PUSHED":
            return f"❌ 仅待审（PUSHED）凭证可批准，当前 {v.status}"
        transition(s, voucher_id=v.id, actor=actor, target="APPROVED")
        s.commit()
        return f"approved:{v.voucher_no}"
    # reject（卡片按钮无意见输入，意见固定入审计链，可随后用文本指令补充）
    if v.status != "PUSHED":
        return f"❌ 仅待审（PUSHED）凭证可驳回，当前 {v.status}"
    reason = "（企微卡片驳回，可用文本指令「驳回 凭证号 意见」补充）"
    v.status = "DRAFT"
    append_event(
        s,
        ledger_set_id=v.ledger_set_id,
        event_type="voucher.rejected",
        aggregate_id=v.id,
        payload={"voucher_no": v.voucher_no, "from": "PUSHED", "to": "DRAFT",
                 "reason": reason},
        actor=actor,
    )
    s.commit()
    return f"rejected:{v.voucher_no}"
