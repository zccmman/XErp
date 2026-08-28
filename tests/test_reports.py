"""P1-01 TDD：三大报表投影（资产负债表/利润表/现金流量表）。

断言使用独立于报表代码的期望值（手工算出的账），并校验两张表的勾稽恒等式。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Subject, Voucher, VoucherLine
from kernel.opening import import_opening_balances
from kernel.posting import post_voucher
from kernel.reporting.mapping import MAPPINGS, get_mapping
from kernel.reporting.statements import balance_sheet, cash_flow, income_statement
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="报表审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer"] = reviewer.id

    def book(d: str, summary: str, lines: list[tuple[str, str, str]]) -> str:
        """建一笔完整过账凭证，返回凭证号。"""
        accs = {a.code: a for a in s.scalars(select(Account)).all()}
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=f"记-R{abs(hash((d, summary))) % 9000 + 1000}",
            voucher_date=date.fromisoformat(d),
            status="DRAFT",
            summary=summary,
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(
                line_no=i + 1,
                account_id=accs[code].id,
                debit=Decimal(dr or "0"),
                credit=Decimal(cr or "0"),
            )
            for i, (code, dr, cr) in enumerate(lines)
        ]
        s.add(v)
        s.flush()
        transition(
            s,
            voucher_id=v.id,
            actor={"type": "user", "id": ids["subject_id"]},
            target="PUSHED",
        )
        transition(
            s,
            voucher_id=v.id,
            actor={"type": "user", "id": ids["reviewer"]},
            target="APPROVED",
        )
        post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
        return v.voucher_no

    ids["book"] = book
    ids["session"] = s
    yield ids
    s.close()


def _setup(ctx):
    s, ids = ctx["session"], ctx
    import_opening_balances(
        s,
        ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=[
            {"account_code": "100201", "debit": "100000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "100000.00"},
        ],
    )
    s.commit()
    ctx["book"]("2026-08-05", "课程收入", [
        ("100201", "10000.00", ""), ("6001", "", "10000.00")])
    ctx["book"]("2026-08-10", "办公费", [
        ("660202", "2000.00", ""), ("100201", "", "2000.00")])
    ctx["book"]("2026-08-20", "购设备", [
        ("160103", "5000.00", ""), ("100201", "", "5000.00")])


def test_income_statement_matches_manual_calc(ctx):
    _setup(ctx)
    inc = income_statement(ctx["session"], ctx["ledger_set_id"], 2026, 8)
    assert inc["revenue"] == Decimal("10000.00")          # 6001 贷方
    assert inc["expense"] == Decimal("2000.00")           # 660202 借方
    assert inc["net_profit"] == Decimal("8000.00")        # 手工: 10,000 - 2,000


def test_balance_sheet_balances_including_unclosed_profit(ctx):
    _setup(ctx)
    bs = balance_sheet(ctx["session"], ctx["ledger_set_id"], 2026, 8)
    # 资产 = 银行(100,000+10,000-2,000-5,000=103,000) + 设备 5,000 = 108,000
    assert bs["assets"]["total"] == Decimal("108000.00")
    assert bs["liabilities"]["total"] == Decimal("0.00")
    assert bs["equity"]["total"] == Decimal("108000.00")   # 实收 100,000 + 未结转利润 8,000
    assert bs["balanced"] is True and bs["check"]["diff"] == Decimal("0.00")


def test_cash_flow_reconciles_to_closing_cash(ctx):
    _setup(ctx)
    cf = cash_flow(ctx["session"], ctx["ledger_set_id"], 2026, 8)
    assert cf["operating"] == Decimal("8000.00")           # 流入 10,000 - 流出 2,000
    assert cf["investing"] == Decimal("-5000.00")          # 购设备
    assert cf["net_increase"] == Decimal("3000.00")
    # 勾稽：期初 100,000 + 净增加 3,000 = 期末 103,000（与资产负债表现金一致）
    rec = cf["reconcile"]
    assert rec["closing_cash"] == Decimal("103000.00")
    bs = balance_sheet(ctx["session"], ctx["ledger_set_id"], 2026, 8)
    cash_items = [
        a for it in bs["assets"]["items"] if it["group"] == "流动资产"
        for a in it["accounts"] if a["code"].startswith(("1001", "1002"))
    ]
    assert sum(a["ending"] for a in cash_items) == rec["closing_cash"]


def test_two_standard_mappings_available(ctx):
    assert set(MAPPINGS) >= {"small_business", "enterprise"}
    ent = get_mapping("enterprise")
    items = [name for name, _p, _s in ent["income_statement"]]
    assert "研发费用" in items                      # 企业会计准则多出研发费用项
    assert "研发费用" not in [
        name for name, _p, _s in get_mapping("small_business")["income_statement"]
    ]
    _setup(ctx)
    # 两套准则下利润表都可用，数值一致（本例无研发支出）
    a = income_statement(ctx["session"], ctx["ledger_set_id"], 2026, 8)
    b = income_statement(ctx["session"], ctx["ledger_set_id"], 2026, 8, "enterprise")
    assert a["net_profit"] == b["net_profit"] == Decimal("8000.00")
