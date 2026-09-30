"""JEV-Decide 确定性决策引擎单测：三原语 + P0 六个财务决策点。

全部复用既有单一真源（amounts_by_code / arap.open_items / anomaly.rule_scan /
budget.budget_vs_actual），不写账、不进余额投影、不改任何状态。
"""

import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import (
    Account,
    Balance,
    Budget,
    BudgetLine,
    LedgerSet,
    Period,
    Subject,
    Voucher,
    VoucherLine,
    utcnow,
)

UTC = timezone.utc
COA = {
    "1002": ("银行存款", "debit", "asset"),
    "2202": ("应付账款", "credit", "liability"),
    "6602": ("管理费用", "debit", "pnl"),
    "6001": ("主营业务收入", "credit", "pnl"),
}


def _new_engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def _build():
    """构建一个覆盖六个决策点的最小账套（直接落库，绕开审批状态机）。"""
    engine, path = _new_engine()
    s = Session(engine)
    ls = LedgerSet(
        name="JEV 测试账套", accounting_standard="small_business",
        functional_currency="CNY",
    )
    s.add(ls)
    s.flush()
    accs = {}
    for code, (nm, dr, cat) in COA.items():
        a = Account(ledger_set_id=ls.id, code=code, name=nm, direction=dr, category=cat)
        s.add(a)
        s.flush()
        accs[code] = a
    per = Period(ledger_set_id=ls.id, year=2026, month=9, status="OPEN")
    s.add(per)
    s.flush()
    subj = Subject(type="user", display_name="制单员", autonomy_level=3)
    s.add(subj)
    s.flush()

    day = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # 工作日上午（非 off_hours）

    def add_voucher(no, summary, lines, created_at=day, status="POSTED"):
        v = Voucher(
            ledger_set_id=ls.id, period_id=per.id, voucher_no=no,
            voucher_date=created_at.date(), status=status, summary=summary,
            created_by=subj.id, created_at=created_at,
        )
        s.add(v)
        s.flush()
        for i, (code, debit, credit, aux) in enumerate(lines):
            s.add(VoucherLine(
                voucher_id=v.id, line_no=i + 1, account_id=accs[code].id,
                debit=Decimal(str(debit)), credit=Decimal(str(credit)),
                aux_dims=aux,
            ))
        s.flush()
        return v

    # 实际数（Balance 投影）：6602 本月借方 8000
    s.add(Balance(
        ledger_set_id=ls.id, period_id=per.id, account_id=accs["6602"].id,
        dims_key="", debit_total=Decimal("8000"), credit_total=0,
    ))
    # ACTIVE 预算：6602 9 月 6000
    b = Budget(
        ledger_set_id=ls.id, name="2026 预算", fiscal_year=2026, version=1,
        status="ACTIVE",
    )
    s.add(b)
    s.flush()
    s.add(BudgetLine(budget_id=b.id, account_code="6602", period=9, amount=Decimal("6000")))

    # 费用超标凭证（6602 借 8000 > 单笔上限 5000）
    add_voucher("记-1", "市场推广费", [("6602", 8000, 0, None), ("1002", 0, 8000, None)])
    # 合规费用凭证（6602 借 3000，唯一摘要）
    add_voucher("记-2", "办公用品", [("6602", 3000, 0, None), ("1002", 0, 3000, None)])
    # 应付发票（未清项）：借 6602 5000 / 贷 2202 5000，supplier=SUP-A
    add_voucher(
        "付-1", "采购发票",
        [("6602", 5000, 0, None), ("2202", 0, 5000, {"supplier": "SUP-A"})],
    )
    # 大额凭证（触发 rule_scan large_amount → warn）
    add_voucher("付-2", "大额付款", [("1002", 20000, 0, None), ("2202", 0, 20000, None)])
    # 重复凭证对（同创建人/金额/摘要/时间窗）
    add_voucher("记-3", "快递费", [("6602", 900, 0, None), ("1002", 0, 900, None)])
    add_voucher("记-4", "快递费", [("6602", 900, 0, None), ("1002", 0, 900, None)])

    s.commit()
    ls_id, per_id, subj_id = ls.id, per.id, subj.id
    s.close()
    return engine, path, ls_id, per_id, subj_id


# ---------------------------------------------------------- 三原语


def test_score_primitive_bands():
    from kernel.decide import Severity, score

    green = score(value=Decimal("3"), bands=[
        (Decimal("5"), Severity.LOW, "绿"), (Decimal("15"), Severity.MEDIUM, "黄"),
        (Decimal("10_000_000"), Severity.HIGH, "红"),
    ], label="t")
    assert green.severity == Severity.LOW
    red = score(value=Decimal("20"), bands=[
        (Decimal("5"), Severity.LOW, "绿"), (Decimal("15"), Severity.MEDIUM, "黄"),
        (Decimal("10_000_000"), Severity.HIGH, "红"),
    ], label="t")
    assert red.severity == Severity.HIGH


def test_classify_primitive_ambiguous():
    from kernel.decide import Severity, classify

    clear = classify(
        choices=["a", "b"], scores={"a": Decimal("10"), "b": Decimal("1")}, label="t"
    )
    assert clear.value == "a"
    assert clear.human_review_required is False
    # 歧义：top1-top2 < epsilon
    amb = classify(
        choices=["a", "b"], scores={"a": Decimal("1"), "b": Decimal("1")}, label="t"
    )
    assert amb.human_review_required is True
    assert amb.severity == Severity.MEDIUM


def test_select_primitive_no_constraint():
    from kernel.decide import Severity, select

    ok = select(options=["x", "y"], ranked=["x", "y"], label="t")
    assert ok.value == "x"
    fail = select(
        options=["x"], ranked=[], label="t", hard_constraint_met=False
    )
    assert fail.value is None
    assert fail.human_review_required is True
    assert fail.severity == Severity.HIGH


# ---------------------------------------------------------- 决策点


def test_budget_variance_red():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        d = run_decision(
            "budget_variance", s, ledger_set_id=ls_id, fiscal_year=2026, period_month=9
        )
    assert d.kind == "score"
    assert d.severity.value == "high"  # 偏差率 33.3% → 红
    ev = d.to_dict()["evidence"]
    assert Decimal(ev["total_actual"]) == Decimal("8000.00")
    engine.dispose()
    os.remove(path)


def test_risk_severity_medium():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        # 找到大额凭证 付-2
        v = s.scalars(select(Voucher).where(Voucher.voucher_no == "付-2")).first()
        d = run_decision(
            "risk_severity", s, ledger_set_id=ls_id, voucher_id=v.id
        )
    assert d.kind == "score"
    assert d.severity.value == "medium"  # 1 warn → raw=10
    engine.dispose()
    os.remove(path)


def test_duplicate_voucher_detected():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        v = s.scalars(select(Voucher).where(Voucher.voucher_no == "记-3")).first()
        d = run_decision(
            "duplicate_voucher", s, ledger_set_id=ls_id, voucher_id=v.id
        )
    assert d.value == "疑似重复"
    assert d.severity.value == "high"
    ev = d.to_dict()["evidence"]
    assert "记-4" in ev["duplicates"]
    engine.dispose()
    os.remove(path)


def test_ap_open_health_overdue():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        d = run_decision(
            "ap_open_health", s, ledger_set_id=ls_id, partner="SUP-A",
            as_of_date=date(2026, 12, 31),
        )
    assert d.kind == "score"
    assert d.severity.value == "high"  # 应付未清项 >90 天 → 红
    engine.dispose()
    os.remove(path)


def test_expense_compliance_over_limit():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        v = s.scalars(select(Voucher).where(Voucher.voucher_no == "记-1")).first()
        d = run_decision(
            "expense_compliance", s, ledger_set_id=ls_id, voucher_id=v.id
        )
    assert d.value == "不合规"
    assert "超标" in d.to_dict()["evidence"]["flags"]
    engine.dispose()
    os.remove(path)


def test_expense_compliance_clean():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        v = s.scalars(select(Voucher).where(Voucher.voucher_no == "记-2")).first()
        d = run_decision(
            "expense_compliance", s, ledger_set_id=ls_id, voucher_id=v.id
        )
    assert d.value == "合规"
    engine.dispose()
    os.remove(path)


def test_approval_route_finance_manager():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import run_decision

        v = s.scalars(select(Voucher).where(Voucher.voucher_no == "记-1")).first()
        d = run_decision(
            "approval_route", s, ledger_set_id=ls_id, voucher_id=v.id
        )
    assert d.kind == "select"
    assert d.value == "finance_manager"  # 8000 ∈ (5000, 50000]
    assert d.human_review_required is False
    engine.dispose()
    os.remove(path)


def test_unknown_decision_errors():
    engine, path, ls_id, per_id, subj_id = _build()
    with Session(engine) as s:
        from kernel.decide import DecideError, list_decisions, run_decision

        lst = list_decisions()
        assert any(x["name"] == "budget_variance" for x in lst)
        try:
            run_decision("nope", s, ledger_set_id=ls_id)
            assert False, "应抛出 DecideError"
        except DecideError as e:
            assert e.code == "UNKNOWN_DECISION"
    engine.dispose()
    os.remove(path)
