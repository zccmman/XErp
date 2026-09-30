"""JEV Web 控制台冒烟测试：GET /jev（决策清单）与 GET /jev?...&decision_type=...（运行单个决策）。

复用测试账套（绕开审批，直接落库）；以演示主体登录后访问路由，校验 200 与关键标记。
"""

import pytest
from datetime import date, datetime, timezone
from decimal import Decimal
from sqlalchemy import create_engine
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

COA = {
    "1002": ("银行存款", "debit", "asset"),
    "2202": ("应付账款", "credit", "liability"),
    "6602": ("管理费用", "debit", "pnl"),
    "6001": ("主营业务收入", "credit", "pnl"),
}
UTC = timezone.utc


def _build(url: str) -> dict:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    ids: dict = {}
    with Session(engine) as s:
        subj = Subject(type="user", display_name="演示", autonomy_level=5)
        s.add(subj)
        s.flush()
        ids["subject_id"] = subj.id

        ls = LedgerSet(
            name="JEV Web 账套", accounting_standard="small_business",
            functional_currency="CNY",
        )
        s.add(ls)
        s.flush()
        ids["ls"] = ls.id
        accs = {}
        for code, (nm, dr, cat) in COA.items():
            a = Account(ledger_set_id=ls.id, code=code, name=nm, direction=dr, category=cat)
            s.add(a)
            s.flush()
            accs[code] = a
        per = Period(ledger_set_id=ls.id, year=2026, month=9, status="OPEN")
        s.add(per)
        s.flush()
        day = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
        v = Voucher(
            ledger_set_id=ls.id, period_id=per.id, voucher_no="记-1",
            voucher_date=date(2026, 9, 15), status="POSTED",
            summary="市场推广费", created_by=subj.id, created_at=day,
        )
        s.add(v)
        s.flush()
        s.add(VoucherLine(
            voucher_id=v.id, line_no=1, account_id=accs["6602"].id,
            debit=Decimal("8000"), credit=0,
        ))
        s.add(VoucherLine(
            voucher_id=v.id, line_no=2, account_id=accs["1002"].id,
            debit=0, credit=Decimal("8000"),
        ))
        s.add(Balance(
            ledger_set_id=ls.id, period_id=per.id, account_id=accs["6602"].id,
            dims_key="", debit_total=Decimal("8000"), credit_total=0,
        ))
        b = Budget(
            ledger_set_id=ls.id, name="2026 预算", fiscal_year=2026, version=1,
            status="ACTIVE",
        )
        s.add(b)
        s.flush()
        s.add(BudgetLine(
            budget_id=b.id, account_code="6602", period=9, amount=Decimal("6000")
        ))
        s.commit()
        ids["ls_id"] = ls.id
        ids["voucher_id"] = v.id
    engine.dispose()
    return ids


@pytest.fixture(scope="module")
def env():
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/web_jev.db"
    ids = _build(url)
    return {"url": url, "ids": ids}


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


def test_jev_listing(client):
    r = client.get("/jev")
    assert r.status_code == 200
    assert "JEV 决策控制台" in r.text
    assert "预算差异分级" in r.text
    assert "应付未清项健康度" in r.text


def test_jev_run_budget_variance(client, env):
    ls_id = env["ids"]["ls_id"]
    r = client.get(
        f"/jev?ledger_set_id={ls_id}&decision_type=budget_variance"
        f"&fiscal_year=2026&period_month=9"
    )
    assert r.status_code == 200
    assert "预算差异分级" in r.text
    assert "high" in r.text  # 偏差率 33.3% → 红


def test_jev_boss_panel(client, env):
    ls_id = env["ids"]["ls_id"]
    r = client.get(f"/ledger/{ls_id}/boss")
    assert r.status_code == 200
    assert "AI 决策引擎" in r.text  # JEV 专属面板标题
    assert "预算差异分级" in r.text  # F11 期间级决策
    assert "应付未清项健康度" in r.text  # F1 期间级决策


def test_jev_voucher_card(client, env):
    vid = env["ids"]["voucher_id"]
    r = client.get(f"/voucher/{vid}")
    assert r.status_code == 200
    assert "JEV 决策" in r.text  # 凭证级内联卡片标题
    assert "费用合规判定" in r.text  # F6：6602 8000>5000 → 不合规
    assert "审批路由" in r.text  # F7：总额 8000 → finance_manager
    assert "重复凭证标记" in r.text  # F3：唯一
    assert "风险严重度" in r.text  # F14

