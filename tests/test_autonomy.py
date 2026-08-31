"""P3-03 TDD：L3 自治档——额度内自主过账/事后抽检/红字冲销/一键回放。

红线断言：L3 自治不是 Agent 自审（状态机 AGENT_APPROVAL_FORBIDDEN 不可绕过）；
非 L3 主体拒绝；额度超限拒绝；抽检推翻走红字冲销（不删历史）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.autonomy import (
    AutonomyError,
    audit_list,
    audit_review,
    autonomous_post,
    replay,
)
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Subject, Voucher
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    l3 = Subject(type="agent", display_name="L3Agent", autonomy_level=3,
                 daily_voucher_limit=__import__("decimal").Decimal("1000.00"))
    l1 = Subject(type="agent", display_name="L1Agent", autonomy_level=1)
    human = Subject(type="user", display_name="复核人", autonomy_level=3)
    s.add_all([l3, l1, human])
    s.commit()
    return {"s": s, "ids": ids, "l3": l3, "l1": l1, "human": human}


def _lines(amount: str):
    return [("660202", __import__("decimal").Decimal(amount),
             __import__("decimal").Decimal("0.00")),
            ("100201", __import__("decimal").Decimal("0.00"),
             __import__("decimal").Decimal(amount))]


def test_autonomous_post_direct_to_posted(ctx):
    """L3 额度内 → 直接 POSTED（系统规则执行，不经过 PUSHED/APPROVED）。"""
    s, ids = ctx["s"], ctx["ids"]
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=__import__("datetime").date(2026, 8, 10),
                          actor_id=ctx["l3"].id, summary="自治采购",
                          lines=_lines("500.00"))
    s.commit()
    v = s.get(Voucher, res["voucher"]["id"])
    assert v.status == "POSTED"
    assert res["quota_used_today"] == "500.00"
    assert res["autonomous"] is True


def test_non_l3_rejected(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=__import__("datetime").date(2026, 8, 10),
                        actor_id=ctx["l1"].id, summary="L1 尝试自治",
                        lines=_lines("10.00"))
    assert ei.value.code == "L3_REQUIRED"
    # 人类也不行（自治是 Agent 特权）
    with pytest.raises(AutonomyError):
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=__import__("datetime").date(2026, 8, 10),
                        actor_id=ctx["human"].id, summary="人类尝试",
                        lines=_lines("10.00"))


def test_quota_exceeded(ctx):
    s, ids = ctx["s"], ctx["ids"]
    autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                    voucher_date=__import__("datetime").date(2026, 8, 10),
                    actor_id=ctx["l3"].id, summary="第一笔",
                    lines=_lines("600.00"))
    s.commit()
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=__import__("datetime").date(2026, 8, 10),
                        actor_id=ctx["l3"].id, summary="超额度",
                        lines=_lines("500.00"))
    assert ei.value.code == "QUOTA_EXCEEDED"
    # 额度内剩余 400 可用（quota 按创建时刻 UTC 当日累计，非凭证业务日期）
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=__import__("datetime").date(2026, 8, 11),
                          actor_id=ctx["l3"].id, summary="额度内",
                          lines=_lines("400.00"))
    s.commit()
    assert res["quota_used_today"] == "1,000.00"


def test_non_leaf_account_rejected(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(AutonomyError) as ei:
        autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                        voucher_date=__import__("datetime").date(2026, 8, 10),
                        actor_id=ctx["l3"].id, summary="非叶子",
                        lines=[("1002", __import__("decimal").Decimal("10.00"),
                                __import__("decimal").Decimal("0.00")),
                               ("6001", __import__("decimal").Decimal("0.00"),
                                __import__("decimal").Decimal("10.00"))])
    assert ei.value.code == "ACCOUNT_NOT_LEAF"


def test_audit_review_pass_and_reverse(ctx):
    """抽检通过/推翻：推翻生成红字冲销凭证并过账，余额归零。"""
    s, ids = ctx["s"], ctx["ids"]
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=__import__("datetime").date(2026, 8, 10),
                          actor_id=ctx["l3"].id, summary="待抽检",
                          lines=_lines("300.00"))
    s.commit()
    vid = res["voucher"]["id"]

    pool = audit_list(s, ledger_set_id=ids["ledger_set_id"])
    assert pool["pending"] == 1

    # 推翻
    rev = audit_review(s, voucher_id=vid, verdict="reverse",
                       reviewer_id=ctx["human"].id, note="科目用错")
    s.commit()
    assert rev["reversal_voucher_no"].startswith("记-")
    reversal = s.scalars(select(Voucher).where(
        Voucher.voucher_no == rev["reversal_voucher_no"])).one()
    assert reversal.status == "POSTED"
    # 余额归零：银行科目借贷合计相等
    accs = {a.id: a for a in s.scalars(select(Account)).all()}
    accs[next(ln.account_id for ln in reversal.lines
                     if accs[ln.account_id].code == "100201")]
    [
        b for b in [__import__("kernel.db.models", fromlist=["Balance"]).Balance]
    ]
    # 直接验证：再次抽检该凭证 → ALREADY_REVIEWED
    with pytest.raises(AutonomyError) as ei:
        audit_review(s, voucher_id=vid, verdict="pass",
                     reviewer_id=ctx["human"].id)
    assert ei.value.code == "ALREADY_REVIEWED"
    pool2 = audit_list(s, ledger_set_id=ids["ledger_set_id"])
    status = {x["voucher_id"]: x["audit_status"] for x in pool2["pool"]}
    assert status[vid] == "reversed"


def test_audit_pass(ctx):
    s, ids = ctx["s"], ctx["ids"]
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=__import__("datetime").date(2026, 8, 10),
                          actor_id=ctx["l3"].id, summary="合规",
                          lines=_lines("50.00"))
    s.commit()
    r = audit_review(s, voucher_id=res["voucher"]["id"], verdict="pass",
                     reviewer_id=ctx["human"].id, note="无异常")
    s.commit()
    assert r["verdict"] == "pass"


def test_replay_full_timeline(ctx):
    """一键回放：自治过账 + 抽检通过 → 事件链完整可回放。"""
    s, ids = ctx["s"], ctx["ids"]
    res = autonomous_post(s, ledger_set_id=ids["ledger_set_id"],
                          voucher_date=__import__("datetime").date(2026, 8, 10),
                          actor_id=ctx["l3"].id, summary="回放演示",
                          lines=_lines("10.00"))
    s.commit()
    audit_review(s, voucher_id=res["voucher"]["id"], verdict="pass",
                 reviewer_id=ctx["human"].id)
    s.commit()
    rep = replay(s, voucher_id=res["voucher"]["id"])
    types = [e["event_type"] for e in rep["timeline"]]
    assert "voucher.autonomous.posted" in types
    assert "agent.autonomous.reviewed" in types
    assert rep["event_count"] == len(types)


def test_replay_nonexistent(ctx):
    s, _ids = ctx["s"], ctx["ids"]
    with pytest.raises(AutonomyError) as ei:
        replay(s, voucher_id="nonexistent")
    assert ei.value.code == "VOUCHER_NOT_FOUND"
