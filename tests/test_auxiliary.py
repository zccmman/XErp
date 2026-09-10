"""② 辅助核算报表：按维度透视余额（TDD）。

DoD：
- aux_ledger 按（维度值 × 科目）聚合 POSTED 余额投影。
- aux_summary 跨科目合计每个维度值的净额。
- party_name / account_code / 期间 过滤生效。
- 未知维度报错 BAD_DIM。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, LedgerSet, Party, Period
from kernel.posting import post_voucher
from kernel.reporting.auxiliary import AuxReportError, aux_ledger, aux_summary
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
    # 建一个带 department 维度的费用科目 + 部门往来对象
    acc = Account(
        ledger_set_id=ls_id, code="660299", name="部门费用", direction="debit",
        category="cost", is_leaf=True, aux_dim_defs=["department", "customer"],
    )
    sess.add(acc)
    sess.add(Party(ledger_set_id=ls_id, party_type="department", name="销售部"))
    sess.add(Party(ledger_set_id=ls_id, party_type="department", name="行政部"))
    p = sess.scalars(
        select(Period).where(Period.ledger_set_id == ls_id, Period.year == 2026, Period.month == 8)
    ).first()
    if p is None:
        p = Period(ledger_set_id=ls_id, year=2026, month=8, status="OPEN")
        sess.add(p)
    sess.flush()
    return {"ledger_set_id": ls_id, "acc_id": acc.id}


def _post(sess, env, *, dept, amount, voucher_date="2026-08-10"):
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date=voucher_date, summary="部门费",
        lines=[
            {"account_code": "660299", "debit": str(amount), "credit": "",
             "aux_dims": {"department": dept}},
            {"account_code": "1001", "debit": "", "credit": str(amount)},
        ],
    )
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()


def test_aux_ledger_aggregates_by_dim(env, sess):
    _post(sess, env, dept="销售部", amount="300")
    _post(sess, env, dept="行政部", amount="200")
    rep = aux_ledger(sess, ledger_set_id=env["ledger_set_id"], dim="department")
    assert rep["totals"]["net"] == "500.00"
    vals = {r["dim_value"]: Decimal(r["net"]) for r in rep["rows"]}
    assert vals == {"销售部": Decimal("300.00"), "行政部": Decimal("200.00")}
    # party_id 应解析到已知往来对象
    sales = next(r for r in rep["rows"] if r["dim_value"] == "销售部")
    assert sales["party_id"] is not None


def test_aux_summary_cross_account(env, sess):
    _post(sess, env, dept="销售部", amount="300")
    _post(sess, env, dept="销售部", amount="120", voucher_date="2026-08-12")
    rep = aux_summary(sess, ledger_set_id=env["ledger_set_id"], dim="department")
    item = next(i for i in rep["items"] if i["dim_value"] == "销售部")
    assert item["net"] == "420.00"


def test_aux_ledger_party_filter(env, sess):
    _post(sess, env, dept="销售部", amount="300")
    _post(sess, env, dept="行政部", amount="200")
    rep = aux_ledger(sess, ledger_set_id=env["ledger_set_id"],
                     dim="department", party_name="行政部")
    assert len(rep["rows"]) == 1
    assert rep["rows"][0]["dim_value"] == "行政部"
    assert rep["totals"]["net"] == "200.00"


def test_aux_ledger_account_filter(env, sess):
    _post(sess, env, dept="销售部", amount="300")
    _post(sess, env, dept="行政部", amount="200")
    # 前缀匹配：6602 包含子科目 660299，故能命中
    rep = aux_ledger(sess, ledger_set_id=env["ledger_set_id"],
                     dim="department", account_code="6602")
    assert rep["totals"]["net"] == "500.00"
    # 不相关前缀无命中
    rep2 = aux_ledger(sess, ledger_set_id=env["ledger_set_id"],
                      dim="department", account_code="6601")
    assert rep2["rows"] == []
    assert rep2["totals"]["net"] == "0.00"


def test_aux_ledger_bad_dim(env, sess):
    with pytest.raises(AuxReportError) as ei:
        aux_ledger(sess, ledger_set_id=env["ledger_set_id"], dim="region")
    assert ei.value.code == "BAD_DIM"


def test_aux_ledger_multi_dim_split(env, sess):
    # 一张凭证同时挂 department + customer 两个维度，两个维度都能各自聚合到
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date="2026-08-10", summary="x",
        lines=[
            {"account_code": "660299", "debit": "90", "credit": "",
             "aux_dims": {"department": "销售部", "customer": "甲公司"}},
            {"account_code": "1001", "debit": "", "credit": "90"},
        ],
    )
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()
    rep_dept = aux_ledger(sess, ledger_set_id=env["ledger_set_id"], dim="department")
    assert any(r["dim_value"] == "销售部" for r in rep_dept["rows"])
    rep_cust = aux_ledger(sess, ledger_set_id=env["ledger_set_id"], dim="customer")
    assert any(r["dim_value"] == "甲公司" for r in rep_cust["rows"])
