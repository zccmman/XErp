"""企微凭据自检脚本（不改动任何状态，只读 .env + 只读 API）。

用法:
    python scripts/wecom_selfcheck.py          # 格式校验 + 加解密往返 + access_token 实测
    python scripts/wecom_selfcheck.py --send   # 额外向 WECOM_RECEIVE_USER 发一条测试消息
                                               # （要求企业可信 IP 已包含本机出口 IP）

每项输出 ✅/❌ 与原因，全部通过即可去企微后台保存回调配置。
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from kernel.wecom import WecomError, encrypt_message, load_env  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="额外发送测试消息")
    args = ap.parse_args()

    env = load_env()

    # 1) 必填项存在性 + 格式
    corp_id = env.get("WECOM_CORP_ID", "")
    check(
        "WECOM_CORP_ID 已配置", bool(corp_id),
        corp_id if corp_id else "缺失（我的企业→企业信息→企业ID，ww 开头）",
    )
    if corp_id:
        check("CorpID 格式", bool(re.fullmatch(r"ww[0-9a-fA-F]+", corp_id)), "通常 ww 开头+16 位")

    secret = env.get("WECOM_CORP_SECRET", "")
    check("WECOM_CORP_SECRET 已配置", bool(secret),
          secret[:4] + "****" if secret else "缺失（应用详情→Secret→查看→发送到企业微信）")

    agent_id = env.get("WECOM_AGENT_ID", "")
    check(
        "WECOM_AGENT_ID 已配置", bool(agent_id),
        agent_id if agent_id else "缺失（应用详情页顶部 AgentId）",
    )
    if agent_id:
        check("AgentId 格式（纯数字）", agent_id.isdigit(), agent_id)

    token = env.get("WECOM_TOKEN", "")
    hint = "" if token else "缺失（接收消息→设置API接收→随机获取）"
    check("WECOM_TOKEN 已配置", bool(token), hint)

    aes_key = env.get("WECOM_ENCODING_AES_KEY", "")
    ok_key = check("WECOM_ENCODING_AES_KEY 已配置", bool(aes_key),
                   "缺失（接收消息→设置API接收→随机获取，43 位）" if not aes_key else "")
    if ok_key:
        try:
            raw = base64.b64decode(aes_key + "=")
            ok_len = check("AESKey 解码为 32 字节", len(raw) == 32, f"实际 {len(raw)} 字节")
            if ok_len:
                plain = "<xml><Test>XErp自检</Test></xml>"
                enc = encrypt_message(plain, corp_id=corp_id, aes_key=raw)
                from kernel.wecom import decrypt_message

                back = decrypt_message(enc, corp_id=corp_id, aes_key=raw)
                check("回调加解密往返", back == plain, "encrypt→decrypt 一致")
        except Exception as e:  # noqa: BLE001
            check("AESKey 可用性", False, str(e))

    # 2) access_token 实测（验证 CorpID+Secret 正确；不要求可信 IP）
    if corp_id and secret:
        try:
            from kernel.wecom import get_access_token

            tok = get_access_token(force=True)
            check("access_token 实测（gettoken）", bool(tok), "CorpID+Secret 有效")
        except WecomError as e:
            check("access_token 实测（gettoken）", False, str(e))
        except Exception as e:  # noqa: BLE001
            check("access_token 实测（gettoken）", False, f"网络异常: {e}")
    else:
        check("access_token 实测（gettoken）", False, "CorpID/Secret 缺失，跳过")

    # 3) 可选：发测试消息（要求可信 IP + 接收人已配置）
    if args.send:
        user = env.get("WECOM_RECEIVE_USER", "")
        if not user:
            check("发送测试消息", False, "WECOM_RECEIVE_USER 未配置（先在企微应用会话发「绑定」）")
        else:
            try:
                from kernel.wecom import send_text

                resp = send_text(user, "XErp 企微自检消息 ✅ 收到即通道正常")
                check("发送测试消息", resp.get("errcode") == 0,
                      f"errcode={resp.get('errcode')} errmsg={resp.get('errmsg', '')}")
            except WecomError as e:
                check("发送测试消息", False, str(e))

    failed = [r for r in RESULTS if not r[1]]
    if failed:
        tail = f"{len(failed)} 项未过，按提示补齐后重跑"
    else:
        tail = "全部通过，可去企微后台保存回调配置 🎉"
    print("\n结论: " + tail)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
