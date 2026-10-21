"""预算执行 Web 视图（`/ledger/{ls_id}/budget`）渲染测试。

DoD：
- 有 ACTIVE 预算时渲染 KPI（预算合计 / 实际合计 / 差异 / 执行率）+ 逐科目对比行，
  金额与差异符号由内核 `budget_vs_actual` 单一真源决定（ADR-002）；
- 年度总额行（period=0）按「年度均摊」计入当月，并在预算来源列标注；
- 无 ACTIVE 预算时安全降级（列出已有版本 / 指引激活路径），绝不 500；
- 指定 budget_id 时按该版本对比；
- 全程只读：渲染前后预算行数与余额行数不变。
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kernel.budget import activate_budget, create_budget
from kernel.db.base import Base
from kernel.db.models import Balance, Budget, BudgetLine
from kernel.seed import seed_demo_ledger


def _make_env(name: str) -> dict:
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/{name}.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        # 实际数（2026-08）：管理费用借方 1000、主营业务收入贷方 5000
        s.add(Balance(
            ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
            account_id=ids["expense_account_id"], dims_key="",
            debit_total=Decimal("1000"), credit_total=Decimal("0"),
        ))
        s.add(Balance(
            ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
            account_id=ids["revenue_account_id"], dims_key="",
            debit_total=Decimal("0"), credit_total=Decimal("5000"),
        ))
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture(scope="module")
def env():
    return _make_env("budget_web")


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post(
        "/login",
        data={"subject_id": env["ids"]["subject_id"], "password": ""},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture(scope="module")
def budget(env):
    """建一套 2026 年度预算并激活。

    - 6602 管理费用：8 月 800（实际 1000 → 超支 +200）
    - 6001 主营业务收入：年度总额 120000（当月均摊 10000，实际 5000 → 差异 -5000）
    """
    engine = create_engine(env["url"])
    with Session(engine) as s:
        created = create_budget(
            s,
            ledger_set_id=env["ids"]["ledger_set_id"],
            name="2026 年度预算",
            fiscal_year=2026,
            lines=[
                {"account_code": "6602", "period": 8, "amount": "800"},
                {"account_code": "6001", "period": 0, "amount": "120000"},
            ],
        )
        activate_budget(s, created["budget_id"])
        s.commit()
    engine.dispose()
    return created


def test_budget_page_renders_kpis(client, env, budget) -> None:
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}/budget")
    assert r.status_code == 200, r.text
    t = r.text
    assert "预算执行" in t
    assert "预算合计" in t and "实际合计" in t and "执行率" in t
    # 预算合计 = 800（6602 当月）+ 10000（120000 / 12 年度均摊）= 10800
    assert "10800.00" in t
    # 实际合计 = 1000（费用）+ 5000（收入）= 6000；差异 = -4800；执行率 55.6%
    assert "6000.00" in t
    assert "-4800.00" in t
    assert "55.6%" in t
    # 科目编码与名称（模板来自 seed 演示科目）
    assert "6602" in t and "管理费用" in t
    assert "6001" in t and "主营业务收入" in t


def test_budget_page_annual_proration_and_variance(client, env, budget) -> None:
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}/budget?year=2026&month=8")
    t = r.text
    # 年度总额行按 12 个月均摊，并在来源列标注
    assert "年度均摊" in t
    assert "10000.00" in t  # 120000 / 12
    # 差异符号：费用超支 +200、收入低于预算 -5000
    assert "200.00</td>" in t
    assert "-5000.00" in t
    # 单一真源口径披露
    assert "正差异" in t


def test_budget_page_explicit_version(client, env, budget) -> None:
    bid = budget["budget_id"]
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}/budget?budget_id={bid}")
    assert r.status_code == 200
    assert "2026 年度预算" in r.text
    assert "对比基准" in r.text


def test_budget_page_is_readonly(client, env, budget) -> None:
    engine = create_engine(env["url"])
    with Session(engine) as s:
        before = (
            s.scalar(select(func.count()).select_from(Budget)),
            s.scalar(select(func.count()).select_from(BudgetLine)),
            s.scalar(select(func.count()).select_from(Balance)),
        )
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}/budget")
    assert r.status_code == 200
    with Session(engine) as s:
        after = (
            s.scalar(select(func.count()).select_from(Budget)),
            s.scalar(select(func.count()).select_from(BudgetLine)),
            s.scalar(select(func.count()).select_from(Balance)),
        )
    engine.dispose()
    assert before == after


def test_budget_page_empty_state() -> None:
    """无任何预算：安全降级为指引（不 500、不臆造数字）。"""
    empty = _make_env("budget_web_empty")
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(empty["url"]))
    c.post("/login", data={"subject_id": empty["ids"]["subject_id"], "password": ""},
           follow_redirects=False)
    r = c.get(f"/ledger/{empty['ids']['ledger_set_id']}/budget")
    assert r.status_code == 200, r.text
    assert "尚无任何预算" in r.text
    assert "只读" in r.text
