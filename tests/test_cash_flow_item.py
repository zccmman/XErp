"""② 现金流项目语义：声明式 cash_flow_item + 结构化 categories（TDD）。

DoD：
- cash_flow 返回 categories（经营/投资/筹资 各含 in/out/net/items）。
- 科目 attrs.cash_flow_item 优先作为项目名（如 6001 → 销售商品、提供劳务收到的现金），
  替代按对方科目前缀的默认归类。
- 净额与既有 operating/investing/financing/net_increase 一致（向后兼容）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Period
from kernel.posting import post_voucher
from kernel.reporting.statements import cash_flow
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
    ls_id = ids["ledger_set_id"]
    p = sess.scalars(
        select(Period).where(Period.ledger_set_id == ls_id, Period.year == 2026, Period.month == 8)
    ).first()
    if p is None:
        p = Period(ledger_set_id=ls_id, year=2026, month=8, status="OPEN")
        sess.add(p)
    sess.flush()
    return {"ledger_set_id": ls_id}


def _post(sess, env, lines, voucher_date="2026-08-10"):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date=voucher_date, summary="x", lines=lines,
    )
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()


def test_cash_flow_categories_structure(env, sess):
    _post(sess, env, [
        {"account_code": "100201", "debit": "10000", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "10000"},
    ])
    _post(sess, env, [
        {"account_code": "6401", "debit": "2000", "credit": ""},
        {"account_code": "100201", "debit": "", "credit": "2000"},
    ])
    _post(sess, env, [
        {"account_code": "160103", "debit": "5000", "credit": ""},
        {"account_code": "100201", "debit": "", "credit": "5000"},
    ])
    cf = cash_flow(sess, env["ledger_set_id"], 2026, 8)
    # 结构化三类
    assert cf["categories"]["operating"]["in"] == Decimal("10000.00")
    assert cf["categories"]["operating"]["out"] == Decimal("2000.00")
    assert cf["categories"]["operating"]["net"] == Decimal("8000.00")
    assert cf["categories"]["investing"]["out"] == Decimal("5000.00")
    assert cf["categories"]["investing"]["net"] == Decimal("-5000.00")
    # 向后兼容键
    assert cf["operating"] == Decimal("8000.00")
    assert cf["investing"] == Decimal("-5000.00")
    assert cf["net_increase"] == Decimal("3000.00")


def test_cash_flow_item_declarative_override(env, sess):
    _post(sess, env, [
        {"account_code": "100201", "debit": "10000", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "10000"},
    ])
    cf = cash_flow(sess, env["ledger_set_id"], 2026, 8)
    labels = {it["item"] for it in cf["items"]}
    # 6001 的 cash_flow_item 声明式覆盖生效（不再是默认的「经营活动-流入」）
    assert "销售商品、提供劳务收到的现金" in labels
    assert "经营活动-流入" not in labels
    # 该项目名归在经营活动的流入明细里
    op_items = {it["item"] for it in cf["categories"]["operating"]["items"]}
    assert "销售商品、提供劳务收到的现金" in op_items
