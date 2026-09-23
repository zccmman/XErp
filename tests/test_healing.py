"""E4 异常自愈建议（TDD）。

DoD：
- rule_scan 命中 → 映射为 HITL 整改建议（human_approval_required=True、auto_executable=False）。
- 大额凭证 → large_amount 建议（severity warn）。
- 当前 open 的 Agent 断路器 → breaker_review 建议（action anomaly_release、severity critical）。
- 只读：不改账（Voucher / Event 行数不变）。
- severity_rank：无命中=normal；critical 建议存在=critical。
"""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.anomaly import trip_breaker
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import AgentBreaker, Event, Period, Subject, Voucher
from kernel.healing import healing_suggestions
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher

ZERO = Decimal("0.00")


@pytest.fixture()
def sess():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def env(sess):
    ids = seed_demo_ledger(sess)
    import_chart_of_accounts(sess, ids["ledger_set_id"], load_template_rows())
    p = sess.scalars(
        select(Period).where(
            Period.ledger_set_id == ids["ledger_set_id"],
            Period.year == 2026, Period.month == 8,
        )
    ).first()
    if p is None:
        p = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                   status="OPEN")
        sess.add(p)
    sess.flush()
    return ids


def _post(sess, env, lines, voucher_date="2026-08-05"):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date=voucher_date, summary="x", lines=lines,
    )
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()


def test_healing_large_amount_maps_to_hitl(sess, env):
    # 大额凭证（5 万 ≥ 阈值 1 万）→ 命中 large_amount
    _post(sess, env, [
        {"account_code": "1122", "debit": "50000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "50000"},
    ])
    # 小额正常凭证（不应触发大额）
    _post(sess, env, [
        {"account_code": "1122", "debit": "100", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "100"},
    ])
    res = healing_suggestions(
        sess, ledger_set_id=env["ledger_set_id"], lookback_days=9999
    )
    assert res["scanned_vouchers"] >= 2
    large = [s for s in res["suggestions"] if s["rule"] == "large_amount"]
    assert large, "应检出大额凭证并产出 large_amount 建议"
    s0 = large[0]
    assert s0["severity"] == "warn"
    assert s0["human_approval_required"] is True
    assert s0["auto_executable"] is False
    assert s0["draft_payload"]["action"] == "escalate_approval"


def test_healing_breaker_review(sess, env):
    # 准备一个 agent 主体并跳闸（setup 副作用，不在只读断言范围内）
    agent = Subject(id="agent_x", type="agent", display_name="bot")
    sess.add(agent)
    sess.commit()
    trip_breaker(
        sess, subject_id="agent_x",
        reasons=["large_amount: 测试跳闸"], actor={"id": "u1", "type": "user"},
    )
    sess.commit()

    before_evt = len(sess.scalars(select(Event)).all())
    res = healing_suggestions(
        sess, ledger_set_id=env["ledger_set_id"], lookback_days=30
    )
    after_evt = len(sess.scalars(select(Event)).all())

    assert res["breaker_open_count"] >= 1
    br = [s for s in res["suggestions"] if s["action_id"].startswith("B-")]
    assert br, "应汇总 open 的 Agent 断路器复核建议"
    b0 = br[0]
    assert b0["severity"] == "critical"
    assert b0["draft_payload"]["action"] == "anomaly_release"
    assert b0["human_approval_required"] is True
    # 只读：healing 调用本身不得追加事件
    assert before_evt == after_evt, "healing 必须只读，绝不追加事件"


def test_healing_readonly(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "50000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "50000"},
    ])
    before_v = len(sess.scalars(
        select(Voucher).where(Voucher.ledger_set_id == env["ledger_set_id"])
    ).all())
    healing_suggestions(
        sess, ledger_set_id=env["ledger_set_id"], lookback_days=9999
    )
    after_v = len(sess.scalars(
        select(Voucher).where(Voucher.ledger_set_id == env["ledger_set_id"])
    ).all())
    assert before_v == after_v, "healing 必须只读，绝不写账"


def test_healing_tool_calls_traceable(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "50000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "50000"},
    ])
    res = healing_suggestions(
        sess, ledger_set_id=env["ledger_set_id"], lookback_days=9999
    )
    assert any(tc["tool"] == "anomaly.rule_scan" for tc in res["tool_calls"])
