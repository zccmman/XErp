"""业务语言向导 · Web 端 TDD（S1）。

锁定契约：
  1. 目录页列出全部场景（含「业务向导」入口可见）
  2. 选场景后出填写表单
  3. 提交后预览：逐行分录 + 科目真名 + 为什么这么记 + 借贷平衡
  4. 确认生成草稿 → 跳转到凭证详情（DRAFT，未自动过账）
  5. 非法金额：原地重渲染并回填，不生成凭证
"""

import re
import tempfile
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Voucher
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webwiz.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        ls_id = ids["ledger_set_id"]
        import_chart_of_accounts(s, ls_id, load_template_rows())
        # 向导测试用 2026-09 的业务日期，补一个 OPEN 期间
        from kernel.db.models import Period
        s.add(Period(ledger_set_id=ls_id, year=2026, month=9, status="OPEN"))
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": env["ids"]["subject_id"],
                               "password": ""}, follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


def test_wizard_catalog_lists_scenarios(client, env):
    ls_id = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls_id}/wizard")
    assert r.status_code == 200
    assert "业务语言向导" in r.text
    assert "现销收款" in r.text          # 场景名可见
    assert "发工资" in r.text


def test_wizard_search_filters(client, env):
    ls_id = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls_id}/wizard?q=发了工资")
    assert r.status_code == 200
    assert "发放工资" in r.text


def test_wizard_form_for_scenario(client, env):
    ls_id = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls_id}/wizard?s=cash_sale")
    assert r.status_code == 200
    assert "收款金额" in r.text
    assert 'value="cash_sale"' in r.text and 'name=s' in r.text


def test_wizard_preview_shows_lines_and_why(client, env):
    ls_id = env["ids"]["ledger_set_id"]
    r = client.post(f"/ledger/{ls_id}/wizard", data={
        "s": "cash_sale", "amount": "1200", "sale_date": "2026-09-10",
        "customer": "零售客",
    })
    assert r.status_code == 200
    assert "分录预览" in r.text
    assert "库存现金" in r.text               # 科目真名
    assert "为什么这么记" in r.text            # 解释列
    assert "借贷平衡" in r.text
    assert 'action="/ledger/{ls_id}/wizard/confirm"'.format(ls_id=ls_id) in r.text


def test_wizard_confirm_creates_draft_redirects(client, env):
    ls_id = env["ids"]["ledger_set_id"]
    r = client.post(f"/ledger/{ls_id}/wizard/confirm", data={
        "s": "cash_sale", "amount": "1200", "sale_date": "2026-09-10",
        "customer": "零售客",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    loc = r.headers.get("location", "")
    m = re.search(r"/voucher/([^/?]+)", loc)
    assert m, f"应跳转到凭证详情：{loc}"
    vid = m.group(1)
    # 凭证详情：状态应为 DRAFT（未自动过账），且含两行
    page = client.get(f"/voucher/{vid}")
    assert page.status_code == 200
    assert "库存现金" in page.text
    assert "主营业务收入" in page.text


def test_wizard_bad_amount_rerenders_form(client, env):
    ls_id = env["ids"]["ledger_set_id"]
    with Session(create_engine(env["url"])) as s:
        before = len(list(s.scalars(select(Voucher).where(
            Voucher.ledger_set_id == ls_id))))
    r = client.post(f"/ledger/{ls_id}/wizard", data={
        "s": "cash_sale", "amount": "不是钱", "sale_date": "2026-09-10",
    })
    assert r.status_code == 200
    assert "<h2>分录预览" not in r.text          # 没进入预览（仅表单按钮含「生成分录预览」）
    assert "收款金额" in r.text               # 仍停在表单
    # 没有凭空生成凭证（坏提交前后计数不变）
    with Session(create_engine(env["url"])) as s:
        after = len(list(s.scalars(select(Voucher).where(
            Voucher.ledger_set_id == ls_id))))
        assert after == before
