"""P3-01 TDD：关账 Agent——检查/催办/结转/试算/报表草稿/开下期编排。

核心红线：Agent 永不代审——存在未审凭证时正式关账必须中止，
dry-run 不动账，全程产出 agent.monthend.run 事件。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Subject, Voucher, VoucherLine
from kernel.events import E
from kernel.monthend import MonthendError, run_monthend
from kernel.seed import seed_demo_ledger
from kernel.state import transition


class _SpyNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    return {"s": s, "ids": ids,
            "actor": {"type": "user", "id": ids["subject_id"]},
            "reviewer": {"type": "user", "id": reviewer.id}}


def _book(s, ids, ctx, no, day, summary, amount, status="PUSHED"):
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
        voucher_no=no, voucher_date=day, status="DRAFT", summary=summary,
        created_by=ids["subject_id"],
    )
    v.lines = [
        VoucherLine(line_no=1, account_id=accs["660202"].id,
                    debit=amount, credit=Decimal0()),
        VoucherLine(line_no=2, account_id=accs["100201"].id,
                    debit=Decimal0(), credit=amount),
    ]
    s.add(v)
    s.flush()
    transition(s, voucher_id=v.id, actor=ctx["actor"], target="PUSHED")
    if status in ("APPROVED", "POSTED"):
        transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="APPROVED")
    if status == "POSTED":
        from kernel.posting import post_voucher

        post_voucher(s, voucher_id=v.id, actor=ctx["actor"])
    s.commit()
    return v


def Decimal0():
    from decimal import Decimal

    return Decimal("0.00")


def test_dry_run_reports_pending_and_does_not_touch_books(ctx):
    """dry-run：催办清单 + 不动账。"""
    s, ids = ctx["s"], ctx["ids"]
    _book(s, ids, ctx, "记-7001", __import__("datetime").date(2026, 8, 5),
          "待审批费用", __import__("decimal").Decimal("100.00"), status="PUSHED")
    spy = _SpyNotifier()
    rep = run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026,
                       month=8, actor=ctx["actor"], notifier=spy, dry_run=True)
    s.commit()
    assert rep["dry_run"] is True
    assert rep["steps"]["chase"]["pending_count"] == 1
    assert spy.sent == []                    # dry-run 不发真实催办
    vouchers = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).all()
    assert all(v.status != "POSTED" or "7001" not in v.voucher_no for v in vouchers)
    assert s.scalars(select(Event).where(
        Event.event_type == E.AGENT_MONTHEND_RUN)).all() == []


def test_chase_notifies_pending_vouchers(ctx):
    """正式执行遇未审凭证 → 中止 + 催办已发（Agent 不代审）。"""
    s, ids = ctx["s"], ctx["ids"]
    _book(s, ids, ctx, "记-7101", __import__("datetime").date(2026, 8, 6),
          "未审费用", __import__("decimal").Decimal("50.00"), status="PUSHED")
    spy = _SpyNotifier()
    with pytest.raises(MonthendError) as ei:
        run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx["actor"], notifier=spy)
    s.commit()
    assert ei.value.code == "PENDING_VOUCHERS"
    assert len(spy.sent) == 1
    assert "1 张凭证待处理" in spy.sent[0][0]
    assert "记-7101" in spy.sent[0][1]


def test_full_monthend_happy_path(ctx):
    """全流程：审批完毕 → 结转 → 试算 → 报表草稿 → 开下期 → 事件落链。"""
    s, ids = ctx["s"], ctx["ids"]
    _book(s, ids, ctx, "记-7201", __import__("datetime").date(2026, 8, 8),
          "已审批费用", __import__("decimal").Decimal("800.00"), status="POSTED")
    spy = _SpyNotifier()
    rep = run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026,
                       month=8, actor=ctx["actor"], notifier=spy)
    s.commit()
    steps = rep["steps"]
    assert steps["closing"]["voucher_no"] and steps["closing"]["voucher_no"].startswith("结转-")
    assert steps["trial_balance"]["reconcile_ok"] is True
    assert steps["reports_draft"]["income_statement"]["net_profit"] == "-800.00"
    assert steps["reports_draft"]["balance_sheet"]["balanced"] is True
    assert steps["open_next"]["voucher_no"].startswith("期初-")
    assert steps["chase"]["pending_count"] == 0
    run_events = s.scalars(select(Event).where(
        Event.event_type == E.AGENT_MONTHEND_RUN)).all()
    assert len(run_events) == 1
    assert run_events[0].payload["net_profit"] == "-800.00"


def test_monthend_idempotent_on_rerun(ctx):
    """重复执行：结转/开账幂等（ALREADY_*），报表照常输出。"""
    s, ids = ctx["s"], ctx["ids"]
    _book(s, ids, ctx, "记-7301", __import__("datetime").date(2026, 8, 9),
          "已过账", __import__("decimal").Decimal("30.00"), status="POSTED")
    run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx["actor"], notifier=_SpyNotifier())
    s.commit()
    rep2 = run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026,
                        month=8, actor=ctx["actor"], notifier=_SpyNotifier())
    s.commit()
    assert rep2["steps"]["closing"]["status"] == "ALREADY_CLOSED"
    assert rep2["steps"]["open_next"]["status"] == "ALREADY_OPENED"


def test_monthend_requires_open_period(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(MonthendError) as ei:
        run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2030,
                     month=1, actor=ctx["actor"], notifier=_SpyNotifier())
    assert ei.value.code == "PERIOD_NOT_FOUND"
