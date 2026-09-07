"""工具分层契约：三档的包含关系、归类完备性、以及"极简档不含自动过账能力"。

这里测的不是功能，是**暴露面**。分层靠 mcp.json 的 disabledTools 生效，
一旦归类漂移（新增工具忘了归档、或名字写错），AI 侧会静默看不到工具——
没有任何报错，只有"AI 怎么不会用"。所以必须钉死。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from xerp_mcp import profiles  # noqa: E402


def _all_tools():
    from xerp_mcp.server import build_server

    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/profiles.db"

    async def inner():
        return sorted(t.name for t in await build_server(url).list_tools())

    return asyncio.run(inner())


@pytest.fixture(scope="module")
def all_tools():
    return _all_tools()


# ------------------------------------------------------------ 1. 归类完备性


def test_tiers_are_disjoint():
    a, b, c = (
        set(profiles.MINIMAL),
        set(profiles.STANDARD_EXTRA),
        set(profiles.PRO_ONLY),
    )
    assert not (a & b), f"极简与标准重复：{a & b}"
    assert not (a & c), f"极简与专业重复：{a & c}"
    assert not (b & c), f"标准与专业重复：{b & c}"


def test_tiers_cover_every_registered_tool(all_tools):
    """新增工具必须显式归到某一档，否则这里红。"""
    covered = set(profiles.known_tools())
    assert set(all_tools) - covered == set(), (
        f"以下工具尚未归类：{sorted(set(all_tools) - covered)}"
    )
    assert covered - set(all_tools) == set(), (
        f"以下工具已不存在，请从 profiles 删除：{sorted(covered - set(all_tools))}"
    )


def test_no_duplicate_within_tier():
    names = list(profiles.known_tools())
    dup = {n for n in names if names.count(n) > 1}
    assert not dup, f"归类表内有重复工具名：{sorted(dup)}"


# ------------------------------------------------------------ 2. 包含关系


def test_minimal_subset_standard_subset_pro(all_tools):
    mi = set(profiles.enabled_for("minimal"))
    st = set(profiles.enabled_for("standard"))
    pr = set(profiles.enabled_for("pro"))
    assert mi < st < pr
    assert pr == set(all_tools), "专业档必须是全量，不得裁剪"


def test_disabled_is_exact_complement(all_tools):
    for p in ("minimal", "standard", "pro"):
        dis = set(profiles.disabled_for(p, all_tools))
        en = set(profiles.enabled_for(p))
        assert dis | en == set(all_tools), f"{p} 档并集不等于全集"
        assert dis & en == set(), f"{p} 档同一工具既启用又禁用"


def test_pro_disables_nothing(all_tools):
    assert profiles.disabled_for("pro", all_tools) == []


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        profiles.enabled_for("ultimate")


# ------------------------------------------------------------ 3. 语义约束


def test_every_enabled_tool_really_exists(all_tools):
    """防止归类表里写了错别字——名字对不上时工具会静默消失。"""
    for p in ("minimal", "standard", "pro"):
        missing = set(profiles.enabled_for(p)) - set(all_tools)
        assert not missing, f"{p} 档含不存在的工具：{sorted(missing)}"


def test_deprecated_alias_hidden_outside_pro():
    """get_workspace 只是兼容已发出交付包的兜底，不该占 AI 的上下文。"""
    assert "get_workspace" not in profiles.enabled_for("minimal")
    assert "get_workspace" not in profiles.enabled_for("standard")


#: 会自行改账的能力：极简档一律不暴露（这些动作一旦误触发代价高）
SELF_ACTING = {
    "autonomy_post",
    "autonomy_replay",
    "anomaly_release",
    "transfer_run",
    "adapter_ingest",
    "adapter_register",
    "monthend_run",
    "close_period",
}


def test_minimal_has_no_self_acting_tools():
    leaked = SELF_ACTING & set(profiles.enabled_for("minimal"))
    assert not leaked, f"极简档不应暴露自动改账能力：{sorted(leaked)}"


def test_minimal_covers_main_chain():
    """极简档必须能独立跑通：建账 → 制单 → 审核 → 记账 → 查账 → 报表。"""
    needed = {
        "init_ledger_set",
        "ensure_period",
        "create_voucher",
        "push_voucher",
        "approve_voucher",
        "post_voucher",
        "query_balances",
        "report_balance_sheet",
    }
    assert needed <= set(profiles.enabled_for("minimal"))


def test_standard_covers_month_end():
    needed = {
        "close_period",
        "open_next_period",
        "precheck_close",
        "report_cash_flow",
    }
    assert needed <= set(profiles.enabled_for("standard"))


# ------------------------------------------------------------ 4. 档位说明


def test_profiles_documented():
    assert set(profiles.PROFILES) == {"minimal", "standard", "pro"}
    for v in profiles.PROFILES.values():
        assert v and len(v) > 5


# ------------------------------------------------------------ 5. 真禁用（build_server 接线）

import tempfile as _tf  # noqa: E402


def _tools_for(profile):
    from xerp_mcp.server import build_server

    url = f"sqlite:///{_tf.mkdtemp()}/x.db"

    async def inner():
        return sorted(t.name for t in await build_server(url, profile=profile).list_tools())

    return asyncio.run(inner())


def test_build_server_profile_filters_tools():
    """build_server(profile=) 必须真的把工具挡在 list_tools 之外（不只是改配置）。"""
    for p, expected in (("minimal", 15), ("standard", 29), ("pro", 49)):
        got = _tools_for(p)
        assert len(got) == expected, f"{p} 档 list_tools 应为 {expected}，实为 {len(got)}"
        assert set(got) == set(profiles.enabled_for(p)), f"{p} 档暴露集合与 profiles 不一致"


def test_build_server_no_profile_exposes_all(all_tools):
    assert set(_tools_for(None)) == set(all_tools)
