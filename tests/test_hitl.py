"""P0-08 TDD：HITL 状态机补完——cancel_post 补偿事务 + Agent 自治门禁。

ADR-004：POSTED→DRAFT 逆向窗口仅未结账期间开放；补偿是追加事件而非改历史；
余额投影必须同步回冲；Agent 审批一律禁止；Agent 撤销记账须 L3。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Balance, Event, Period, Subject, Voucher, VoucherLine
from kernel.posting import PostingError, post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import cancel_post_voucher, transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    agent_l1 = Subject(type="agent", display_name="助理Agent", autonomy_level=1)
    agent_l3 = Subject(type="agent", display_name="关账Agent", autonomy_level=3)
    s.add_all([reviewer, agent_l1, agent_l3])
    s.commit()
    ids["reviewer_subject_id"] = reviewer.id
    ids["agent_l1"] = agent_l1.id
    ids["agent_l3"] = agent_l3.id

    user_actor = {"type": "user", "id": ids["subject_id"]}
    reviewer_actor = {"type": "user", "id": ids["reviewer_subject_id"]}

    def make_posted(voucher_no="记-9001"):
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=voucher_no,
            voucher_date=date(2026, 8, 27),
            status="DRAFT",
            summary="撤销测试",
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("300.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("300.00")),
        ]
        s.add(v)
        s.flush()
        transition(s, voucher_id=v.id, actor=user_actor, target="PUSHED")
        transition(s, voucher_id=v.id, actor=reviewer_actor, target="APPROVED")
        post_voucher(s, voucher_id=v.id, actor=user_actor)
        s.commit()
        return s.get(Voucher, v.id)

    yield {
        "s": s,
        "ids": ids,
        "user": user_actor,
        "reviewer": reviewer_actor,
        "make_posted": make_posted,
    }
    s.close()


def _balances(s, ledger_set_id):
    rows = s.scalars(select(Balance).where(Balance.ledger_set_id == ledger_set_id)).all()
    return {b.account_id: (b.debit_total, b.credit_total) for b in rows}


# ---------- 正向补偿流 ----------


def test_cancel_post_returns_to_draft_and_reverses_projection(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = ctx["make_posted"]()

    cancelled = cancel_post_voucher(
        s, voucher_id=v.id, actor={"type": "user", "id": ids["reviewer_subject_id"]}
    )
    s.commit()
    fresh = s.get(Voucher, v.id)
    assert fresh.status == "DRAFT" and fresh.posted_at is None
    assert cancelled  # 返回凭证对象

    after = _balances(s, ids["ledger_set_id"])
    # 费用借方被回冲后无残留（投影行随清零删除）
    assert ids["expense_account_id"] not in after or (
        after[ids["expense_account_id"]][0] == Decimal("0.00")
    )
    # 现金贷方发生额应被回冲干净
    cash_after = after.get(ids["cash_account_id"])
    assert cash_after is None or cash_after[1] == Decimal("0.00")

    from kernel.ledger import verify_chain

    ok, problem = verify_chain(s, ids["ledger_set_id"])
    assert ok and problem is None


def test_cancel_then_full_replay(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = ctx["make_posted"]()
    cancel_post_voucher(s, voucher_id=v.id, actor=ctx["reviewer"])
    s.commit()

    # 撤销后可完整重走生命周期（重放）
    transition(s, voucher_id=v.id, actor=ctx["user"], target="PUSHED")
    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="APPROVED")
    post_voucher(s, voucher_id=v.id, actor=ctx["user"])
    s.commit()
    fresh = s.get(Voucher, v.id)
    assert fresh.status == "POSTED"
    bal = _balances(s, ids["ledger_set_id"])
    assert bal[ids["expense_account_id"]][0] >= Decimal("300.00")


def test_original_events_untouched_append_only_chain_ok(ctx):
    from kernel.ledger import verify_chain

    s, ids = ctx["s"], ctx["ids"]
    v = ctx["make_posted"]()
    snapshot_before = [e.hash for e in s.query(Event).all()]
    cancel_post_voucher(s, voucher_id=v.id, actor=ctx["reviewer"])
    s.commit()
    after = [e.hash for e in s.query(Event).all()]
    # 只追加不修改：前缀哈希序列保持不变
    assert after[: len(snapshot_before)] == snapshot_before
    ok, problem = verify_chain(s, ids["ledger_set_id"])
    assert ok and problem is None


# ---------- 门禁与边界 ----------


def test_cancel_requires_open_period(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = ctx["make_posted"]()
    period = s.get(Period, ids["period_id"])
    period.status = "CLOSED"
    s.commit()
    with pytest.raises(PostingError) as ei:
        cancel_post_voucher(s, voucher_id=v.id, actor=ctx["reviewer"])
    assert ei.value.code == "PERIOD_CLOSED"


def test_cancel_on_draft_invalid(ctx):
    s, ids = ctx["s"], ctx["ids"]
    draft_v = Voucher(
        ledger_set_id=ids["ledger_set_id"],
        period_id=ids["period_id"],
        voucher_no="记-9100",
        voucher_date=date(2026, 8, 27),
        status="DRAFT",
        created_by=ids["subject_id"],
    )
    s.add(draft_v)
    s.flush()
    with pytest.raises(PostingError) as ei:
        cancel_post_voucher(s, voucher_id=draft_v.id, actor=ctx["reviewer"])
    assert ei.value.code == "INVALID_TRANSITION"


def test_agent_cannot_approve(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = ctx["make_posted"]()
    cancel_post_voucher(s, voucher_id=v.id, actor=ctx["reviewer"])
    s.commit()
    transition(s, voucher_id=v.id, actor=ctx["user"], target="PUSHED")
    with pytest.raises(PostingError) as ei:
        transition(
            s,
            voucher_id=v.id,
            actor={"type": "agent", "id": ids["agent_l3"]},
            target="APPROVED",
        )
    assert ei.value.code == "AGENT_APPROVAL_FORBIDDEN"


def test_agent_cancel_needs_l3(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = ctx["make_posted"]()

    with pytest.raises(PostingError) as ei:
        cancel_post_voucher(
            s, voucher_id=v.id, actor={"type": "agent", "id": ids["agent_l1"]}
        )
    assert ei.value.code == "AUTONOMY_DENIED"

    # L3 Agent 在未结账期间可执行撤销
    got = cancel_post_voucher(
        s, voucher_id=v.id, actor={"type": "agent", "id": ids["agent_l3"]}
    )
    assert got.status == "DRAFT"
