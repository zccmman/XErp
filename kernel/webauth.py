"""Web 端最小认证（G0 止血）。

背景（P0-4 / P0-5）：
    修复前 webapp 没有任何认证，且用 `s_first_subject()` 取数据库里第一个
    主体当 actor。后果是致命的——NO_SELF_APPROVAL 等治理红线在 Web 端完全失效，
    「谁做的」这个审计链最核心的字段恒为同一个人，事件账本的证据价值归零。

设计取舍（最小可用，不做完整 IAM）：
    - 单口令 + 身份选择：口令来自环境变量 XERP_WEB_PASSWORD；
      登录后从账套内已有主体（Subject）中选择身份，actor 即该身份
    - 无口令则进入「单机开放模式」并打印 WARNING —— 保证开发环境零配置可用，
      但生产部署必须设口令
    - 会话用 HMAC-SHA256 签名的 Cookie，密钥来自 XERP_WEB_SECRET
      （未设置则随机生成，重启后已登录会话失效）
    - 不改数据库模型，无需迁移

不在本模块职责内：多租户、RBAC（那是 Casbin 层的活）、密码策略、审计登录日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

COOKIE_NAME = "xerp_web_session"
# 会话有效期：8 小时（一个工作日）
SESSION_TTL_SECONDS = 8 * 3600

# 未显式配置时随机生成（重启失效，已登录用户需重新登录）
_SECRET = os.environ.get("XERP_WEB_SECRET") or secrets.token_urlsafe(32)


def web_password() -> str:
    """Web 入口口令。为空表示单机开放模式（不校验口令，仅选身份）。"""
    return os.environ.get("XERP_WEB_PASSWORD", "").strip()


def is_open_mode() -> bool:
    """是否处于单机开放模式（未设口令）。生产环境必须为 False。"""
    return not web_password()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    return hmac.new(_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def issue_token(subject_id: str, subject_name: str = "") -> str:
    """签发会话令牌（payload.signature）。"""
    payload = {
        "sub": subject_id,
        "name": subject_name,
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    body = _b64e(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return f"{body}.{_sign(body)}"


def parse_token(token: str | None) -> dict[str, Any] | None:
    """校验并解析会话令牌；无效/过期返回 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    if not hmac.compare_digest(_sign(body), sig):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload


def check_password(candidate: str) -> bool:
    """口令校验（开放模式下恒为 True）。使用常量时间比较防时序侧信道。"""
    expected = web_password()
    if not expected:
        return True
    return hmac.compare_digest(candidate or "", expected)
