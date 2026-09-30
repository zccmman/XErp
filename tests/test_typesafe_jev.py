"""TypeSafe Jev 云端适配器测试：请求构造 / 云端映射 / resolve_decision 门控。

全部 mock 网络与 DB，验证：
- call_system_one 正确构造 Bearer 鉴权请求（端点/Header/Body）；
- run_decision_cloud 把 Jev 答案映射回 Decision（本地权威值/severity 不变，云端叠加置信度与分歧标记）；
- resolve_decision 默认本地；云端需 backend=typesafe + cloud_consent + 已配置 key 才启用；
  云端失败安全回退本地（标记 cloud_fallback）；绝不因云端异常改变本地判定。
- 不发送任何真实密钥 / 不触网（CI 零网络依赖）。
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from kernel.adapters.typesafe_jev import (
    Decision,
    Severity,
    TypeSafeError,
    _cloud_dispatch,
    build_cloud_question,
    call_system_one,
    is_configured,
    map_cloud_answer,
    resolve_decision,
    run_decision_cloud,
)

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def _fake_urlopen(req, timeout=None):
    _fake_urlopen.req = req
    resp = MagicMock()
    resp.read.return_value = json.dumps({"answers": {}}).encode("utf-8")
    # 让 `with _urlopen(...) as resp` 仍拿到同一个配置好的 resp（而非 __enter__ 默认新 Mock）
    resp.__enter__.return_value = resp
    return resp


def test_is_configured_toggles_with_env():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("TYPESAFE_API_KEY", None)
        assert is_configured() is False
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "  K  "}):
        assert is_configured() is True


def test_call_system_one_builds_bearer_request():
    with patch("kernel.adapters.typesafe_jev._urlopen", _fake_urlopen):
        call_system_one(
            state={"x": 1},
            questions={"q": {"type": "noul", "instructions": "?"}},
            api_key="SECRET_KEY",
        )
    req = _fake_urlopen.req
    assert req.full_url == _ENDPOINT
    assert req.headers.get("Authorization") == "Bearer SECRET_KEY"
    # urllib 会规范化头名大小写（Content-Type→Content-type），用不区分大小写比对
    hdrs = {k.lower(): v for k, v in req.headers.items()}
    assert hdrs.get("content-type") == "application/json"
    body = json.loads(req.data)
    assert body["state"] == {"x": 1}
    assert body["model"] == "jev-latest"
    assert "q" in body["questions"]


def test_call_system_one_no_key_raises():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("TYPESAFE_API_KEY", None)
        with pytest.raises(TypeSafeError) as e:
            call_system_one(state={}, questions={})
    assert e.value.code == "NOT_CONFIGURED"


def test_run_decision_cloud_noul_merge():
    base = Decision(
        kind="classify", label="重复凭证标记", value="唯一", severity=Severity.LOW,
        evidence={"target_total": "100", "duplicates": ["V2"],
                  "target_summary": "x", "window_days": 7},
    )
    canned = {"answers": {"likely_dup": {"type": "noul", "noul": 0.9}}}
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
        with patch("kernel.adapters.typesafe_jev._dispatch_local", return_value=base), \
             patch("kernel.adapters.typesafe_jev._post", return_value=canned):
            d = run_decision_cloud("duplicate_voucher", MagicMock(), ledger_set_id="LS")
    assert d.evidence["backend"] == "typesafe"
    # 本地权威值不变（仍为「唯一」），云端仅叠加第二意见置信度
    assert d.value == "唯一"
    assert d.evidence["jev"]["value"] == "疑似重复"
    assert d.evidence["jev"]["confidence"] == 0.9
    # severity LOW(1) vs 云端 HIGH(3) 分歧≥2档 → 标记人工复核
    assert d.human_review_required is True


def test_run_decision_cloud_choice_merge_no_divergence():
    base = Decision(
        kind="score", label="预算差异分级（整体）", value="high", severity=Severity.HIGH,
        evidence={"totals": {}, "rows": []},
    )
    canned = {"answers": {"grade": {"type": "choice", "choice": "yellow", "confidence": 0.7}}}
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
        with patch("kernel.adapters.typesafe_jev._dispatch_local", return_value=base), \
             patch("kernel.adapters.typesafe_jev._post", return_value=canned):
            d = run_decision_cloud("budget_variance", MagicMock(), ledger_set_id="LS")
    assert d.evidence["jev"]["severity"] == "medium"
    assert d.evidence["jev"]["confidence"] == 0.7
    # HIGH(3) vs MEDIUM(2) 分歧 1 档 <2 → 不升人工
    assert d.human_review_required is False


def test_run_decision_cloud_unconfigured_raises():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("TYPESAFE_API_KEY", None)
        with pytest.raises(TypeSafeError):
            run_decision_cloud("risk_severity", MagicMock(), ledger_set_id="LS")


def test_cloud_dispatch_default_local_when_no_setting():
    sess = MagicMock()
    sess.get.return_value = None
    local = Decision(kind="score", label="x", value="high", severity=Severity.HIGH, evidence={})
    with patch("kernel.adapters.typesafe_jev._dispatch_local", return_value=local) as md, \
         patch("kernel.adapters.typesafe_jev.run_decision_cloud") as mc:
        d = _cloud_dispatch("budget_variance", sess, ledger_set_id="LS")
    assert d.evidence["backend"] == "local"
    assert not mc.called
    assert md.called


def test_cloud_dispatch_local_when_no_consent():
    setting = MagicMock()
    setting.backend = "typesafe"
    setting.cloud_consent = False
    sess = MagicMock()
    sess.get.return_value = setting
    local = Decision(kind="score", label="x", value="high", severity=Severity.HIGH, evidence={})
    with patch("kernel.adapters.typesafe_jev._dispatch_local", return_value=local) as md, \
         patch("kernel.adapters.typesafe_jev.run_decision_cloud") as mc:
        d = _cloud_dispatch("budget_variance", sess, ledger_set_id="LS")
    assert d.evidence["backend"] == "local"
    assert not mc.called
    assert md.called


def test_cloud_dispatch_cloud_success():
    setting = MagicMock()
    setting.backend = "typesafe"
    setting.cloud_consent = True
    sess = MagicMock()
    sess.get.return_value = setting
    fake = Decision(kind="score", label="x", value="high", severity=Severity.HIGH, evidence={})
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
        with patch("kernel.adapters.typesafe_jev.run_decision_cloud", return_value=fake) as mc:
            d = _cloud_dispatch("budget_variance", sess, ledger_set_id="LS")
    assert d.evidence["backend"] == "typesafe"
    assert mc.called


def test_cloud_dispatch_cloud_failure_falls_back_local():
    setting = MagicMock()
    setting.backend = "typesafe"
    setting.cloud_consent = True
    sess = MagicMock()
    sess.get.return_value = setting
    local = Decision(kind="score", label="x", value="high", severity=Severity.HIGH, evidence={})
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
        with patch("kernel.adapters.typesafe_jev.run_decision_cloud",
                   side_effect=TypeSafeError("NETWORK_ERROR", "x")), \
             patch("kernel.adapters.typesafe_jev._dispatch_local", return_value=local) as md:
            d = _cloud_dispatch("budget_variance", sess, ledger_set_id="LS")
    assert d.evidence["backend"] == "local"
    assert d.evidence.get("cloud_fallback") is True
    assert md.called


def test_run_decision_routes_to_cloud_via_ledger_set_id():
    """回归：run_decision 经 ledger_set_id 转发云端后端时，不得把 ledger_set_id 既作
    显式 kwarg 又留在 **params 中（否则 TypeError: multiple values），且应把 ledger_set_id
    恰好传一次；云端成功时 backend=typesafe 而非静默回退本地。"""
    from decimal import Decimal

    from kernel.decide import run_decision

    base = Decision(
        kind="classify", label="费用合规判定", value="合规",
        severity=Severity.LOW, confidence=Decimal("1"),
        basis=["本地基线与云端无关"], evidence={"flags": []},
    )
    setting = MagicMock()
    setting.backend = "typesafe"
    setting.cloud_consent = True
    sess = MagicMock()
    sess.get.return_value = setting
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
        with patch("kernel.decide._dispatch_local", return_value=base) as mlocal, \
             patch("kernel.adapters.typesafe_jev.run_decision_cloud",
                   return_value=base) as mcloud:
            d = run_decision("expense_compliance", sess, ledger_set_id="LS", voucher_id="V")
    # 云端后端必须被调用（证明未因重复传参异常而静默回退本地）
    mcloud.assert_called_once()
    kw = mcloud.call_args.kwargs
    assert kw.get("ledger_set_id") == "LS"
    assert kw.get("voucher_id") == "V"
    assert d.evidence.get("backend") == "typesafe"
    assert d.evidence.get("cloud_fallback") is None
    assert mlocal.called


def test_build_and_map_duplicate_noul():
    base = Decision(
        kind="classify", label="重复凭证标记", value="唯一", severity=Severity.LOW,
        evidence={"duplicates": ["V2"], "target_total": "100",
                  "target_summary": "x", "window_days": 7},
    )
    state, q = build_cloud_question("duplicate_voucher", base)
    assert q["likely_dup"]["type"] == "noul"
    # state 仅含聚合指标，不含原始凭证明细
    assert "duplicates" in state and "target_total" in state
    m = map_cloud_answer("duplicate_voucher",
                         {"likely_dup": {"type": "noul", "noul": 0.2}}, base)
    assert m["jev_value"] == "唯一"
    assert m["jev_severity"] == Severity.LOW


def test_build_and_map_approval_route_choice():
    base = Decision(
        kind="select", label="费用审批路由", value="finance_manager", severity=Severity.LOW,
        evidence={"options": ["a", "b", "c"],
                  "ranked": ["finance_manager", "line_manager", "general_manager"]},
    )
    state, q = build_cloud_question("approval_route", base)
    assert q["route"]["type"] == "choice"
    m = map_cloud_answer(
        "approval_route",
        {"route": {"type": "choice", "choice": "general_manager", "confidence": 0.9}}, base)
    assert m["jev_severity"] == Severity.HIGH


def test_run_decision_cloud_unsupported_decision_returns_local():
    base = Decision(kind="score", label="未知", value="x", severity=Severity.INFO, evidence={})
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "K"}):
        with patch("kernel.adapters.typesafe_jev._dispatch_local", return_value=base), \
             patch("kernel.adapters.typesafe_jev._post") as mp:
            d = run_decision_cloud("no_such_decision", MagicMock(), ledger_set_id="LS")
    assert not mp.called
    assert d.evidence["backend"] == "local"
    assert d.evidence.get("cloud_skipped") == "unsupported_decision"
