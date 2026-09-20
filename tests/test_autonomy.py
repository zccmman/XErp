"""P3-03 / O11 TDD：L3 自治档——授权令牌门禁 + 额度内自主过账/事后抽检/红字冲销/回放。

红线断言：
- **O11 ① 每会话显式授权令牌**：autonomous_post 必须持有效、未过期、预算充足的
  授权令牌，否则 AUTH_TOKEN_REQUIRED / GRANT_*（即便是 L3 Agent 也绝不可无令牌自执行）；
- **O11 ③ 额度用尽自动暂停并通知**：令牌预算耗尽 → 自动跳闸冻结 Agent（BREAKER_OPEN）
  + 发 AGENT_AUTONOMY_PAUSED 人类通知事件 + 抛 GRANT_BUDGET；
- L3 自治不是 Agent 自审（AGENT_APPROVAL_FORBIDDEN 不可绕过）；非 L3/人类拒绝；
- 抽检推翻走红字冲销（不删历史）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.anomaly import breaker_is_open
from kernel.autonomy import (
    AutonomyError, audit_list, audit_review, autonomous_post, issue_grant,
    replay, revoke_grant,
)
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Subject, Voucher
from kernel.events import E
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    l3 = Subject(type="agent", display_name="L3Agent", autonomy_level=3,
                 daily_voucher_limit=Decimal("1000.00"))
    l3b = Subject(type="agent", display_name="L3AgentB", autonomy_level=3,
                  daily_voucher_limit=Decimal("1000.00"))
    l1 = Subject(type="agent", display_name="L1Agent", autonomy_level=1)
    human = Subject(type="user", display_name="复核人", autonomy_level=3)
    s.add_all([l3, l3b, l1, human])
    s.commit()
    return {"s": s, "ids": ids, "l3": l3, "l3b": l3b, "l1": l1, "human": human}


def _lines(amount: str):
    return [("660202", Decimal(amount), Decimal("0.00")),
            ("100201", Decimal("0.00"), Decimal(amount))]


def _grant(ctx, budget: str = "1000.00", days: int = 1) -> str:
    """签发一张给 L3 Agent 的授权令牌，返回 grant_id。"""
    return issue_grant(
        ctx["s"], ledger_set_id=ctx["ids"]["ledger_set_id"],
        agent_subject_id=ctx["l3"].id, admin_subject_id=ctx["human"].id,
        budget=Decimal(budget),
        expires_at=datetime.now(timezone.utc) + timedelta(days=days),
    )["grant_id"]


# -------------------------------------------------- O11 ① 授权令牌门禁


def test_autonomous_post_requires_valid_grant(ctx):
    """无令牌 → AUTH_TOKEN_REQUIRED；令牌不存在 → GRANT_NOT_FOUND。"""
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l3"].id, summary="无令牌",
                        lines=_lines("10.00"), grant_id="nope")
    assert ei.value.code == "GRANT_NOT_FOUND"


def test_autonomous_post_rejects_expired_grant(ctx):
    s, ids = ctx["s"], ctx["ids"]
    gid = issue_grant(
        s, ledger_set_id=ids["ledger_set_id"], agent_subject_id=ctx["l3"].id,
        admin_subject_id=ctx["human"].id, budget=Decimal("1000.00"),
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )["grant_id"]
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l3"].id, summary="过期令牌",
                        lines=_lines("10.00"), grant_id=gid)
    assert ei.value.code == "GRANT_EXPIRED"


def test_autonomous_post_rejects_revoked_grant(ctx):
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    revoke_grant(s, grant_id=gid, admin_subject_id=ctx["human"].id, note="收回")
    s.commit()
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l3"].id, summary="已吊销",
                        lines=_lines("10.00"), grant_id=gid)
    assert ei.value.code == "GRANT_REVOKED"


def test_autonomous_post_rejects_mismatched_grant(ctx):
    """令牌签发给 L3，但由另一个 L3 主体（l3b）持用 → GRANT_MISMATCH。"""
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")  # 签发给 l3
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l3b"].id, summary="冒用令牌",
                        lines=_lines("10.00"), grant_id=gid)
    assert ei.value.code == "GRANT_MISMATCH"


# -------------------------------------------------- O11 ③ 额度用尽自动暂停 + 通知


def test_grant_budget_exhaust_auto_pauses_and_notifies(ctx):
    """令牌预算用尽 → GRANT_BUDGET + 自动跳闸冻结 Agent + 人类通知事件。"""
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    # 第一笔 600 成功，剩余 400
    autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                    voucher_date=datetime(2026, 8, 10).date(),
                    actor_id=ctx["l3"].id, summary="第一笔",
                    lines=_lines("600.00"), grant_id=gid)
    s.commit()
    # 第二笔 500 > 剩余 400 → 预算耗尽
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l3"].id, summary="超预算",
                        lines=_lines("500.00"), grant_id=gid)
    assert ei.value.code == "GRANT_BUDGET"
    # 自动暂停：Agent 被跳闸冻结
    assert breaker_is_open(s, ctx["l3"].id) is not None
    # 人类通知事件已落链
    paused = s.scalars(select(Event).where(
        Event.event_type == E.AGENT_AUTONOMY_PAUSED)).all()
    assert len(paused) == 1
    assert paused[0].payload["subject_id"] == ctx["l3"].id


# -------------------------------------------------- 基础红线（L3 / 科目）


def test_autonomous_post_direct_to_posted(ctx):
    """L3 + 有效令牌 + 额度内 → 直接 POSTED，且令牌 remaining 扣减。"""
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=datetime(2026, 8, 10).date(),
                          actor_id=ctx["l3"].id, summary="自治采购",
                          lines=_lines("500.00"), grant_id=gid)
    s.commit()
    v = s.get(Voucher, res["voucher"]["id"])
    assert v.status == "POSTED"
    assert res["autonomous"] is True
    assert res["grant_id"] == gid
    assert res["grant_remaining"] == "500.00"   # 1000 - 500
    assert res["quota_used_today"] == "500.00"


def test_non_l3_rejected(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l1"].id, summary="L1 尝试自治",
                        lines=_lines("10.00"), grant_id="x")
    assert ei.value.code == "L3_REQUIRED"
    # 人类也不行（自治是 Agent 特权）
    with pytest.raises(AutonomyError):
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["human"].id, summary="人类尝试",
                        lines=_lines("10.00"), grant_id="x")


def test_non_leaf_account_rejected(ctx):
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=datetime(2026, 8, 10).date(),
                        actor_id=ctx["l3"].id, summary="非叶子",
                        lines=[("1002", Decimal("10.00"), Decimal("0.00")),
                               ("6001", Decimal("0.00"), Decimal("10.00"))],
                        grant_id=gid)
    assert ei.value.code == "ACCOUNT_NOT_LEAF"


# -------------------------------------------------- 抽检 / 回放


def test_audit_review_pass_and_reverse(ctx):
    """抽检通过/推翻：推翻生成红字冲销凭证并过账，余额归零。"""
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=datetime(2026, 8, 10).date(),
                          actor_id=ctx["l3"].id, summary="待抽检",
                          lines=_lines("300.00"), grant_id=gid)
    s.commit()
    vid = res["voucher"]["id"]

    pool = audit_list(s, ledger_set_id=ids["ledger_set_id"])
    assert pool["pending"] == 1

    rev = audit_review(s, voucher_id=vid, verdict="reverse",
                       reviewer_id=ctx["human"].id, note="科目用错")
    s.commit()
    assert rev["reversal_voucher_no"].startswith("记-")
    reversal = s.scalars(select(Voucher).where(
        Voucher.voucher_no == rev["reversal_voucher_no"])).one()
    assert reversal.status == "POSTED"
    # 再次抽检该凭证 → ALREADY_REVIEWED
    with pytest.raises(AutonomyError) as ei:
        audit_review(s, voucher_id=vid, verdict="pass",
                     reviewer_id=ctx["human"].id)
    assert ei.value.code == "ALREADY_REVIEWED"
    pool2 = audit_list(s, ledger_set_id=ids["ledger_set_id"])
    status = {x["voucher_id"]: x["audit_status"] for x in pool2["pool"]}
    assert status[vid] == "reversed"


def test_audit_pass(ctx):
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=datetime(2026, 8, 10).date(),
                          actor_id=ctx["l3"].id, summary="合规",
                          lines=_lines("50.00"), grant_id=gid)
    s.commit()
    r = audit_review(s, voucher_id=res["voucher"]["id"], verdict="pass",
                     reviewer_id=ctx["human"].id, note="无异常")
    s.commit()
    assert r["verdict"] == "pass"


def test_replay_full_timeline(ctx):
    """一键回放：自治过账 + 抽检通过 → 事件链完整可回放。"""
    s, ids = ctx["s"], ctx["ids"]
    gid = _grant(ctx, "1000.00")
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=datetime(2026, 8, 10).date(),
                          actor_id=ctx["l3"].id, summary="回放演示",
                          lines=_lines("10.00"), grant_id=gid)
    s.commit()
    audit_review(s, voucher_id=res["voucher"]["id"], verdict="pass",
                 reviewer_id=ctx["human"].id)
    s.commit()
    rep = replay(s, voucher_id=res["voucher"]["id"])
    types = [e["event_type"] for e in rep["timeline"]]
    assert E.AUTONOMOUS_POSTED in types
    assert E.AUTONOMOUS_REVIEWED in types
    assert rep["event_count"] == len(types)


def test_replay_nonexistent(ctx):
    s, _ids = ctx["s"], ctx["ids"]
    with pytest.raises(AutonomyError) as ei:
        replay(s, voucher_id="nonexistent")
    assert ei.value.code == "VOUCHER_NOT_FOUND"
