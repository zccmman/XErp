"""复盘 D3 TDD：转账模板——声明式定义/取数公式/执行 PUSHED/幂等/不平衡拦截。

场景：按应收账款余额 5% 计提折旧（借 6701 资产减值损失 / 贷 1141 坏账准备）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Period, Subject, Voucher
from kernel.opening import import_opening_balances
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.transfers import (
    TransferError,
    list_templates,
    register_template,
    run_template,
)

TPL = {
    "name": "计提折旧",
    "period_type": "monthly",
    "description": "固定资产原值 × 10%（演示简化，不计残值）",
    "lines": [
        {"side": "debit", "account": "660205",
         "amount": {"source": "balance", "account": "1601",
                    "scope": "balance", "ratio": 0.10}},
        {"side": "credit", "account": "1602",
         "amount": {"source": "balance", "account": "1601",
                    "scope": "balance", "ratio": 0.10}},
    ],
    "balance_check": True,
}


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    p9 = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=9,
                status="OPEN")
    s.add(p9)
    s.flush()
    maker = Subject(type="user", display_name="验收人", autonomy_level=3)
    approver = Subject(type="user", display_name="审批员", autonomy_level=3)
    s.add_all([maker, approver])
    s.commit()
    act = {"type": "user", "id": maker.id}
    # 期初：应收 10,000 + 固定资产 20,000（折旧模板取数来源）
    import_opening_balances(
        s, ledger_set_id=ids["ledger_set_id"], actor=act,
        period_year=2026, period_month=9,
        lines=[
            {"account_code": "1122", "debit": "10000.00", "credit": ""},
            {"account_code": "1601", "debit": "20000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "30000.00"},
        ])
    s.commit()
    # 9 月赊销（应收余额 → 10,000）
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(ledger_set_id=ids["ledger_set_id"], period_id=p9.id,
                voucher_no="记-0001", voucher_date=_d(2026, 9, 15),
                status="DRAFT", summary="赊销", created_by=maker.id)
    v.lines = [
        _ln(accs["1122"].id, "10000.00", "0.00", 1),
        _ln(accs["6001"].id, "0.00", "10000.00", 2),
    ]
    s.add(v)
    s.flush()
    transition(s, voucher_id=v.id, actor=act, target="PUSHED")
    transition(s, voucher_id=v.id,
               actor={"type": "user", "id": approver.id}, target="APPROVED")
    from kernel.posting import post_voucher

    post_voucher(s, voucher_id=v.id, actor=act)
    s.commit()
    return {"s": s, "ids": ids, "act": act}


def _d(y, m, dd):
    import datetime

    return datetime.date(y, m, dd)


def _ln(acc_id, dr, cr, no):
    from decimal import Decimal

    from kernel.db.models import VoucherLine

    return VoucherLine(line_no=no, account_id=acc_id,
                       debit=Decimal(dr), credit=Decimal(cr))


def test_register_and_list():
    register_template(TPL)
    names = [t["name"] for t in list_templates()]
    assert "计提折旧" in names


def test_register_rejects_bad_template():
    with pytest.raises(TransferError) as ei:
        register_template({**TPL, "lines": [
            {**TPL["lines"][0], "side": "debit"},
            {**TPL["lines"][1], "side": "debit"},
        ]})
    assert ei.value.code == "ONE_SIDED"
    with pytest.raises(TransferError) as ei2:
        register_template({**TPL, "lines": [
            {**TPL["lines"][0], "amount": {"source": "formula"}},
            TPL["lines"][1],
        ]})
    assert ei2.value.code == "BAD_SOURCE"


def test_run_creates_pushed_voucher_with_ratio(ctx):
    """固定资产 20,000 × 10% = 2,000 → PUSHED 凭证（待人审）。"""
    s, ids = ctx["s"], ctx["ids"]
    register_template(TPL)
    res = run_template(s, ledger_set_id=ids["ledger_set_id"],
                       template_name="计提折旧", year=2026, month=9,
                       actor=ctx["act"])
    s.commit()
    v = s.get(Voucher, res["voucher"]["id"])
    assert v.status == "PUSHED"
    lines = sorted(v.lines, key=lambda x: x.line_no)
    from decimal import Decimal

    assert lines[0].debit == Decimal("2000.00")
    assert lines[1].credit == Decimal("2000.00")


def test_run_idempotent(ctx):
    s, ids = ctx["s"], ctx["ids"]
    register_template(TPL)
    run_template(s, ledger_set_id=ids["ledger_set_id"],
                 template_name="计提折旧", year=2026, month=9,
                 actor=ctx["act"])
    s.commit()
    with pytest.raises(TransferError) as ei:
        run_template(s, ledger_set_id=ids["ledger_set_id"],
                     template_name="计提折旧", year=2026, month=9,
                     actor=ctx["act"])
    assert ei.value.code == "ALREADY_RUN"


def test_run_unbalanced_detected(ctx):
    """取数不对称（只借方计提、贷方固定 1 元）→ 不平衡拦截。"""
    s, ids = ctx["s"], ctx["ids"]
    register_template({
        "name": "坏模板", "period_type": "monthly",
        "lines": [
            {"side": "debit", "account": "660205",
             "amount": {"source": "balance", "account": "1601",
                        "scope": "balance", "ratio": 0.10}},
            {"side": "credit", "account": "1602", "amount": {"const": "1.00"}},
        ],
        "balance_check": True,
    })
    with pytest.raises(TransferError) as ei:
        run_template(s, ledger_set_id=ids["ledger_set_id"],
                     template_name="坏模板", year=2026, month=9,
                     actor=ctx["act"])
    assert ei.value.code == "TEMPLATE_UNBALANCED"


def test_run_unknown_template(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(TransferError) as ei:
        run_template(s, ledger_set_id=ids["ledger_set_id"],
                     template_name="不存在", year=2026, month=9,
                     actor=ctx["act"])
    assert ei.value.code == "TEMPLATE_NOT_FOUND"
