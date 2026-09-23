"""Phase E / E1 运营财务本体层（TDD）。

DoD：
- build_graph：由凭证 + ArapClearing 重建节点/边；invoice↔payment cleared_by 边存在；
  统计计数正确；空账套返回空图谱。
- partner_profile：聚合敞口/账龄/未清/待匹配/催收/对账；数字与底层内核一致；只读不改账。
- graph_metrics：AR/AP 总额、敞口 TopN、HHI、对账健康；只读不改账。
- 只读不变量：operating 三个函数均不改账（凭证明细行数不变）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Period, Voucher, VoucherLine
from kernel.operating import build_graph, graph_metrics, partner_profile
from kernel.posting import post_voucher
from kernel.reporting.arap import record_clearing
from kernel.reporting.credit import set_credit_limit
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
    return v


def _line_ids_by_code(sess, voucher_id):
    rows = sess.execute(
        select(VoucherLine, Account.code)
        .join(Account, Account.id == VoucherLine.account_id)
        .where(VoucherLine.voucher_id == voucher_id)
    ).all()
    return {code: line.id for line, code in rows}


# ------------------------------------------------------------ build_graph


def test_build_graph_rebuilds_edges(sess, env):
    v_inv = _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    v_pay = _post(sess, env, [
        {"account_code": "1001", "debit": "600", "credit": ""},
        {"account_code": "1122", "debit": "", "credit": "600",
         "aux_dims": {"customer": "示例科技"}},
    ], voucher_date="2026-08-10")
    ids_inv = _line_ids_by_code(sess, v_inv.id)
    ids_pay = _line_ids_by_code(sess, v_pay.id)
    record_clearing(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        partner="示例科技",
        assignments=[{
            "invoice_line_id": ids_inv["1122"],
            "payment_line_id": ids_pay["1122"],
            "amount": "600",
        }],
        source="test", actor={"id": "u1"},
    )
    sess.commit()

    g = build_graph(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        as_of_date=date(2026, 8, 31),
    )
    types = {n["type"] for n in g["nodes"]}
    assert {"partner", "invoice", "payment", "account", "voucher"} <= types
    cleared = [e for e in g["edges"] if e["type"] == "cleared_by"]
    assert len(cleared) == 1
    assert cleared[0]["amount"] == "600.00"
    assert g["stats"]["clearings"] == 1
    assert g["stats"]["invoices"] == 1
    assert g["stats"]["payments"] == 1
    # 关系边完整：partner→单据、单据→科目、单据→凭证
    assert any(e["type"] == "has_invoice" for e in g["edges"])
    assert any(e["type"] == "uses_account" for e in g["edges"])
    assert any(e["type"] == "from_voucher" for e in g["edges"])


def test_build_graph_empty(sess):
    g = build_graph(
        sess, ledger_set_id="nonexistent", dim_key="customer",
        as_of_date=date(2026, 8, 31),
    )
    assert g["nodes"] == []
    assert g["edges"] == []
    assert g["stats"]["invoices"] == 0


def test_build_graph_partner_filter(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    _post(sess, env, [
        {"account_code": "1122", "debit": "500", "credit": "",
         "aux_dims": {"customer": "另一客户"}},
        {"account_code": "6001", "debit": "", "credit": "500"},
    ], voucher_date="2026-08-06")
    g = build_graph(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        partner="示例科技", as_of_date=date(2026, 8, 31),
    )
    partners = {n["label"] for n in g["nodes"] if n["type"] == "partner"}
    assert partners == {"示例科技"}


# ------------------------------------------------------------ partner_profile


def test_partner_profile_aggregates(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    set_credit_limit(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        partner="示例科技", limit="300", actor={"id": "u1"},
    )
    sess.commit()

    prof = partner_profile(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        partner="示例科技", as_of_date=date(2026, 8, 31),
    )
    assert prof["partner"] == "示例科技"
    assert Decimal(prof["summary"]["exposure"]) == Decimal("1000.00")
    assert prof["summary"]["breach"] is True  # 敞口 1000 > 额度 300
    assert prof["open_items"]["count"] == 1
    assert Decimal(prof["open_items"]["totals"]["balance"]) == Decimal("1000.00")
    assert prof["reconcile"]["partner_assigned"] is True
    # 账龄含该单位
    assert len(prof["aging"]["items"]) == 1
    assert prof["aging"]["items"][0]["partner"] == "示例科技"


def test_partner_profile_unassigned_dimension_flag(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    _post(sess, env, [
        {"account_code": "1122", "debit": "500", "credit": ""},  # 漏挂客户
        {"account_code": "1001", "debit": "", "credit": "500"},
    ], voucher_date="2026-08-06")
    prof = partner_profile(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        partner="示例科技", as_of_date=date(2026, 8, 31),
    )
    # 该单位本身仍完整挂接；但维度整体失配
    assert prof["reconcile"]["partner_assigned"] is True
    assert prof["reconcile"]["dimension_ok"] is False


# ------------------------------------------------------------ graph_metrics


def test_graph_metrics_ar_ap_and_hhi(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    _post(sess, env, [
        {"account_code": "6001", "debit": "800", "credit": ""},
        {"account_code": "2202", "debit": "", "credit": "800",
         "aux_dims": {"supplier": "供货商A"}},
    ], voucher_date="2026-08-06")
    gm = graph_metrics(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        as_of_date=date(2026, 8, 31),
    )
    assert Decimal(gm["ar_total"]) == Decimal("1000.00")
    assert Decimal(gm["ap_total"]) == Decimal("800.00")
    assert any(r["partner"] == "示例科技" for r in gm["exposure_topn"])
    assert gm["hhi"]["partner_count"] == 1
    assert gm["reconcile_health"]["ok"] is True


def test_graph_metrics_reconcile_mismatch(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])
    _post(sess, env, [
        {"account_code": "1122", "debit": "500", "credit": ""},  # 漏挂
        {"account_code": "1001", "debit": "", "credit": "500"},
    ], voucher_date="2026-08-06")
    gm = graph_metrics(
        sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
        as_of_date=date(2026, 8, 31),
    )
    assert gm["reconcile_health"]["ok"] is False
    assert gm["reconcile_health"]["unassigned_count"] == 1


# ------------------------------------------------------------ 只读不变量


def test_operating_readonly(sess, env):
    _post(sess, env, [
        {"account_code": "1122", "debit": "1000", "credit": "",
         "aux_dims": {"customer": "示例科技"}},
        {"account_code": "6001", "debit": "", "credit": "1000"},
    ])

    def _line_count():
        return len(sess.scalars(
            select(VoucherLine.id).where(
                VoucherLine.voucher_id.in_(
                    select(Voucher.id).where(
                        Voucher.ledger_set_id == env["ledger_set_id"])
                )
            )
        ).all())

    before = _line_count()
    build_graph(sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
                as_of_date=date(2026, 8, 31))
    partner_profile(sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
                    partner="示例科技", as_of_date=date(2026, 8, 31))
    graph_metrics(sess, ledger_set_id=env["ledger_set_id"], dim_key="customer",
                  as_of_date=date(2026, 8, 31))
    after = _line_count()
    assert before == after  # 只读，绝不写账
