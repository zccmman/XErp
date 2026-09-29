"""预算编制与对比（ERP 模块纵深）单元测试。

覆盖：编制/列表/取用/克隆升版/激活(同组失效) / 预算v.s.实际(零实际数 + 真实投影数)
/ 输入校验 / 单一真源(actual 来自 amounts_by_code) / 内核零宿主依赖。
"""

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.budget import (
    BudgetError,
    activate_budget,
    budget_vs_actual,
    copy_budget,
    create_budget,
    get_active_budget,
    list_budgets,
)
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Budget, BudgetLine, LedgerSet, Period, Subject
from kernel.seed import seed_demo_ledger


_MODULE = Path(__file__).resolve().parents[1] / "kernel" / "budget.py"
# 红线：预算模块不得 import 任何宿主层（adapters / webapp / mcp）


@pytest.fixture()
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{Path(d) / 'budget.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def session(env):
    from sqlalchemy import create_engine as _ce

    eng = _ce(env["url"])
    s = Session(eng)
    yield s
    s.close()
    eng.dispose()


def _mk_account(s: Session, ls_id: str, code: str, direction="debit", category="损益") -> str:
    # 幂等：seed_demo_ledger 已建部分科目（如 6602），重复建会触发 UNIQUE 冲突
    existing = (
        s.query(Account).filter_by(ledger_set_id=ls_id, code=code).one_or_none()
    )
    if existing is not None:
        return existing.id
    acc = Account(ledger_set_id=ls_id, code=code, name=f"科目{code}",
                  direction=direction, category=category)
    s.add(acc)
    s.flush()
    return acc.id


def _mk_balance(s: Session, ls_id: str, period_id: str, acc_id: str,
                debit: str = "0", credit: str = "0") -> None:
    s.add(Balance(ledger_set_id=ls_id, period_id=period_id, account_id=acc_id,
                  dims_key="", debit_total=Decimal(debit), credit_total=Decimal(credit)))


# ---------------------------------------------------------- 编制与查询


def test_create_budget_then_list_and_get(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    res = create_budget(
        session, ledger_set_id=ls, name="2026 预算", fiscal_year=2026,
        lines=[
            {"account_code": "6602", "period": 8, "amount": "800.00", "note": "管理费月预算"},
            {"account_code": "6001", "period": 0, "amount": "120000.00"},  # 年度收入
        ],
    )
    session.commit()
    assert res["version"] == 1
    assert res["lines_count"] == 2

    lst = list_budgets(session, ls)
    assert len(lst) == 1
    assert lst[0]["status"] == "DRAFT"

    got = get_active_budget(session, ls, 2026)
    assert got is None  # 尚未激活，无 ACTIVE

    full = __import__("kernel.budget", fromlist=["get_budget"]).get_budget(session, res["budget_id"])
    codes = {ln["account_code"] for ln in full["lines"]}
    assert codes == {"6602", "6001"}


def test_version_autoincrements_per_ledger_year(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    a = create_budget(session, ledger_set_id=ls, name="v1", fiscal_year=2026,
                      lines=[{"account_code": "6602", "period": 8, "amount": "1"}])
    b = create_budget(session, ledger_set_id=ls, name="v2", fiscal_year=2026,
                      lines=[{"account_code": "6602", "period": 9, "amount": "1"}])
    session.commit()
    assert a["version"] == 1 and b["version"] == 2


def test_activate_supersedes_same_group(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    v1 = create_budget(session, ledger_set_id=ls, name="v1", fiscal_year=2026,
                       lines=[{"account_code": "6602", "period": 8, "amount": "1"}])
    v2 = create_budget(session, ledger_set_id=ls, name="v2", fiscal_year=2026,
                       lines=[{"account_code": "6602", "period": 9, "amount": "1"}])
    session.commit()
    r1 = activate_budget(session, v1["budget_id"])
    r2 = activate_budget(session, v2["budget_id"])
    session.commit()
    assert r1["status"] == "ACTIVE"
    # 激活 v2 后，同 (账套,年度) 的 v1 自动失效
    get_budget = __import__("kernel.budget", fromlist=["get_budget"]).get_budget
    assert get_budget(session, v1["budget_id"])["status"] == "SUPERSEDED"
    assert r2["superseded"] == [v1["budget_id"]]
    assert get_active_budget(session, ls, 2026)["budget_id"] == v2["budget_id"]


def test_copy_budget_new_version_draft(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    src = create_budget(
        session, ledger_set_id=ls, name="原版", fiscal_year=2026,
        lines=[{"account_code": "6602", "period": 8, "amount": "500"}],
    )
    session.commit()
    cp = copy_budget(session, src["budget_id"], new_name="修订版", new_fiscal_year=2026)
    session.commit()
    assert cp["version"] == 2
    assert cp["status"] == "DRAFT"
    assert cp["copied_from"] == src["budget_id"]
    assert cp["lines_count"] == 1


# ---------------------------------------------------------- 校验


def test_duplicate_line_rejected(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    with pytest.raises(BudgetError):
        create_budget(session, ledger_set_id=ls, name="x", fiscal_year=2026,
                      lines=[{"account_code": "6602", "period": 8, "amount": "1"},
                             {"account_code": "6602", "period": 8, "amount": "2"}])


def test_period_out_of_range_rejected(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    with pytest.raises(BudgetError):
        create_budget(session, ledger_set_id=ls, name="x", fiscal_year=2026,
                      lines=[{"account_code": "6602", "period": 13, "amount": "1"}])


def test_empty_lines_rejected(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    with pytest.raises(BudgetError):
        create_budget(session, ledger_set_id=ls, name="x", fiscal_year=2026, lines=[])


# ---------------------------------------------------------- 预算 v.s. 实际（单一真源）


def test_budget_vs_actual_zero_actuals(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    b = create_budget(
        session, ledger_set_id=ls, name="主预算", fiscal_year=2026,
        lines=[{"account_code": "6602", "period": 8, "amount": "800"}],
    )
    activate_budget(session, b["budget_id"])
    session.commit()
    rep = budget_vs_actual(session, ledger_set_id=ls, fiscal_year=2026, period_month=8)
    assert rep["has_budget"] is True
    row = rep["rows"][0]
    assert row["account_code"] == "6602"
    assert row["budget"] == "800.00"
    assert row["actual"] == "0.00"        # 无实际数
    assert row["variance"] == "-800.00"   # 实际低于预算（费用类=节约）
    assert rep["totals"]["variance"] == "-800.00"


def test_budget_vs_actual_with_projection(session, env) -> None:
    """实际数来自 balances 投影（amounts_by_code 单一真源），与三表同口径。"""
    ls = env["ids"]["ledger_set_id"]
    pid = env["ids"]["period_id"]
    acc_id = _mk_account(session, ls, "6602")
    _mk_balance(session, ls, pid, acc_id, debit="1000")  # 实际管理费 1000
    session.commit()

    b = create_budget(
        session, ledger_set_id=ls, name="主预算", fiscal_year=2026,
        lines=[{"account_code": "6602", "period": 8, "amount": "800"}],
    )
    activate_budget(session, b["budget_id"])
    session.commit()

    rep = budget_vs_actual(session, ledger_set_id=ls, fiscal_year=2026, period_month=8)
    row = rep["rows"][0]
    assert row["actual"] == "1000.00"
    assert row["budget"] == "800.00"
    assert row["variance"] == "200.00"   # 实际高于预算（费用类=超支）
    assert row["source"] == "月度"


def test_budget_vs_actual_annual_line_monthly_split(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    b = create_budget(
        session, ledger_set_id=ls, name="主预算", fiscal_year=2026,
        lines=[{"account_code": "6602", "period": 0, "amount": "12000"}],  # 年度总额
    )
    activate_budget(session, b["budget_id"])
    session.commit()
    rep = budget_vs_actual(session, ledger_set_id=ls, fiscal_year=2026, period_month=8)
    row = rep["rows"][0]
    assert row["budget"] == "1000.00"   # 12000/12 月度均摊
    assert row["source"] == "年度均摊"


def test_budget_vs_actual_no_active_returns_empty(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    rep = budget_vs_actual(session, ledger_set_id=ls, fiscal_year=2026, period_month=8)
    assert rep["has_budget"] is False
    assert rep["rows"] == []


def test_budget_vs_actual_explicit_budget_id(session, env) -> None:
    ls = env["ids"]["ledger_set_id"]
    b1 = create_budget(session, ledger_set_id=ls, name="b1", fiscal_year=2026,
                       lines=[{"account_code": "6602", "period": 8, "amount": "800"}])
    b2 = create_budget(session, ledger_set_id=ls, name="b2", fiscal_year=2026,
                       lines=[{"account_code": "6602", "period": 8, "amount": "900"}])
    activate_budget(session, b1["budget_id"])
    session.commit()
    # 即使 b1 生效，显式指定 b2 也要用 b2
    rep = budget_vs_actual(session, ledger_set_id=ls, fiscal_year=2026,
                           period_month=8, budget_id=b2["budget_id"])
    assert rep["budget_id"] == b2["budget_id"]
    assert rep["rows"][0]["budget"] == "900.00"


# ---------------------------------------------------------- 红线：零宿主依赖


def test_kernel_budget_has_no_host_imports() -> None:
    text = _MODULE.read_text(encoding="utf-8")
    assert "from kernel.adapters" not in text
    assert "import kernel.webapp" not in text
    assert "xerp_mcp" not in text
