"""解释层（阶段0 第三步）TDD：把 anomaly/autonomy 的机器结论"讲给人听"。

覆盖三件事：
    1. 单元：explain_findings 三态判定（clean/attention/frozen）+ 未知规则兜底
       + explain_replay 逐行中文 + explain_audit_pool 摘要口径；
    2. 映射完整性：内核 anomaly 全部规则名都有中文名与建议动作（防新增规则漏配）；
    3. MCP 集成：anomaly_scan / autonomy_audit_list / autonomy_replay 三个工具
       返回都带 guide 块，且原始字段不被破坏（向后兼容）。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Subject  # noqa: E402
from kernel.explain import (  # noqa: E402
    ANOMALY_RULES,
    explain_audit_pool,
    explain_findings,
    explain_replay,
)
from kernel.seed import seed_demo_ledger  # noqa: E402


# ------------------------------------------------------------ 单元：三态判定

def test_findings_empty_is_clean():
    g = explain_findings([])
    assert g["verdict"] == "clean"
    assert g["verdict_zh"] == "未检出异常"
    assert g["items"] == []


def test_findings_info_only_is_attention():
    g = explain_findings([
        {"rule": "off_hours", "severity": "info", "message": "夜间创建"},
    ])
    assert g["verdict"] == "attention"
    item = g["items"][0]
    assert item["rule_zh"] == "非常规时间操作"
    assert item["severity_zh"] == "提示"
    assert "确认" in item["action"]


def test_findings_with_breaker_is_frozen():
    g = explain_findings([
        {"rule": "large_amount", "severity": "warn", "message": "大额"},
    ], breaker_tripped=True)
    assert g["verdict"] == "frozen"
    assert "anomaly_release" in g["summary_zh"]
    # 铁律口径：被冻结的是 Agent 自治，人类操作不受影响
    assert "人类" in g["summary_zh"]
    assert g["items"][0]["rule_zh"] == "大额凭证"


def test_findings_unknown_rule_falls_back():
    g = explain_findings([
        {"rule": "brand_new_rule", "severity": "warn", "message": "新规则"},
    ])
    item = g["items"][0]
    assert item["rule_zh"] == "brand_new_rule"   # 未知码原样透出，不编造
    assert item["action"]                        # 但给兜底建议，不空转


# ------------------------------------------------------------ 映射完整性

@pytest.mark.parametrize("rule", [
    "large_amount", "off_hours", "rare_account", "freq_spike", "llm_suspicious",
])
def test_all_kernel_rules_have_zh_and_action(rule):
    meta = ANOMALY_RULES.get(rule)
    assert meta, f"规则 {rule} 缺中文映射"
    assert meta["zh"] and meta["action"]


# ------------------------------------------------------------ 回放与抽检池

def test_replay_steps_annotated_and_narrative():
    res = {
        "voucher_no": "记-0001", "status": "POSTED", "summary": "x",
        "event_count": 3,
        "timeline": [
            {"event_type": "VOUCHER_CREATED", "actor": "u1"},
            {"event_type": "VOUCHER_APPROVED", "actor": "u2"},
            {"event_type": "VOUCHER_POSTED", "actor": "u1"},
        ],
    }
    g = explain_replay(res)
    assert [s["event_zh"] for s in g["steps"]] == [
        "创建凭证", "审批通过", "过账入账"]
    assert "记-0001" in g["narrative_zh"]
    # 未知事件码不炸：原样透出
    res2 = {**res, "timeline": [{"event_type": "FUTURE_THING", "actor": None}]}
    assert explain_replay(res2)["steps"][0]["event_zh"] == "FUTURE_THING"


def test_audit_pool_summaries():
    empty = explain_audit_pool({"pool": [], "pending": 0})
    assert "抽检池为空" in empty["summary_zh"]
    pending = explain_audit_pool({"pool": [
        {"voucher_no": "记-0009", "audit_status": "pending", "total": "100"},
    ], "pending": 1})
    assert "1 张待人工抽检" in pending["summary_zh"]
    assert "记-0009" in pending["summary_zh"]
    assert pending["items"][0]["status_zh"] == "待抽检"
    done = explain_audit_pool({"pool": [
        {"voucher_no": "记-0002", "audit_status": "passed"},
    ], "pending": 0})
    assert "无待办" in done["summary_zh"]


# ------------------------------------------------------------ MCP 集成

@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/explain.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()  # 铁律：casbin 表不在 metadata 里，grant 前必须先 commit
        from kernel.authz import grant_ledger_role

        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=ids["subject_id"], role="admin")
        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=reviewer.id, role="reviewer")
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def server(env):
    from xerp_mcp.server import build_server

    return build_server(env["url"])


def _call(server, name, **args):
    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(name, args)
            if getattr(res, "data", None) is not None:
                return res.data
            import json

            return json.loads(res.content[0].text)

    return asyncio.run(inner())


def _big_lines():
    return [
        {"account_code": "6602", "debit": "20000", "credit": ""},
        {"account_code": "1002", "debit": "", "credit": "20000"},
    ]


def test_anomaly_scan_returns_guide(server, env):
    mk = _call(server, "create_voucher",
               ledger_set_id=env["ids"]["ledger_set_id"],
               voucher_date="2026-08-27", summary="大额联调",
               actor_id=env["ids"]["subject_id"], lines=_big_lines())
    vid = mk["voucher"]["id"]
    r = _call(server, "anomaly_scan",
              ledger_set_id=env["ids"]["ledger_set_id"],
              actor_id=env["ids"]["subject_id"], voucher_id=vid)
    assert r["ok"] is True
    # 原始字段不动（向后兼容）
    assert r["findings"][0]["rule"] == "large_amount"
    # guide 讲给人听
    assert r["guide"]["verdict"] == "attention"
    assert r["guide"]["items"][0]["rule_zh"] == "大额凭证"
    assert r["guide"]["items"][0]["action"]


def test_audit_list_and_replay_return_guide(server, env):
    ls = env["ids"]["ledger_set_id"]
    r = _call(server, "autonomy_audit_list", ledger_set_id=ls)
    assert "summary_zh" in r["guide"]
    assert "抽检池" in r["guide"]["summary_zh"]

    mk = _call(server, "create_voucher", ledger_set_id=ls,
               voucher_date="2026-08-27", summary="回放联调",
               actor_id=env["ids"]["subject_id"],
               lines=[{"account_code": "6602", "debit": "10", "credit": ""},
                      {"account_code": "1001", "debit": "", "credit": "10"}])
    rp = _call(server, "autonomy_replay", voucher_id=mk["voucher"]["id"])
    assert rp["guide"]["steps"][0]["event_zh"] == "创建凭证"
    assert mk["voucher"]["voucher_no"] in rp["guide"]["narrative_zh"]
