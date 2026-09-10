"""业务语言向导 · 内核 TDD（S1）。

锁定契约：
  1. 自然语言匹配：说业务 → 命中场景（确定性，同输入同输出）
  2. 预览：返回候选分录 + 账套真实科目名 + 逐行「为什么这么记」+ 借贷平衡
  3. 动态科目：office_supply 按支付方式解析出 库存现金 / 基本存款户
  4. 防乱引导护栏：账套缺科目时 propose 标 missing、不编造
  5. 输入校验：非法金额 / 日期抛 WizardError
"""

import tempfile
from decimal import Decimal

import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/biz.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        ls_id = ids["ledger_set_id"]
        import_chart_of_accounts(s, ls_id, load_template_rows())
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def session(env):
    from sqlalchemy import create_engine
    eng = create_engine(env["url"])
    with Session(eng) as s:
        yield s


def test_match_scenarios_picks_payroll(session):
    from kernel.biz_wizard import match_scenarios

    got = match_scenarios("这个月发了3万工资")
    assert got and got[0]["key"] == "payroll_paid"
    # 同输入确定性
    assert match_scenarios("这个月发了3万工资")[0]["key"] == "payroll_paid"


def test_match_empty_returns_nothing(session):
    from kernel.biz_wizard import match_scenarios

    assert match_scenarios("") == []
    assert match_scenarios("   ") == []


def test_propose_cash_sale_balanced_with_names(session, env):
    from kernel.biz_wizard import propose

    p = propose(session, env["ids"]["ledger_set_id"], "cash_sale",
                {"amount": "1200", "sale_date": "2026-09-10", "customer": "零售客"})
    assert p["balanced"] is True
    assert p["debit"] == p["credit"] == "1200.00"
    # 科目真名解析到位，且带逐行解释
    names = {l["account"]: l["account_name"] for l in p["lines"]}
    assert names["1001"] == "库存现金"
    assert names["6001"] == "主营业务收入"
    assert all(l["why"] for l in p["lines"]), "每行都应有「为什么这么记」解释"
    assert p["missing_accounts"] == []


def test_propose_dynamic_account_by_pay_method(session, env):
    from kernel.biz_wizard import propose

    cash = propose(session, env["ids"]["ledger_set_id"], "office_supply",
                   {"amount": "800", "buy_date": "2026-09-10", "pay_via": "cash"})
    bank = propose(session, env["ids"]["ledger_set_id"], "office_supply",
                   {"amount": "800", "buy_date": "2026-09-10", "pay_via": "bank"})
    cash_credit = [l["account"] for l in cash["lines"] if l["side"] == "credit"][0]
    bank_credit = [l["account"] for l in bank["lines"] if l["side"] == "credit"][0]
    assert cash_credit == "1001"   # 库存现金
    assert bank_credit == "100201"  # 基本存款户


def test_propose_missing_account_flagged_not_invented(session):
    from kernel.biz_wizard import propose

    # 一个没有任何科目的账套 → 预览软提示缺失，绝不编造
    p = propose(session, "no-such-ledger", "cash_sale",
                {"amount": "100", "sale_date": "2026-09-10"})
    assert p["missing_accounts"] == ["1001", "6001"]
    assert all(l["missing"] for l in p["lines"])


def test_propose_rejects_bad_amount(session, env):
    from kernel.biz_wizard import WizardError, propose

    try:
        propose(session, env["ids"]["ledger_set_id"], "cash_sale",
                {"amount": "abc", "sale_date": "2026-09-10"})
        assert False, "非法金额应抛 WizardError"
    except WizardError as e:
        assert e.code == "AMOUNT_INVALID"


def test_propose_rejects_bad_date(session, env):
    from kernel.biz_wizard import WizardError, propose

    try:
        propose(session, env["ids"]["ledger_set_id"], "cash_sale",
                {"amount": "100", "sale_date": "2026/9/10"})
        assert False, "非法日期应抛 WizardError"
    except WizardError as e:
        assert e.code == "DATE_INVALID"
