"""ai_runtime_state 工具 TDD（ADR-007 迭代3）。

算子状态的 MCP 暴露面：AI 能读（口述与视觉一致）、能写（声明自己的
活动层，offline 终于有了真实触发路径）。状态只动信号桥，不碰账。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))
sys.path.insert(0, str(ROOT))

from xerp_mcp.server import build_server  # noqa: E402


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    """桥指向临时文件 + 复位进程内状态，测试间互不污染。"""
    from kernel import operator as op

    monkeypatch.setenv("XERP_OPERATOR_STATE_FILE", str(tmp_path / "op_bridge.json"))
    monkeypatch.setattr(op, "_applied_ts", 0.0)
    op.reset()
    yield
    op.reset()


@pytest.fixture(scope="module")
def tool():
    async def _get():
        mcp = build_server(f"sqlite:///{tempfile.mkdtemp()}/air.db")
        return await mcp.get_tool("ai_runtime_state")

    return asyncio.run(_get())


def _invoke(tool, **kwargs):
    """fastmcp 4.x：Tool.fn 是裸函数；兜底 await tool.run。"""
    fn = getattr(tool, "fn", None)
    if fn is not None:
        return fn(**kwargs)
    return asyncio.run(tool.run(kwargs))


def test_read_returns_current_state_and_labels(tool):
    r = _invoke(tool)
    assert r["ok"] is True
    assert r["state"] == "idle"
    assert r["label_zh"] == "算子 · 待机"
    assert r["label_en"] == "Operator · Idle"
    assert r["last_signal"] is None


def test_write_agent_signal_lands_in_bridge(tool):
    r = _invoke(tool, state="listening")
    assert r["ok"] is True
    assert r["state"] == "listening"
    from kernel.operator import peek_bridge

    sig = peek_bridge()
    assert sig["state"] == "listening"
    assert sig["source"] == "agent"


def test_write_offline_via_tool(tool):
    """offline 触发路径打通：会话结束/LLM 不可用时 Agent 手动声明。"""
    r = _invoke(tool, state="offline")
    assert r["ok"] is True
    assert r["state"] == "offline"
    from kernel.operator import peek_bridge, sync_from_bridge

    assert peek_bridge()["state"] == "offline"
    # Web 渲染侧能合法化应用：offline 对任意当前态直达
    assert sync_from_bridge().value == "offline"


def test_write_rejects_pending_idle_and_unknown(tool):
    for bad, why in (("pending", "自动联动"), ("idle", "复位态"), ("bogus", "未知")):
        r = _invoke(tool, state=bad)
        assert r["ok"] is False, bad
        assert r["error"]["code"] == "BAD_STATE"
    # 被拒状态不得落桥
    from kernel.operator import peek_bridge

    assert peek_bridge() is None


def test_peek_bridge_ignores_polluted_payload():
    from kernel import operator as op

    p = op._bridge_path()
    p.write_text("not-json{{", encoding="utf-8")
    assert op.peek_bridge() is None
    p.write_text('{"state":"hacked","source":"x","ts":1.0}', encoding="utf-8")
    assert op.peek_bridge() is None
