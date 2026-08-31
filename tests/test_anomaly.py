"""P3-02 TDD：异常侦测 + 断路器——规则通道/冻结语义/人工解除。

红线断言：断路器只冻结 type=agent，人类永不受影响；
冻结状态是事件流最终态，Agent 不能自解。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.anomaly import (
    AnomalyError,
    breaker_is_open,
    check_breaker,
    release_breaker,
    rule_scan,
    scan_voucher,
    trip_breaker,
)
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Subject, Voucher, VoucherLine
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    bot = Subject(type="agent", display_name="自治Agent", autonomy_level=2)
    human = Subject(type="user", display_name="人类会计", autonomy_level=1)
    s.add_all([bot, human])
    s.commit()
    return {"s": s, "ids": ids, "bot": bot, "human": human,
            "actor": {"type": "user", "id": ids["subject_id"]}}


def _voucher(s, ids, creator_id, no, amount, summary="测试凭证",
             account="660202", hour=None):
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
        voucher_no=no, voucher_date=__import__("datetime").date(2026, 8, 10),
        status="DRAFT", summary=summary, created_by=creator_id,
    )
    v.lines = [
        VoucherLine(line_no=1, account_id=accs[account].id,
                    debit=amount, credit=__import__("decimal").Decimal("0.00")),
        VoucherLine(line_no=2, account_id=accs["100201"].id,
                    debit=__import__("decimal").Decimal("0.00"), credit=amount),
    ]
    s.add(v)
    s.flush()
    return v


def test_large_amount_detected_and_trips_agent_breaker(ctx):
    """大额凭证 → 检出 + Agent 创建者断路器跳闸。"""
    s, ids = ctx["s"], ctx["ids"]
    big = __import__("decimal").Decimal("20000.00")
    v = _voucher(s, ids, ctx["bot"].id, "记-8101", big)
    s.commit()
    findings = scan_voucher(s, v, actor=ctx["actor"])
    s.commit()
    rules = {f.rule for f in findings}
    assert "large_amount" in rules
    state = breaker_is_open(s, ctx["bot"].id)
    assert state is not None
    assert any("large_amount" in r for r in state["reasons"])


def test_breaker_blocks_agent_only(ctx):
    """冻结只拦 Agent：同库人类完全不受影响。"""
    s, _ids = ctx["s"], ctx["ids"]
    trip_breaker(s, subject_id=ctx["bot"].id, reasons=["large_amount: 测试"],
                 actor=ctx["actor"])
    s.commit()
    with pytest.raises(AnomalyError) as ei:
        check_breaker(s, ctx["bot"].id)
    assert "断路器" in str(ei.value)
    check_breaker(s, ctx["human"].id)   # 人类不抛错
    check_breaker(s, ctx["ids"]["subject_id"])


def test_release_restores_autonomy(ctx):
    """人工解除 → Agent 恢复；事件流可回放全轨迹。"""
    s, _ids = ctx["s"], ctx["ids"]
    trip_breaker(s, subject_id=ctx["bot"].id, reasons=["测试跳闸"],
                 actor=ctx["actor"])
    s.commit()
    assert breaker_is_open(s, ctx["bot"].id) is not None
    release_breaker(s, subject_id=ctx["bot"].id, actor=ctx["actor"], note="误报")
    s.commit()
    assert breaker_is_open(s, ctx["bot"].id) is None
    types = [e.event_type for e in s.scalars(select(Event).order_by(Event.id))]
    assert types.count("agent.breaker.tripped") == 1
    assert types.count("agent.breaker.released") == 1


def test_off_hours_rule(ctx):
    """周末/夜间创建 → off_hours 检出（info 级，不触发断路器）。"""
    s, ids = ctx["s"], ctx["ids"]
    v = _voucher(s, ids, ctx["bot"].id, "记-8102",
                 __import__("decimal").Decimal("100.00"))
    # 强制 created_at 到周日 23:00
    import datetime as dt

    v.created_at = dt.datetime(2026, 8, 9, 23, 0)   # 2026-08-09 是周日
    s.commit()
    findings = rule_scan(s, v)
    assert any(f.rule == "off_hours" for f in findings)
    # 不含 large/freq → 不跳闸
    scan_voucher(s, v, actor=ctx["actor"])
    s.commit()
    assert breaker_is_open(s, ctx["bot"].id) is None


def test_normal_voucher_no_findings(ctx):
    s, ids = ctx["s"], ctx["ids"]
    v = _voucher(s, ids, ctx["bot"].id, "记-8103",
                 __import__("decimal").Decimal("100.00"))
    findings = scan_voucher(s, v, actor=ctx["actor"])
    s.commit()
    assert findings == []
    assert breaker_is_open(s, ctx["bot"].id) is None
