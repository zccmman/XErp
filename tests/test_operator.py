"""算子 · 状态机单元测试（产品方案 §8 落地钉死）。"""

from __future__ import annotations

import json

import pytest

from kernel import operator as op


@pytest.fixture(autouse=True)
def _reset_op(monkeypatch, tmp_path):
    op.reset()
    # 桥指向临时文件 + 清已应用时间戳：测试间互不污染，也不碰仓库根真桥
    monkeypatch.setenv("XERP_OPERATOR_STATE_FILE", str(tmp_path / "op_bridge.json"))
    monkeypatch.setattr(op, "_applied_ts", 0.0)
    yield
    op.reset()
    op.set_locale("zh-CN")  # i18n 用例切过语言，恢复默认防串扰


def test_state_enum_has_six_members():
    assert {s.value for s in op.OperatorState} == {
        "idle", "listening", "drafting", "pending", "alert", "offline",
    }


def test_default_state_is_idle():
    assert op.current_state() == op.OperatorState.IDLE


def test_set_state_legal_transition():
    op.set_state(op.OperatorState.LISTENING)
    assert op.current_state() == op.OperatorState.LISTENING


def test_set_state_illegal_transition_raises():
    # idle → pending 非法（pending 必须经 drafting 进入）
    with pytest.raises(op.IllegalOperatorTransition):
        op.set_state(op.OperatorState.PENDING)
    # pending → drafting 非法（待审不能倒退起草）
    op.reset()
    op.set_state(op.OperatorState.LISTENING)
    op.set_state(op.OperatorState.DRAFTING)
    op.set_state(op.OperatorState.PENDING)
    with pytest.raises(op.IllegalOperatorTransition):
        op.set_state(op.OperatorState.DRAFTING)


def test_all_six_states_render_svg():
    for s in op.OperatorState:
        svg = op.render_svg(s)
        assert f'op-{s.value}' in svg
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")


def test_render_fragment_contains_state_token():
    frag = op.render_fragment(op.OperatorState.DRAFTING)
    assert 'data-state="drafting"' in frag
    assert "算子 · 起草中" in frag


def test_hidden_via_env_returns_empty_container(monkeypatch):
    monkeypatch.setenv("XERP_OPERATOR_HIDDEN", "1")
    frag = op.render_fragment(op.OperatorState.IDLE)
    assert 'data-state="hidden"' in frag
    assert "<svg" not in frag, "关闭开关后容器应为空 fragment，不渲染 SVG"


def test_legal_transition_table_offline_only_to_idle():
    """offline 是降级态，仅允许回 idle 或保持 offline；不能跳到 drafting。"""
    op.set_state(op.OperatorState.OFFLINE)
    with pytest.raises(op.IllegalOperatorTransition):
        op.set_state(op.OperatorState.DRAFTING)


def test_legal_transition_alert_can_recover_to_idle():
    """alert 必须能回到 idle（异常处理后状态复位）。"""
    op.set_state(op.OperatorState.ALERT)
    op.set_state(op.OperatorState.IDLE)
    assert op.current_state() == op.OperatorState.IDLE


def test_drafting_to_pending_is_legal():
    """起草完成后进入待审是 B 标准版联动的核心转移。"""
    op.set_state(op.OperatorState.DRAFTING)
    op.set_state(op.OperatorState.PENDING)
    assert op.current_state() == op.OperatorState.PENDING


# ---------- 迭代2 · 跨进程信号桥 ----------


def test_signal_writes_bridge_and_sync_applies():
    """MCP 侧 signal 只写不应用；Web 侧 sync 读桥后合法化应用。"""
    ok = op.signal(op.OperatorState.DRAFTING, source="test")
    assert ok is True
    assert op.current_state() == op.OperatorState.IDLE, "signal 不改本进程状态"
    applied = op.sync_from_bridge()
    assert applied == op.OperatorState.DRAFTING
    assert op.current_state() == op.OperatorState.DRAFTING


def test_sync_walks_legally_to_pending_from_idle():
    """IDLE 直达 PENDING 非法（合法表约束）；sync 经 DRAFTING 中转必须到达。"""
    op.signal(op.OperatorState.PENDING, source="test")
    applied = op.sync_from_bridge()
    assert applied == op.OperatorState.PENDING
    # 重复 sync 同一信号不重放（ts 去重）
    assert op.sync_from_bridge() is None
    assert op.current_state() == op.OperatorState.PENDING


def test_sync_ignores_unknown_state_and_broken_bridge():
    """桥污染不致死：未知状态值 / 坏 JSON / 缺文件都静默忽略。"""
    p = op._bridge_path()
    p.write_text(json.dumps({"state": "hacked", "source": "x", "ts": 99.0}),
                 encoding="utf-8")
    assert op.sync_from_bridge() is None
    assert op.current_state() == op.OperatorState.IDLE
    p.write_text("not-json{{", encoding="utf-8")
    assert op.sync_from_bridge() is None
    p.unlink()
    assert op.sync_from_bridge() is None


# ---------- 迭代2 · i18n（zh-CN / en-US） ----------


def test_labels_en_us_cover_all_six_states():
    op.set_locale("en-US")
    assert op.current_locale() == "en-US"
    for s in op.OperatorState:
        label = op.state_label(s)
        assert label.startswith("Operator · "), f"{s} 缺英文文案"
    frag = op.render_fragment(op.OperatorState.PENDING)
    assert "Operator · Awaiting review" in frag
    assert "算子" not in frag, "en-US 下不得残留中文标签"


def test_locale_switch_roundtrip_and_unknown_raises():
    assert op.current_locale() == "zh-CN"
    assert op.state_label(op.OperatorState.IDLE) == "算子 · 待机"
    op.set_locale("en-US")
    op.set_locale("zh-CN")
    assert op.state_label(op.OperatorState.IDLE) == "算子 · 待机"
    with pytest.raises(ValueError):
        op.set_locale("fr-FR")
    assert op.current_locale() == "zh-CN", "非法 locale 不应改变现值"

# ---------- 迭代4 · 用户到场听令（arrive） ----------

def test_arrive_from_idle_goes_listening():
    """IDLE 页面到场 → LISTENING（听令只升不压）。"""
    op.arrive()
    assert op.current_state() == op.OperatorState.LISTENING


def test_arrive_keeps_drafting():
    """DRAFTING 是有效信息，浏览页面不抹掉。"""
    op.set_state(op.OperatorState.DRAFTING)
    assert op.arrive() == op.OperatorState.DRAFTING


def test_arrive_keeps_pending():
    """PENDING 同理——待审信息比「用户在看哪页」重要。"""
    op.set_state(op.OperatorState.DRAFTING)
    op.set_state(op.OperatorState.PENDING)
    assert op.arrive() == op.OperatorState.PENDING


def test_arrive_from_offline_walks_to_listening():
    """OFFLINE→IDLE→LISTENING 合法路径唤醒。"""
    op.set_state(op.OperatorState.OFFLINE)
    assert op.arrive() == op.OperatorState.LISTENING
