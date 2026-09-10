"""② 外币/数量核算：数据模型 + 制单校验（TDD）。

DoD：
- COA 模板属性经 import 落到科目（foreign=yes / quantity=yes），且模板优先合并
  不清除既有属性（100203 同时保留 cash_flow=yes）。
- 外币核算科目（foreign=yes）必须带非本币币种 + 汇率 + 原币借/贷。
- 数量核算科目（quantity=yes）必须带数量（>0）+ 计量单位。
- 普通科目无币种/数量仍正常（向后兼容）。
- 外币凭证可建 + 记账，原币/汇率字段落库。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import attr_is, import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Period
from kernel.ledgerbook import ledger_detail
from kernel.posting import PostingError, post_voucher
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
        p = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=8, status="OPEN")
        sess.add(p)
    sess.flush()
    return ids


def _acc(sess, env, code):
    return sess.scalars(
        select(Account).where(
            Account.ledger_set_id == env["ledger_set_id"], Account.code == code
        )
    ).first()


# ---------- COA 属性落地 ----------

def test_foreign_attr_backfill_merges_not_clobbers(sess, env):
    acc = _acc(sess, env, "100203")
    assert attr_is(acc.attrs, "foreign") is True
    # 模板优先合并：既有 cash_flow=yes 必须保留
    assert attr_is(acc.attrs, "cash_flow") is True


def test_quantity_attr_backfill(sess, env):
    assert attr_is(_acc(sess, env, "1405").attrs, "quantity") is True
    assert attr_is(_acc(sess, env, "1403").attrs, "quantity") is True


# ---------- 外币校验 ----------

def test_foreign_account_requires_currency(sess, env):
    with pytest.raises(PostingError) as ei:
        create_draft_voucher(
            sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
            voucher_date="2026-08-06", summary="x",
            lines=[
                {"account_code": "100203", "debit": "100", "credit": ""},
                {"account_code": "6001", "debit": "", "credit": "100"},
            ],
        )
    assert ei.value.code == "FOREIGN_CCY_REQUIRED"


def test_foreign_account_requires_fx_rate(sess, env):
    with pytest.raises(PostingError) as ei:
        create_draft_voucher(
            sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
            voucher_date="2026-08-06", summary="x",
            lines=[
                {"account_code": "100203", "debit": "710", "credit": "",
                 "currency": "USD", "foreign_debit": "100"},
                {"account_code": "6001", "debit": "", "credit": "710"},
            ],
        )
    assert ei.value.code == "FX_RATE_REQUIRED"


def test_foreign_account_requires_foreign_amount(sess, env):
    with pytest.raises(PostingError) as ei:
        create_draft_voucher(
            sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
            voucher_date="2026-08-06", summary="x",
            lines=[
                {"account_code": "100203", "debit": "710", "credit": "",
                 "currency": "USD", "fx_rate": "7.1"},
                {"account_code": "6001", "debit": "", "credit": "710"},
            ],
        )
    assert ei.value.code == "FOREIGN_AMOUNT_REQUIRED"


# ---------- 数量校验 ----------

def test_quantity_account_requires_quantity(sess, env):
    with pytest.raises(PostingError) as ei:
        create_draft_voucher(
            sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
            voucher_date="2026-08-07", summary="x",
            lines=[
                {"account_code": "1405", "debit": "500", "credit": ""},
                {"account_code": "6001", "debit": "", "credit": "500"},
            ],
        )
    assert ei.value.code == "QUANTITY_REQUIRED"


def test_quantity_account_requires_unit(sess, env):
    with pytest.raises(PostingError) as ei:
        create_draft_voucher(
            sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
            voucher_date="2026-08-07", summary="x",
            lines=[
                {"account_code": "1405", "debit": "500", "credit": "",
                 "quantity": "10"},
                {"account_code": "6001", "debit": "", "credit": "500"},
            ],
        )
    assert ei.value.code == "UNIT_REQUIRED"


# ---------- 向后兼容 + 记账落库 ----------

def test_plain_account_without_fcy_still_ok(sess, env):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date="2026-08-08", summary="普通",
        lines=[
            {"account_code": "6602", "debit": "80", "credit": ""},
            {"account_code": "1001", "debit": "", "credit": "80"},
        ],
    )
    assert v.lines[0].currency is None


def test_foreign_voucher_persists_and_posts(sess, env):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date="2026-08-05", summary="收美元货款",
        lines=[
            {"account_code": "100203", "debit": "710", "credit": "",
             "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
            {"account_code": "6001", "debit": "", "credit": "710"},
        ],
    )
    sess.flush()
    ln = v.lines[0]
    assert ln.currency == "USD"
    assert ln.fx_rate == Decimal("7.1")
    assert ln.foreign_debit == Decimal("100.00")
    assert ln.foreign_credit == ZERO
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()
    assert v.status == "POSTED"


def _approve_post(sess, v):
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()


def test_ledger_detail_quantity_format(sess, env):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date="2026-08-10", summary="购入商品",
        lines=[
            {"account_code": "1405", "debit": "500", "credit": "",
             "quantity": "10", "unit": "件"},
            {"account_code": "1001", "debit": "", "credit": "500"},
        ],
    )
    _approve_post(sess, v)
    d = ledger_detail(sess, ledger_set_id=env["ledger_set_id"],
                      account_code="1405", year=2026, month=8)
    assert d["quantity_enabled"] is True
    assert d["closing_quantity"] == "10.000"
    row = d["rows"][0]
    assert row["quantity"] == "10.000"
    assert row["unit"] == "件"
    assert row["balance_quantity"] == "10.000件"


def test_ledger_detail_foreign_columns(sess, env):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date="2026-08-11", summary="收美元",
        lines=[
            {"account_code": "100203", "debit": "710", "credit": "",
             "currency": "USD", "fx_rate": "7.1", "foreign_debit": "100"},
            {"account_code": "6001", "debit": "", "credit": "710"},
        ],
    )
    _approve_post(sess, v)
    d = ledger_detail(sess, ledger_set_id=env["ledger_set_id"],
                      account_code="100203", year=2026, month=8)
    assert d["functional_currency"] == "CNY"
    row = d["rows"][0]
    assert row["currency"] == "USD"
    assert row["fx_rate"] == "7.1"
    assert row["foreign_debit"] == "100.00"
