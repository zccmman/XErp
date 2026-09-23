"""G7 外币重估：内核只读草稿 + HITL 落库（TDD）。

DoD：
- fx_revaluation_draft 按（科目×币种）累计外币头寸，按期末汇率计提，借贷恒等平衡，只读不改账。
- 缺汇率的币种跳过并记 notes；无外币头寸/缺汇率→needs_revaluation False。
- 汇兑损益科目缺失→FX_ACCOUNT_MISSING。
- create_fx_revaluation_voucher 仅落 PUSHED 凭证（HITL，绝不自动过账），幂等 ALREADY_RUN。
- 无需重估→NO_REVALUATION。
- has_foreign_exposure / fx_revaluation_posted 判定正确。
- 月结 step 2.5 暴露外币重估只读草稿（fx_rates 透传）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Period, Voucher
from kernel.monthend import run_monthend
from kernel.posting import post_voucher
from kernel.reporting.foreign import (
    FxError,
    create_fx_revaluation_voucher,
    fx_revaluation_draft,
    fx_revaluation_posted,
    has_foreign_exposure,
)
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher

ZERO = Decimal("0.00")


class _SpyNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


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


def _count(sess, env):
    return len(sess.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == env["ledger_set_id"])).all())


def test_draft_gain(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    rep = fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                              year=2026, month=8, fx_rates={"USD": "7.3"})
    # 期末 7.3 → 目标本币 730，账面 710，升值 +20
    assert rep["needs_revaluation"] is True
    asset = [l for l in rep["lines"] if l["account_code"] == "100203"][0]
    assert asset["side"] == "debit"
    assert Decimal(asset["amount"]) == Decimal("20.00")
    assert Decimal(asset["foreign_net"]) == Decimal("100.00")
    assert Decimal(asset["target_domestic"]) == Decimal("730.00")
    assert Decimal(asset["carrying_domestic"]) == Decimal("710.00")
    fx = [l for l in rep["lines"] if l.get("fx_gain_loss")][0]
    assert fx["account_code"] == "660304"
    assert fx["side"] == "credit"
    assert Decimal(fx["amount"]) == Decimal("20.00")
    assert Decimal(rep["total_gain"]) == Decimal("20.00")
    assert Decimal(rep["total_loss"]) == Decimal("0.00")
    assert Decimal(rep["net_impact"]) == Decimal("20.00")


def test_draft_balanced(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    rep = fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                              year=2026, month=8, fx_rates={"USD": "7.3"})
    debit = sum((Decimal(l["amount"]) for l in rep["lines"]
                 if l["side"] == "debit"), ZERO)
    credit = sum((Decimal(l["amount"]) for l in rep["lines"]
                  if l["side"] == "credit"), ZERO)
    assert debit == credit


def test_draft_loss(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    rep = fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                              year=2026, month=8, fx_rates={"USD": "6.9"})
    # 期末 6.9 → 目标 690，账面 710，贬值 -20
    asset = [l for l in rep["lines"] if l["account_code"] == "100203"][0]
    assert asset["side"] == "credit"
    assert Decimal(asset["amount"]) == Decimal("20.00")
    fx = [l for l in rep["lines"] if l.get("fx_gain_loss")][0]
    assert fx["side"] == "debit"
    assert Decimal(rep["total_loss"]) == Decimal("20.00")
    assert Decimal(rep["net_impact"]) == Decimal("-20.00")


def test_draft_missing_rate(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    rep = fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                              year=2026, month=8, fx_rates={})
    assert rep["needs_revaluation"] is False
    assert any("USD" in n and "跳过" in n for n in rep["notes"])


def test_draft_no_foreign(sess, env):
    _post(sess, env, [
        {"account_code": "6602", "debit": "80", "credit": ""},
        {"account_code": "1001", "debit": "", "credit": "80"},
    ])
    rep = fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                              year=2026, month=8, fx_rates={"USD": "7.3"})
    assert rep["needs_revaluation"] is False


def test_draft_missing_fx_account(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    with pytest.raises(FxError) as ei:
        fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                            year=2026, month=8, fx_rates={"USD": "7.3"},
                            fx_gain_loss_account="999999")
    assert ei.value.code == "FX_ACCOUNT_MISSING"


def test_draft_readonly(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    before = _count(sess, env)
    fx_revaluation_draft(sess, ledger_set_id=env["ledger_set_id"],
                        year=2026, month=8, fx_rates={"USD": "7.3"})
    assert _count(sess, env) == before  # 只读，绝不写账


def test_has_foreign_exposure(sess, env):
    assert has_foreign_exposure(sess, ledger_set_id=env["ledger_set_id"],
                               year=2026, month=8) is False
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    assert has_foreign_exposure(sess, ledger_set_id=env["ledger_set_id"],
                               year=2026, month=8) is True


def test_create_pushed_not_posted(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    rep = create_fx_revaluation_voucher(
        sess, ledger_set_id=env["ledger_set_id"], year=2026, month=8,
        fx_rates={"USD": "7.3"}, actor={"id": "u3"})
    sess.commit()
    assert rep["voucher"]["status"] == "PUSHED"
    assert "[汇兑损益重估:2026-08]" in rep["voucher"]["summary"]
    v = sess.get(Voucher, rep["voucher"]["id"])
    assert v.status == "PUSHED"  # 绝不自动过账


def test_create_no_revaluation(sess, env):
    _post(sess, env, [
        {"account_code": "6602", "debit": "80", "credit": ""},
        {"account_code": "1001", "debit": "", "credit": "80"},
    ])
    with pytest.raises(FxError) as ei:
        create_fx_revaluation_voucher(
            sess, ledger_set_id=env["ledger_set_id"], year=2026, month=8,
            fx_rates={"USD": "7.3"}, actor={"id": "u3"})
    assert ei.value.code == "NO_REVALUATION"


def test_create_idempotent(sess, env):
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    create_fx_revaluation_voucher(
        sess, ledger_set_id=env["ledger_set_id"], year=2026, month=8,
        fx_rates={"USD": "7.3"}, actor={"id": "u3"})
    sess.commit()
    with pytest.raises(FxError) as ei:
        create_fx_revaluation_voucher(
            sess, ledger_set_id=env["ledger_set_id"], year=2026, month=8,
            fx_rates={"USD": "7.3"}, actor={"id": "u3"})
    assert ei.value.code == "ALREADY_RUN"


def test_posted_flag(sess, env):
    assert fx_revaluation_posted(sess, ledger_set_id=env["ledger_set_id"],
                                year=2026, month=8) is False
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    create_fx_revaluation_voucher(
        sess, ledger_set_id=env["ledger_set_id"], year=2026, month=8,
        fx_rates={"USD": "7.3"}, actor={"id": "u3"})
    sess.commit()
    assert fx_revaluation_posted(sess, ledger_set_id=env["ledger_set_id"],
                                year=2026, month=8) is True


def test_monthend_fx_step_exposes_draft(sess, env):
    """月结 step 2.5：传 fx_rates 即出具只读重估草稿，exposure 与 draft 正确。"""
    _post(sess, env, [
        {"account_code": "100203", "debit": "710", "credit": "",
         "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
        {"account_code": "6001", "debit": "", "credit": "710"},
    ])
    spy = _SpyNotifier()
    rep = run_monthend(sess, ledger_set_id=env["ledger_set_id"], year=2026,
                       month=8, actor={"type": "user", "id": "u1"},
                       notifier=spy, dry_run=True, fx_rates={"USD": "7.3"})
    fx = rep["steps"]["fx_revaluation"]
    assert fx["exposure"] is True
    assert fx["draft"] is not None
    assert fx["draft"]["needs_revaluation"] is True
    assert Decimal(fx["draft"]["total_gain"]) == Decimal("20.00")
