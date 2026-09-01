"""P4-W1 TDD：企业微信审核与交互端——回调加解密 + 审批指令内核 + 卡片事件。

离线测试：不访问企微 API；AES/签名均为纯本地运算。
"""

import base64
import os
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel import wecom
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Subject, Voucher, VoucherLine
from kernel.seed import seed_demo_ledger
from kernel.state import transition

CORP_ID = "wwtest_corp_id"
TOKEN = "test_token"
AES_KEY = base64.b64encode(os.urandom(32)).decode()[:43]  # 企微格式：43 位 Base64


@pytest.fixture()
def env_vars(monkeypatch):
    monkeypatch.setenv("WECOM_CORP_ID", CORP_ID)
    monkeypatch.setenv("WECOM_TOKEN", TOKEN)
    monkeypatch.setenv("WECOM_ENCODING_AES_KEY", AES_KEY)
    monkeypatch.setenv("WECOM_AGENT_ID", "1000002")


@pytest.fixture()
def ctx(tmp_path_factory):
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('wecom')}/w.db")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()

    user_actor = {"type": "user", "id": ids["subject_id"]}

    def make_pushed(voucher_no="记-9501"):
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=voucher_no,
            voucher_date=date(2026, 8, 31),
            status="DRAFT",
            summary="企微审批测试",
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("120.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("120.00")),
        ]
        s.add(v)
        s.flush()
        transition(s, voucher_id=v.id, actor=user_actor, target="PUSHED")
        s.commit()
        return s.get(Voucher, v.id)

    yield {"s": s, "ids": ids, "make_pushed": make_pushed}
    s.close()


# ---------- 回调加解密 ----------

def test_encrypt_decrypt_roundtrip():
    plain = "<xml><Content>你好</Content></xml>"
    key = base64.b64decode(AES_KEY + "=")
    enc = wecom.encrypt_message(plain, corp_id=CORP_ID, aes_key=key)
    assert wecom.decrypt_message(enc, corp_id=CORP_ID, aes_key=key) == plain


def test_decrypt_rejects_wrong_corp_id():
    enc = wecom.encrypt_message("x", corp_id=CORP_ID, aes_key=base64.b64decode(AES_KEY + "="))
    with pytest.raises(wecom.WecomError, match="receiveid"):
        wecom.decrypt_message(enc, corp_id="other_corp", aes_key=base64.b64decode(AES_KEY + "="))


def test_signature_verify():
    enc = wecom.encrypt_message("hello", corp_id=CORP_ID, aes_key=base64.b64decode(AES_KEY + "="))
    sig = wecom.signature(TOKEN, "123", "abc", enc)
    assert wecom.verify_signature(TOKEN, sig, "123", "abc", enc)
    assert not wecom.verify_signature(TOKEN, sig, "123", "abc", enc + "x")


def test_build_reply_xml_roundtrip(env_vars):
    xml_body = wecom.build_reply_xml("<inner/>", timestamp="1700000000", nonce="n1")
    root = ET.fromstring(xml_body)
    enc = root.findtext("Encrypt")
    assert root.findtext("MsgSignature") == wecom.signature(TOKEN, "1700000000", "n1", enc)
    assert wecom.decrypt_message(enc) == "<inner/>"


def test_build_text_reply_xml(env_vars):
    xml_body = wecom.build_text_reply_xml("✅ 已批准 记-9501", to_user="zhangsan")
    root = ET.fromstring(xml_body)
    inner = ET.fromstring(wecom.decrypt_message(root.findtext("Encrypt")))
    assert inner.findtext("Content") == "✅ 已批准 记-9501"
    assert inner.findtext("FromUserName") == CORP_ID
    assert inner.findtext("MsgType") == "text"


def test_parse_encrypt_xml():
    raw = b"<xml><Encrypt><![CDATA[abc123]]></Encrypt></xml>"
    assert wecom.parse_encrypt_xml(raw) == "abc123"
    with pytest.raises(wecom.WecomError, match="Encrypt"):
        wecom.parse_encrypt_xml(b"<xml></xml>")


# ---------- 审批卡片结构 ----------

def test_approval_card_structure():
    card = wecom.build_approval_card(
        voucher_no="记-9501", status="PUSHED", summary="企微审批测试",
        lines=[{"account_code": "6601", "account_name": "管理费用",
                "debit": "120.00", "credit": "0.00"}],
        voucher_id="vid-1",
    )
    assert card["card_type"] == "button_interaction"
    assert card["task_id"] == "vid-1"
    keys = [b["key"] for b in card["button_list"]]
    assert keys == ["approve:vid-1", "reject:vid-1"]


def test_send_finished_card_task_id(env_vars, monkeypatch):
    """完成态卡片用独立 task_id（finish{vid}，无冒号）——规避企微 42014。"""
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"errcode": 0, "msgid": "msg-1"}

    monkeypatch.setattr(wecom, "_post_api", fake_post)
    resp = wecom.send_finished_card("boss", "记-9501", "已批准 ✅", "vid-1")
    assert resp["msgid"] == "msg-1"
    card = captured["payload"]["template_card"]
    assert captured["payload"]["touser"] == "boss"
    assert card["task_id"] == "finishvid-1"
    assert ":" not in card["task_id"]  # 冒号是 task_id 非法字符
    assert card["button_list"][0]["key"] == "noop:vid-1"
    assert "已批准 ✅" in card["main_title"]["title"]


# ---------- 文本指令 → 渠道无关审批内核 ----------

def test_text_command_approve(ctx):
    v = ctx["make_pushed"]()
    reply = wecom.handle_text_command(ctx["s"], f"同意 {v.voucher_no}", "wecom_boss")
    assert "已批准" in reply
    assert ctx["s"].get(Voucher, v.id).status == "APPROVED"


def test_text_command_reject_with_reason(ctx):
    v = ctx["make_pushed"]()
    reply = wecom.handle_text_command(ctx["s"], f"驳回 {v.voucher_no} 差旅超标", "wecom_boss")
    assert "已驳回" in reply and "差旅超标" in reply
    assert ctx["s"].get(Voucher, v.id).status == "DRAFT"


def test_text_command_bind_writes_env_key(ctx, monkeypatch):
    saved = {}
    monkeypatch.setattr(wecom, "bind_user", lambda uid: saved.update(uid=uid))
    reply = wecom.handle_text_command(ctx["s"], "绑定", "wecom_boss")
    assert "已绑定" in reply and "企业微信" in reply
    assert saved["uid"] == "wecom_boss"


def test_text_command_help(ctx):
    assert "同意 凭证号" in wecom.handle_text_command(ctx["s"], "帮助", "u")


# ---------- 卡片按钮事件 → 状态机 ----------

def test_card_event_approve(ctx):
    v = ctx["make_pushed"]()
    result = wecom.handle_card_event(ctx["s"], f"approve:{v.id}", "wecom_boss")
    assert result == f"approved:{v.voucher_no}"
    assert ctx["s"].get(Voucher, v.id).status == "APPROVED"


def test_card_event_reject(ctx):
    v = ctx["make_pushed"]()
    result = wecom.handle_card_event(ctx["s"], f"reject:{v.id}", "wecom_boss")
    assert result == f"rejected:{v.voucher_no}"
    assert ctx["s"].get(Voucher, v.id).status == "DRAFT"


def test_card_event_reject_twice_fails(ctx):
    v = ctx["make_pushed"]()
    wecom.handle_card_event(ctx["s"], f"reject:{v.id}", "wecom_boss")
    result = wecom.handle_card_event(ctx["s"], f"reject:{v.id}", "wecom_boss")
    assert "仅待审" in result


def test_card_event_noop_and_unknown(ctx):
    v = ctx["make_pushed"]()
    assert wecom.handle_card_event(ctx["s"], f"noop:{v.id}", "u") == ""
    assert "未知按钮" in wecom.handle_card_event(ctx["s"], "haha", "u")


def test_update_card_sends_response_code(env_vars, monkeypatch):
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"errcode": 0}

    monkeypatch.setattr(wecom, "_post_api", fake_post)
    wecom.update_card("boss", "resp-code-123", "vid-1", "已批准 ✅", "记-9501")
    assert "update_template_card" in captured["url"]
    assert captured["payload"]["response_code"] == "resp-code-123"
    assert captured["payload"]["agentid"] == 1000002
    card = captured["payload"]["template_card"]
    assert card["button_list"][0]["key"] == "noop:vid-1"
    assert "已批准 ✅" in card["main_title"]["title"]


# ---------- 回调端点（GET 验证 + POST 分发） ----------

@pytest.fixture()
def client(env_vars, ctx):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    return TestClient(build_app(str(ctx["s"].bind.url)))


def _encrypted(plain: str) -> tuple[str, str, str, str]:
    """返回 (encrypt, msg_signature, timestamp, nonce)。"""
    ts, nonce = "1700000000", "nonce1"
    enc = wecom.encrypt_message(plain)
    sig = wecom.signature(TOKEN, ts, nonce, enc)
    return enc, sig, ts, nonce


def test_callback_url_verify(client):
    echostr, sig, ts, nonce = _encrypted("echo-plain-1234")
    r = client.get(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echostr},
    )
    assert r.status_code == 200
    assert r.text == "echo-plain-1234"


def test_callback_text_command_end_to_end(ctx, client):
    v = ctx["make_pushed"]()
    inner = (
        "<xml><ToUserName><![CDATA[x]]></ToUserName>"
        f"<FromUserName><![CDATA[wecom_boss]]></FromUserName>"
        "<CreateTime>1</CreateTime><MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[同意 {v.voucher_no}]]></Content>"
        "<MsgId>1</MsgId><AgentID>1000002</AgentID></xml>"
    )
    enc, sig, ts, nonce = _encrypted(inner)
    r = client.post(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        content=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode(),
    )
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    reply = ET.fromstring(wecom.decrypt_message(root.findtext("Encrypt")))
    assert "已批准" in reply.findtext("Content")
    ctx["s"].rollback()  # 丢弃本会话快照，读取 webapp 会话提交的最新状态
    assert ctx["s"].get(Voucher, v.id).status == "APPROVED"


def test_callback_card_event_updates_card(ctx, client, monkeypatch):
    v = ctx["make_pushed"]()
    calls = {}

    def fake_update(user, response_code, vid, result_text, voucher_no):
        calls.update(user=user, response_code=response_code, vid=vid,
                     result_text=result_text, voucher_no=voucher_no)
        return {"errcode": 0}

    monkeypatch.setattr(wecom, "update_card", fake_update)
    inner = (
        "<xml>"
        "<FromUserName><![CDATA[wecom_boss]]></FromUserName>"
        "<CreateTime>1</CreateTime><MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[template_card_event]]></Event>"
        f"<EventKey><![CDATA[approve:{v.id}]]></EventKey>"
        "<TaskId><![CDATA[task-x]]></TaskId>"
        "<CardType><![CDATA[button_interaction]]></CardType>"
        "<ResponseCode><![CDATA[resp-code-xyz]]></ResponseCode>"
        "<AgentID>1000002</AgentID>"
        "</xml>"
    )
    enc, sig, ts, nonce = _encrypted(inner)
    r = client.post(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        content=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode(),
    )
    assert r.status_code == 200
    ctx["s"].rollback()  # 读取 webapp 会话提交的最新状态
    assert ctx["s"].get(Voucher, v.id).status == "APPROVED"
    assert calls.get("response_code") == "resp-code-xyz"
    assert calls.get("vid") == v.id
    assert calls.get("result_text") == "已批准 ✅"


def test_callback_bad_signature(client):
    r = client.get(
        "/wecom/callback",
        params={"msg_signature": "bad", "timestamp": "1", "nonce": "n", "echostr": "e"},
    )
    assert r.status_code == 400
