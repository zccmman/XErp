"""Web 月末结账向导卡（超级AI总账 · 阶段2）：TDD。

把引导卡升级为交互式状态机后，会计打开账套首页应看到一张
「5 步流程 + 每步状态 + 一键动作」的卡片，与 MCP month_end_guide 的
steps 同源。本文件守住：

  1. 首页引导卡渲染出 5 个固定步骤与状态徽章
  2. 空账套（已录期初）「日常记账」为下一步且给出业务向导入口
  3. 有未处理完凭证时「处理待办凭证」受阻，给出去审批待办/继续制单入口
  4. 凭证盘点行在有待办时可见
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
    url = f"sqlite:///{d}/web_wizard.db"
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
            summary="向导卡测试",
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


_STEP_LABELS = ["建立本期", "录入期初余额", "日常记账", "处理待办凭证", "月末结账"]


def test_dashboard_renders_five_step_machine(client, env):
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    body = r.text
    assert "本月引导" in body
    for label in _STEP_LABELS:
        assert label in body, f"向导卡应含步骤：{label}"
    # 状态机标记（每步一个 .step 且带状态类）
    assert 'class="step s-' in body
    # 状态徽章文案（已完成 / 下一步 / 受阻 / 未开始）
    assert "已完成" in body and "下一步" in body


def test_empty_phase_opening_is_next_with_import_link(client, env):
    """演示账套（seed 不造凭证）尚无记账也未录期初 → 录入期初为下一步。"""
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    body = r.text
    # 防误导：空账套绝不催结转
    assert "损益尚未结转" not in body
    # 下一步是录入期初，给出导入入口
    assert "录入期初余额" in body
    assert "导入期初余额" in body
    # 状态机徽章文案存在
    assert "下一步" in body


def test_daily_pending_shows_blocked_clear_and_links(client, env):
    _add_voucher(env, "POSTED", "记-W001")
    _add_voucher(env, "DRAFT", "记-W002")
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    body = r.text
    assert "凭证盘点" in body
    assert "未审核草稿 1 张" in body
    assert "已记账 1 张" in body
    # 处理待办凭证受阻，给出两个入口
    assert "去审批待办" in body
    assert "继续制单" in body
    assert "受阻" in body
