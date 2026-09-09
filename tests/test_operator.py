"""算子 · 状态机单元测试（产品方案 §8 落地钉死）。"""

from __future__ import annotations

import pytest

from kernel import operator as op


@pytest.fixture(autouse=True)
def _reset_op():
    op.reset()
    yield
    op.reset()


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