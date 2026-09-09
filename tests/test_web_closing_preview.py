"""结转预览 · Web 端 TDD（P1 剩余项）。

结账页过去是个黑盒：只有一句「结账条件已满足」加一个按钮，点下去才知道结了多少。
本文件锁定新契约——**点之前就能看见**：

  1. 未结转：展示逐行损益结出明细 + 净利润 + 将生成的凭证号
  2. 无损益发生额：明确告知不会生成结转凭证（不给一张空表装样子）
  3. 已结转：变成「结转结果」并给出可点进详情的凭证链接
  4. 预览金额与内核一致（7500.00），页面不做二次计算
"""

import re
import tempfile
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Period, Subject, Voucher, VoucherLine
from kernel.opening import import_opening_balances
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webclose.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        ls_id = ids["ledger_set_id"]
        import_chart_of_accounts(s, ls_id, load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()
        ids["reviewer"] = reviewer.id

        def book(no, day, lines):
            accs = {a.code: a for a in s.scalars(select(Account)).all()}
            v = Voucher(
                ledger_set_id=ls_id, period_id=ids["period_id"], voucher_no=no,
                voucher_date=date(2026, 8, day), status="DRAFT", summary=no,
                created_by=ids["subject_id"],
            )
            v.lines = [
                VoucherLine(line_no=i + 1, account_id=accs[code].id,
                            debit=Decimal(dr or "0"), credit=Decimal(cr or "0"))
                for i, (code, dr, cr) in enumerate(lines)
            ]
            s.add(v)
            s.flush()
            transition(s, voucher_id=v.id,
                       actor={"type": "user", "id": ids["subject_id"]},
                       target="PUSHED")
            transition(s, voucher_id=v.id,
                       actor={"type": "user", "id": reviewer.id}, target="APPROVED")
            post_voucher(s, voucher_id=v.id,
                         actor={"type": "user", "id": ids["subject_id"]})
            s.commit()

        import_opening_balances(
            s, ledger_set_id=ls_id,
            actor={"type": "user", "id": ids["subject_id"]},
            lines=[
                {"account_code": "100201", "debit": "100000.00", "credit": ""},
                {"account_code": "3001", "debit": "", "credit": "100000.00"},
            ],
        )
        # 收入 10,000；办公费 2,000；差旅 500 → 净利润 7,500
        book("记-A001", 5, [("100201", "10000.00", ""), ("6001", "", "10000.00")])
        book("记-A002", 10, [("660202", "2000.00", ""), ("100201", "", "2000.00")])
        book("记-A003", 12, [("660203", "500.00", ""), ("100201", "", "500.00")])
        # 空期间：9 月无任何业务
        s.add(Period(ledger_set_id=ls_id, year=2026, month=9, status="OPEN"))
        s.commit()
        p = s.get(Period, ids["period_id"])
        ids["year"], ids["month"] = p.year, p.month
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


def test_close_page_shows_closing_preview(client, env):
    """未结转：逐行摊开损益结出明细 + 净利润 + 将生成的凭证号。"""
    ls_id = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls_id}/close?year=2026&month=8")
    assert r.status_code == 200
    assert "结转预览" in r.text
    assert "结转-202608-001" in r.text, "应给出将生成的凭证号"
    assert "7500.00" in r.text, "净利润应与内核 preview_closing 一致"
    # 三个损益科目 + 本年利润都在表内
    for code in ("6001", "660202", "660203", "3103"):
        assert code in r.text


def test_preview_empty_period_says_nothing_to_close(client, env):
    """无损益发生额：明说不会生成结转凭证，而不是给一张空表。"""
    ls_id = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls_id}/close?year=2026&month=9")
    assert r.status_code == 200
    assert "无损益类科目发生额" in r.text
    assert "结转-202609-001" not in r.text


def test_after_closing_shows_result_with_voucher_link(client, env):
    """执行结转后：预览区变「结转结果」，并给出可点进详情的凭证链接。"""
    ls_id = env["ids"]["ledger_set_id"]
    r = client.post(f"/ledger/{ls_id}/close",
                    data={"year": 2026, "month": 8}, follow_redirects=False)
    assert r.status_code in (302, 303)
    loc = r.headers.get("location", "")
    assert "error=" not in loc, f"结账被拒：{loc}"
    page = client.get(f"/ledger/{ls_id}/close?year=2026&month=8")
    assert page.status_code == 200
    assert "结转结果" in page.text
    m = re.search(r'<a href="/voucher/([^"]+)">结转-202608-001</a>', page.text)
    assert m, f"应给出结转凭证详情链接：{page.text[:400]}"
    assert "7500.00" in page.text
