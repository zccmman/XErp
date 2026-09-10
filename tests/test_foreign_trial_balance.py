"""② 外币试算平衡：内核聚合（TDD）。

DoD：
- foreign_trial_balance 按（科目 × 币种）汇总本月 POSTED 凭证明细的本币与原币借/贷。
- 纯本币凭证不混入（rows 为空）。
- 同一科目跨多币种时按币种分行，原币分别累计。
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Period
from kernel.posting import post_voucher
from kernel.reporting.foreign import foreign_trial_balance
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


def test_foreign_trial_balance_aggregates(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    rep = foreign_trial_balance(sess, ledger_set_id=env["ledger_set_id"],
                                year=2026, month=8)
    assert rep["functional_currency"] == "CNY"
    assert len(rep["rows"]) == 1
    r = rep["rows"][0]
    assert r["account_code"] == "100203"
    assert r["currency"] == "USD"
    assert Decimal(r["debit"]) == Decimal("710.00")
    assert Decimal(r["credit"]) == Decimal("0.00")
    assert Decimal(r["foreign_debit"]) == Decimal("100.00")
    assert Decimal(r["foreign_credit"]) == Decimal("0.00")
    assert Decimal(rep["totals"]["debit"]) == Decimal("710.00")
    assert Decimal(rep["totals"]["foreign_debit"]) == Decimal("100.00")


def test_foreign_trial_balance_excludes_plain(sess, env):
    _post(sess, env, [
        {"account_code": "6602", "debit": "80", "credit": ""},
        {"account_code": "1001", "debit": "", "credit": "80"},
    ])
    rep = foreign_trial_balance(sess, ledger_set_id=env["ledger_set_id"],
                                year=2026, month=8)
    assert rep["rows"] == []


def test_foreign_trial_balance_multi_currency(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    _post(sess, env, [
        {"account_code": "100203", "debit": "560", "credit": "",
         "currency": "EUR", "fx_rate": "8.0", "foreign_debit": "70"},
        {"account_code": "6001", "debit": "", "credit": "560"},
    ], voucher_date="2026-08-06")
    rep = foreign_trial_balance(sess, ledger_set_id=env["ledger_set_id"],
                                year=2026, month=8)
    assert len(rep["rows"]) == 2
    by_ccy = {r["currency"]: Decimal(r["foreign_debit"]) for r in rep["rows"]}
    assert by_ccy == {"USD": Decimal("100.00"), "EUR": Decimal("70.00")}


def test_foreign_trial_balance_credit_side(sess, env):
    _post(sess, env, [
        {"account_code": "1001", "debit": "710", "credit": ""},
        {"account_code": "100203", "debit": "", "credit": "710",
         "currency": "USD", "fx_rate": "7.1", "foreign_credit": "100"},
    ])
    rep = foreign_trial_balance(sess, ledger_set_id=env["ledger_set_id"],
                                year=2026, month=8)
    r = rep["rows"][0]
    assert r["currency"] == "USD"
    assert Decimal(r["foreign_credit"]) == Decimal("100.00")
    assert Decimal(r["foreign_debit"]) == Decimal("0.00")
