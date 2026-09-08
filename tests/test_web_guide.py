"""Web 本月引导卡（超级AI总账 · 阶段0 第二步）：TDD。

DoD：会计打开账套首页，第一眼看到的不是凭证流水，而是
「这个月处于什么阶段、还差什么、下一步干嘛」——与 MCP month_end_guide 同源。

本文件用数字断言守住四个关键场景：
    空账套不催结转（防误导铁律在 Web 端同样生效）/ 有凭证未处理完给盘点与入口
    / 全部记账后展示结账闸门与体检入口 / 已结账给明确收尾提示。
"""

import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Subject, Voucher, VoucherLine
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/web_guide.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
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

    c = TestClient(build_app(env["url"]))
    r = c.post(
        "/login",
        data={"subject_id": env["ids"]["subject_id"], "password": ""},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    return c


def _add_voucher(env, status, no):
    """造一张指定状态的凭证（DRAFT 直接落库；POSTED 走完整状态机）。"""
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
            summary="引导卡测试",
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("50.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("50.00")),
        ]
        s.add(v)
        s.flush()
        if status != "DRAFT":
            transition(s, voucher_id=v.id,
                       actor={"type": "user", "id": ids["subject_id"]},
                       target="PUSHED")
            transition(s, voucher_id=v.id,
                       actor={"type": "user",
                              "id": ids["reviewer_subject_id"]},
                       target="APPROVED")
            if status == "POSTED":
                post_voucher(s, voucher_id=v.id,
                             actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
    engine.dispose()


# ------------------------------------------------ 1. 空账套：引导记账，不催结转

def test_empty_phase_on_dashboard(client, env):
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    body = r.text
    assert "本月引导" in body
    # 防误导铁律：空账套绝不能说「损益尚未结转」这类结账话术
    assert "损益尚未结转" not in body
    assert "期初余额" in body or "日常记账" in body


# -------------------------------------------- 2. 有凭证未处理完：盘点 + 行动入口

def test_daily_pending_shows_counts_and_links(client, env):
    _add_voucher(env, "POSTED", "记-G001")
    _add_voucher(env, "DRAFT", "记-G002")
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    body = r.text
    assert "凭证盘点" in body
    assert "未审核草稿 1 张" in body
    assert "已记账 1 张" in body
    assert "去审批待办" in body
    assert "继续制单" in body


# ------------------------------------------------ 3. 全部记账：展示结账闸门

def _finish_all_drafts(env):
    """把账内所有 DRAFT 凭证走完 状态机+过账（模拟凭证全部收尾）。"""
    from datetime import date  # noqa: F401
    from decimal import Decimal  # noqa: F401

    from sqlalchemy import select

    from kernel.posting import post_voucher
    from kernel.state import transition

    engine = create_engine(env["url"])
    with Session(engine) as s:
        ids = env["ids"]
        drafts = s.scalars(
            select(Voucher).where(
                Voucher.ledger_set_id == ids["ledger_set_id"],
                Voucher.status == "DRAFT",
            )
        ).all()
        for v in drafts:
            transition(s, voucher_id=v.id,
                       actor={"type": "user", "id": ids["subject_id"]},
                       target="PUSHED")
            transition(s, voucher_id=v.id,
                       actor={"type": "user",
                              "id": ids["reviewer_subject_id"]},
                       target="APPROVED")
            post_voucher(s, voucher_id=v.id,
                         actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
    engine.dispose()


def test_closing_gate_shown_when_all_posted(client, env):
    _finish_all_drafts(env)
    _add_voucher(env, "POSTED", "记-G003")
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    body = r.text
    assert "查看结账体检" in body
    # 闸门明细里的未满足项以 × 呈现（损益尚未结转是典型闸门）
    assert "×" in body


# ------------------------------------------------------ 4. 已结账：明确收尾提示

def test_closed_period_shows_done(client, env):
    from sqlalchemy import select

    from kernel.db.models import Period

    engine = create_engine(env["url"])
    with Session(engine) as s:
        p = s.scalars(
            select(Period).where(Period.ledger_set_id == env["ids"]["ledger_set_id"])
        ).first()
        p.status = "CLOSED"
        s.commit()
    engine.dispose()
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    assert "已结账" in r.text
