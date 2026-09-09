"""
XErp · AI Runtime 具象 — 「算子 · 账本精灵」状态机（第一版）。

§8 产品方案落地。本模块严格遵守 §8.2 五条红线：
  1. 不能是装饰品 —— 状态承载 AI Runtime 当前活动层；
  2. 不能喧宾夺主 —— ≤ 24×24 常驻，不参与主流程；
  3. 不能加戏 —— 状态切换无动画；
  4. 不能拟人化 —— 不做宠物/助手/吉祥物；
  5. 不脱离极简色板 —— 仅 4 色 token：黑/灰白/警示红/通过绿。

不联动任何写动作；落账/结账/付款等终态动作永远由 Boss 显式确认。
"""

from __future__ import annotations

import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any


class OperatorState(str, Enum):
    """六态枚举。值即 CSS 类名后缀（.op-{value}）。"""

    IDLE = "idle"
    LISTENING = "listening"
    DRAFTING = "drafting"
    PENDING = "pending"
    ALERT = "alert"
    OFFLINE = "offline"


# 合法转移表：from → set of to。非法转移抛 IllegalOperatorTransition，
# 避免散落 setState 把状态机搞成无规则状态图。
_LEGAL_TRANSITIONS: dict[OperatorState, frozenset[OperatorState]] = {
    OperatorState.IDLE: frozenset({
        OperatorState.IDLE,
        OperatorState.LISTENING,
        OperatorState.DRAFTING,
        OperatorState.ALERT,
        OperatorState.OFFLINE,
    }),
    OperatorState.LISTENING: frozenset({
        OperatorState.IDLE,
        OperatorState.LISTENING,
        OperatorState.DRAFTING,
        OperatorState.ALERT,
        OperatorState.OFFLINE,
    }),
    OperatorState.DRAFTING: frozenset({
        OperatorState.IDLE,
        OperatorState.DRAFTING,
        OperatorState.PENDING,
        OperatorState.ALERT,
        OperatorState.OFFLINE,
    }),
    OperatorState.PENDING: frozenset({
        OperatorState.IDLE,
        OperatorState.PENDING,
        OperatorState.ALERT,
        OperatorState.OFFLINE,
    }),
    OperatorState.ALERT: frozenset({
        OperatorState.IDLE,
        OperatorState.ALERT,
        OperatorState.LISTENING,
        OperatorState.DRAFTING,
        OperatorState.PENDING,
        OperatorState.OFFLINE,
    }),
    OperatorState.OFFLINE: frozenset({
        OperatorState.IDLE,
        OperatorState.OFFLINE,
    }),
}


class IllegalOperatorTransition(ValueError):
    """状态机非法转移——禁止业务方绕过合法表写状态。"""


# 进程内单点真源。Web 工作进程单线程，dict 读写天然安全；
# 多进程部署时各进程独立持有（无共享态，符合「不联动写动作」原则）。
_state: OperatorState = OperatorState.IDLE


def current_state() -> OperatorState:
    """读取当前算子状态。消费点唯一 API。"""
    return _state


def set_state(next_state: OperatorState) -> OperatorState:
    """写入状态。非法转移抛 IllegalOperatorTransition。

    触发点（写）仅 2 处：webapp.py 输入框 focus、业务完成回调。
    """
    global _state
    if next_state not in _LEGAL_TRANSITIONS[_state]:
        raise IllegalOperatorTransition(
            f"{_state.value} -> {next_state.value} 不在合法转移表"
        )
    _state = next_state
    return _state


def reset() -> None:
    """测试与冷启动用：重置到 IDLE。"""
    global _state
    _state = OperatorState.IDLE


# ---- 6 态 SVG 资产（内联常量；不依赖 StaticFiles） --------------------------

# 视觉规范（沿用产品方案 §8.3）：
#   viewBox 24×24 · 描边 1.5px · 圆角 8-10px · 仅 4 色 token
#   ink #1F1F1F · bg #F1EFE8 · red #E24B4A · green #639922
#   蓝/黄/绿/红用于状态底色（bg-*），与 webapp 既有 .err/.warn/.ok 同色域

_SVG_HEADER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" class="op-svg op-{state}" role="img" aria-label="{label}">'
_SVG_FOOTER = "</svg>"


def _idle_body() -> str:
    return (
        '<rect x="3" y="3" width="18" height="18" rx="2" fill="#F1EFE8" stroke="#1F1F1F" stroke-width="1.5"/>'
        '<line x1="5" y1="7" x2="19" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="10" x2="19" y2="10" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="13" x2="14" y2="13" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="9" y1="16" x2="11" y2="16" stroke="#1F1F1F" stroke-width="1.5" stroke-linecap="round"/>'
        '<line x1="13" y1="16" x2="15" y2="16" stroke="#1F1F1F" stroke-width="1.5" stroke-linecap="round"/>'
        '<line x1="9" y1="20" x2="15" y2="20" stroke="#1F1F1F" stroke-width="1.5" stroke-linecap="round"/>'
    )


def _listening_body() -> str:
    return (
        '<rect x="3" y="3" width="18" height="18" rx="2" fill="#E6F1FB" stroke="#1F1F1F" stroke-width="1.5"/>'
        '<line x1="5" y1="7" x2="19" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="10" x2="19" y2="10" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="13" x2="14" y2="13" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<circle cx="9" cy="17" r="1.2" fill="#1F1F1F"/>'
        '<circle cx="15" cy="17" r="1.2" fill="#1F1F1F"/>'
        '<path d="M9 20 Q12 22 15 20" stroke="#1F1F1F" stroke-width="1.5" fill="none" stroke-linecap="round"/>'
        '<path d="M19 6 L22 4 L22 8 Z" fill="#185FA5"/>'
    )


def _drafting_body() -> str:
    return (
        '<rect x="3" y="3" width="18" height="18" rx="2" fill="#FAEEDA" stroke="#1F1F1F" stroke-width="1.5"/>'
        '<line x1="5" y1="7" x2="13" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="15" y1="7" x2="19" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<path d="M8 17 Q9 16 10 17" stroke="#1F1F1F" stroke-width="1.5" fill="none" stroke-linecap="round"/>'
        '<path d="M14 17 Q15 16 16 17" stroke="#1F1F1F" stroke-width="1.5" fill="none" stroke-linecap="round"/>'
        '<line x1="10" y1="20" x2="14" y2="20" stroke="#1F1F1F" stroke-width="1.5" stroke-linecap="round"/>'
        '<g transform="translate(17,9) rotate(45)">'
        '<rect x="0" y="0" width="5" height="1.5" fill="#1F1F1F"/>'
        '<path d="M5 0 L7 -0.7 L7 1.5 L5 0.8 Z" fill="#1F1F1F"/>'
        "</g>"
    )


def _pending_body() -> str:
    return (
        '<rect x="3" y="3" width="18" height="18" rx="2" fill="#EAF3DE" stroke="#1F1F1F" stroke-width="1.5"/>'
        '<line x1="5" y1="7" x2="19" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="10" x2="19" y2="10" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<circle cx="9" cy="17" r="1.2" fill="#1F1F1F"/>'
        '<circle cx="15" cy="17" r="1.2" fill="#1F1F1F"/>'
        '<path d="M9 19 Q12 21 15 19" stroke="#1F1F1F" stroke-width="1.5" fill="none" stroke-linecap="round"/>'
        '<g transform="translate(16,11)">'
        '<rect x="0" y="0" width="6" height="5" rx="0.5" fill="#FFFFFF" stroke="#1F1F1F" stroke-width="1"/>'
        '<line x1="1" y1="2" x2="3" y2="2" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<circle cx="5" cy="1" r="0.7" fill="#639922"/>'
        "</g>"
    )


def _alert_body() -> str:
    return (
        '<rect x="3" y="3" width="18" height="18" rx="2" fill="#FCEBEB" stroke="#E24B4A" stroke-width="1.5"/>'
        '<line x1="5" y1="7" x2="19" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="10" x2="19" y2="10" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<circle cx="9" cy="16" r="1.4" fill="#FFFFFF" stroke="#1F1F1F" stroke-width="1.2"/>'
        '<circle cx="9" cy="16" r="0.5" fill="#1F1F1F"/>'
        '<circle cx="15" cy="16" r="1.4" fill="#FFFFFF" stroke="#1F1F1F" stroke-width="1.2"/>'
        '<circle cx="15" cy="16" r="0.5" fill="#1F1F1F"/>'
        '<ellipse cx="12" cy="20" rx="1" ry="0.8" fill="#E24B4A"/>'
        '<g transform="translate(16,9)">'
        '<path d="M3 0 L6 5 L0 5 Z" fill="#E24B4A"/>'
        '<line x1="3" y1="2" x2="3" y2="3.5" stroke="#FFFFFF" stroke-width="1"/>'
        '<circle cx="3" cy="4.5" r="0.4" fill="#FFFFFF"/>'
        "</g>"
    )


def _offline_body() -> str:
    return (
        '<rect x="3" y="3" width="18" height="18" rx="2" fill="#F1EFE8" stroke="#A0A0A0" stroke-width="1.5" stroke-dasharray="2 1.5"/>'
        '<line x1="5" y1="7" x2="19" y2="7" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="10" x2="19" y2="10" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="5" y1="13" x2="19" y2="13" stroke="#A0A0A0" stroke-width="0.5"/>'
        '<line x1="8" y1="17" x2="10" y2="17" stroke="#A0A0A0" stroke-width="1.5" stroke-linecap="round"/>'
        '<line x1="14" y1="17" x2="16" y2="17" stroke="#A0A0A0" stroke-width="1.5" stroke-linecap="round"/>'
        '<path d="M9 20 Q12 22 15 20" stroke="#A0A0A0" stroke-width="1.5" fill="none" stroke-linecap="round"/>'
        '<text x="20" y="6" font-family="serif" font-size="5" fill="#A0A0A0">Z</text>'
    )


_SVG_BUILDERS: dict[OperatorState, Any] = {
    OperatorState.IDLE: _idle_body,
    OperatorState.LISTENING: _listening_body,
    OperatorState.DRAFTING: _drafting_body,
    OperatorState.PENDING: _pending_body,
    OperatorState.ALERT: _alert_body,
    OperatorState.OFFLINE: _offline_body,
}

# 状态标签（i18n 迭代2）：zh-CN / en-US 双语；渲染走 current_locale()。
_LABELS: dict[str, dict[OperatorState, str]] = {
    "zh-CN": {
        OperatorState.IDLE: "算子 · 待机",
        OperatorState.LISTENING: "算子 · 听令",
        OperatorState.DRAFTING: "算子 · 起草中",
        OperatorState.PENDING: "算子 · 待审",
        OperatorState.ALERT: "算子 · 异常",
        OperatorState.OFFLINE: "算子 · 离线",
    },
    "en-US": {
        OperatorState.IDLE: "Operator · Idle",
        OperatorState.LISTENING: "Operator · Listening",
        OperatorState.DRAFTING: "Operator · Drafting",
        OperatorState.PENDING: "Operator · Awaiting review",
        OperatorState.ALERT: "Operator · Alert",
        OperatorState.OFFLINE: "Operator · Offline",
    },
}

# locale 真源：环境变量 XERP_OPERATOR_LANG 注入初值，运行期 set_locale 切换。
_locale: str = os.environ.get("XERP_OPERATOR_LANG", "zh-CN").strip() or "zh-CN"
if _locale not in _LABELS:
    _locale = "zh-CN"


def set_locale(lang: str) -> None:
    """切换状态文案语言。未知语言抛 ValueError（静默降级会掩盖配置错误）。"""
    global _locale
    if lang not in _LABELS:
        raise ValueError(f"未知算子语言：{lang}（支持 {'/'.join(_LABELS)}）")
    _locale = lang


def current_locale() -> str:
    return _locale


def state_label(state: OperatorState) -> str:
    """当前 locale 下的状态文案。渲染层唯一取词口。"""
    return _LABELS[_locale][state]


def labels_for(state: OperatorState) -> dict[str, str]:
    """给定状态的全部语言文案（工具输出 label_zh/label_en 用）。"""
    return {lang: _LABELS[lang][state] for lang in sorted(_LABELS)}


def render_svg(state: OperatorState | None = None) -> str:
    """渲染指定状态的 SVG 字符串（无外层容器，inline 用）。"""
    s = state or current_state()
    body = _SVG_BUILDERS[s]()
    return _SVG_HEADER.format(state=s.value, label=state_label(s)) + body + _SVG_FOOTER


def is_hidden() -> bool:
    """偏好开关：隐藏算子。环境变量 XERP_OPERATOR_HIDDEN=1 时不渲染容器。

    第一版仅 env 开关；后续迭代再扩到用户偏好设置表。
    """
    return os.environ.get("XERP_OPERATOR_HIDDEN", "").strip() in ("1", "true", "yes")


def render_fragment(state: OperatorState | None = None) -> str:
    """渲染带容器与详情卡的 HTML fragment。固定位置右上角 24×24。

    容器 div.op-container 内嵌：
      - SVG（24×24，state 对应）
      - hover 展开 div.op-detail（80×80 + 状态文案）

    不参与主流程；点击仅展示详情，不触发任何写动作。
    """
    if is_hidden():
        return '<div class="op-container op-hidden" data-state="hidden"></div>'
    s = state or current_state()
    svg = render_svg(s)
    label = state_label(s)
    detail = (
        f'<div class="op-detail op-{s.value}" role="tooltip">'
        f'<div class="op-detail-svg">{svg.replace("width=\"24\" height=\"24\"", "width=\"40\" height=\"40\"")}</div>'
        f'<div class="op-detail-text">{label}</div>'
        "</div>"
    )
    return (
        f'<div class="op-container op-{s.value}" data-state="{s.value}" '
        f'title="{label}（只读 · 不触发写动作）">'
        f"{svg}{detail}"
        "</div>"
    )


# ---- 跨进程信号桥（迭代2 联动） ---------------------------------------------
#
# MCP 进程（create_voucher / push_voucher / anomaly_scan）与 Web 进程（渲染算子）
# 是两个 Python 进程，进程内 _state 互不可见。桥 = 一个 JSON 信号文件：
#   MCP 侧 signal() 写 → Web 侧 _page 渲染前 sync_from_bridge() 读并合法化应用。
# 信号只表达「最近一次 AI Runtime 活动」，不是队列——新信号覆盖旧信号。
# 桥是纯增值信息：读写任何失败都静默吞掉，绝不阻塞业务主流程。


def _bridge_path() -> Path:
    """桥文件路径。XERP_OPERATOR_STATE_FILE 覆盖；默认仓库根（同仓双进程一致）。"""
    p = os.environ.get("XERP_OPERATOR_STATE_FILE", "").strip()
    if p:
        return Path(p)
    return Path(__file__).resolve().parents[1] / "operator_state.json"


def _walk_to(target: OperatorState) -> None:
    """合法化走到 target：不经过的状态瞬时不渲染，故走快路无副作用。

    PENDING 不能从 IDLE 直达（合法表约束），经 DRAFTING 中转。
    """
    if current_state() == target:
        return
    set_state(OperatorState.IDLE)  # IDLE 对所有态合法出边
    if target is OperatorState.PENDING:
        set_state(OperatorState.DRAFTING)
    if target is not OperatorState.IDLE:
        set_state(target)


def signal(target: OperatorState, source: str = "mcp") -> bool:
    """写桥（MCP 侧调用）。只写不应用——应用权在 Web 渲染侧。

    返回是否落盘成功；任何异常（只读盘/权限）静默吞掉。
    """
    try:
        payload = json.dumps(
            {"state": target.value, "source": source, "ts": time.time()},
            ensure_ascii=False,
        )
        _bridge_path().write_text(payload, encoding="utf-8")
        return True
    except OSError:
        return False


# 渲染侧已应用到的信号时间戳（去重；重复应用同值转移无害但没必要）。
_applied_ts: float = 0.0


def peek_bridge() -> dict | None:
    """读桥最近信号（不应用）。供 ai_runtime_state 工具展示「谁最后说了什么」。

    无桥/坏载荷/未知状态值 → None。
    """
    try:
        raw = json.loads(_bridge_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("state") not in OperatorState._value2member_map_:
        return None
    return {
        "state": raw["state"],
        "source": raw.get("source", ""),
        "ts": float(raw.get("ts", 0)),
    }


def sync_from_bridge() -> OperatorState | None:
    """读桥并合法化应用（Web 渲染前调用）。

    返回应用后的状态；无桥/无效载荷/已应用过 → None（保持现态）。
    """
    global _applied_ts
    try:
        raw = json.loads(_bridge_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or float(raw.get("ts", 0)) <= _applied_ts:
        return None
    try:
        target = OperatorState(raw.get("state", ""))
    except ValueError:
        return None  # 未知状态值：桥污染不致死，忽略
    _applied_ts = float(raw["ts"])
    _walk_to(target)
    return current_state()


__all__ = [
    "OperatorState",
    "IllegalOperatorTransition",
    "current_state",
    "set_state",
    "reset",
    "render_svg",
    "render_fragment",
    "is_hidden",
    "set_locale",
    "current_locale",
    "state_label",
    "labels_for",
    "signal",
    "sync_from_bridge",
    "peek_bridge",
]