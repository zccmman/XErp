"""P0-13 TDD：Web 最小界面（HTML 兜底）——工作区/凭证列表/余额表/建账向导。

DoD：浏览器可见凭证与余额，数值与 MCP 工具查询一致。
"""

import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Subject, Voucher, VoucherLine
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/web.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()
        ids["reviewer_subject_id"] = reviewer.id
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    return TestClient(build_app(env["url"]))


def _post_voucher(env, no="记-7001"):
    """直接经内核造一张 POSTED 凭证，作为网页展示数据。"""
    from datetime import date
    from decimal import Decimal

    from kernel.posting import post_voucher
    from kernel.state import transition

    engine = create_engine(env["url"])
    with Session(engine) as s:
        ids = env["ids"]
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=no,
            voucher_date=date(2026, 8, 27),
            status="DRAFT",
            summary="网页展示用",
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("120.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("120.00")),
        ]
        s.add(v)
        s.flush()
        transition(
            s, voucher_id=v.id,
            actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED",
        )
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer_subject_id"]}, target="APPROVED")
        post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
        vid = v.id
    engine.dispose()
    return vid


def test_index_lists_workspace(client, env):
    r = client.get("/")
    assert r.status_code == 200 and "演示账套" in r.text


def test_dashboard_shows_voucher_and_balances(client, env):
    _post_voucher(env, "记-7001")
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}")
    assert r.status_code == 200
    assert "记-7001" in r.text and "POSTED" in r.text
    assert "120.00" in r.text  # 余额投影数值与内核一致


def test_voucher_detail_page(client, env):
    vid = _post_voucher(env, "记-7002")
    r = client.get(f"/voucher/{vid}")
    assert r.status_code == 200
    assert "6602" in r.text and "1001" in r.text and "POSTED" in r.text


def test_init_wizard_creates_ledger(client, env):
    r = client.post("/init", data={"name": "网页向导账套", "owner_name": "网主"},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    r2 = client.get("/")
    assert "网页向导账套" in r2.text


def test_opening_balance_import_via_web(client, env):
    client.post("/init", data={"name": "期初向导账套", "owner_name": "OW"},
                follow_redirects=False)
    # 拿到新账套 id（通过工作区页解析）
    home = client.get("/").text
    # 找到该账套的链接 id
    import re

    m = re.search(r"/ledger/([0-9a-f]{32})['\"][^>]*>期初向导账套", home)
    assert m, home[:500]
    ls = m.group(1)
    r2 = client.post(
        f"/ledger/{ls}/opening",
        data={"lines_text": "1002,200000,\n3001,,200000"},
        follow_redirects=False,
    )
    assert r2.status_code in (302, 303)
    dash = client.get(f"/ledger/{ls}").text
    assert "期初-0001" in dash and "200000.00" in dash
