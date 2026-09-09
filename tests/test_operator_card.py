"""算子迭代5 · IM 卡片状态行：card_note + 企微/飞书审批卡附状态行。

卡片构建器是纯函数（离线），本文件不打网络。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from kernel import operator as op
from kernel.wecom import build_approval_card as wecom_card
from xerp_mcp.feishu import build_approval_card as feishu_card

_LINES = [
    {"account_code": "660204", "account_name": "业务招待费",
     "debit": "800.00", "credit": "0.00"},
    {"account_code": "1001", "account_name": "库存现金",
     "debit": "0.00", "credit": "800.00"},
]


@pytest.fixture(autouse=True)
def _bridge(monkeypatch, tmp_path):
    """桥指向临时文件：不碰仓库根真桥；测试间互不污染。"""
    monkeypatch.setenv("XERP_OPERATOR_STATE_FILE", str(tmp_path / "op_bridge.json"))
    monkeypatch.setattr(op, "_applied_ts", 0.0)


def _signal(state: op.OperatorState) -> None:
    assert op.signal(state, source="mcp") is True


def test_card_note_none_without_bridge():
    """无桥信号 → None：卡片不显示状态行（纯增值信息）。"""
    assert op.card_note() is None


def test_card_note_reflects_bridge():
    """桥上有 pending 信号 → 状态行含双语标签（zh-CN / en-US）。"""
    _signal(op.OperatorState.PENDING)
    note = op.card_note()
    assert note is not None
    lab = op.labels_for(op.OperatorState.PENDING)
    assert note == f'{lab["zh-CN"]} / {lab["en-US"]}'


def test_card_note_tolerates_polluted_bridge(monkeypatch, tmp_path):
    """桥污染（未知状态值）→ None，绝不抛异常。"""
    p = tmp_path / "bad.json"
    p.write_text('{"state": "dancing", "source": "x", "ts": 99.0}', encoding="utf-8")
    monkeypatch.setenv("XERP_OPERATOR_STATE_FILE", str(p))
    assert op.card_note() is None


def test_wecom_card_includes_operator_note():
    """企微审批卡副标题附「算子 · 状态」。"""
    _signal(op.OperatorState.PENDING)
    card = wecom_card(voucher_no="记-9501", status="PUSHED",
                      summary="迭代5", lines=_LINES, voucher_id="vid-5")
    assert "算子" in card["sub_title_text"]
    assert op.state_label(op.OperatorState.PENDING) in card["sub_title_text"]


def test_wecom_card_without_bridge_has_no_note():
    """无桥信号 → 副标题不含算子字样（向后兼容）。"""
    card = wecom_card(voucher_no="记-9502", status="PUSHED",
                      summary="迭代5", lines=_LINES, voucher_id="vid-6")
    assert "算子" not in card["sub_title_text"]


def test_feishu_card_includes_note_before_guide():
    """飞书审批卡：note 行插在审批引导行之前，引导行保持 elements[-1]。"""
    _signal(op.OperatorState.PENDING)
    card = feishu_card(voucher_no="记-0001", status="PUSHED",
                       summary="迭代5", lines=_LINES, voucher_id="v-5")
    els = card["elements"]
    # 既有位置断言语义不破坏：分录在 [3]、引导行在 [-1]
    assert "660204" in els[3]["text"]["content"]
    assert "同意 记-0001" in els[-1]["text"]["content"]
    # note 行紧邻引导行之前
    note_el = els[-2]
    assert note_el["tag"] == "note"
    assert "算子" in note_el["elements"][0]["content"]
    assert op.state_label(op.OperatorState.PENDING) in note_el["elements"][0]["content"]


def test_feishu_card_without_bridge_keeps_original_layout():
    """无桥信号 → 无 note 元素，elements 布局与迭代4 之前完全一致。"""
    card = feishu_card(voucher_no="记-0002", status="PUSHED",
                       summary="迭代5", lines=_LINES, voucher_id="v-6")
    els = card["elements"]
    assert len(els) == 6
    assert all(e["tag"] != "note" for e in els)
    assert "同意 记-0002" in els[-1]["text"]["content"]
