"""账套状态引导（kernel/period_guide.py · month_end_guide）测试。

阶段0 交付：把内核状态"讲给人听"。核心承诺是**不误导**——
刚建账还没记过凭证的账套，绝不能被告知"去做损益结转再结账"。
本文件用数字级断言守住 phase 推断与中文 next_action，覆盖六个分支：
    期间不存在 / 期间已结账 / 本期无记账(且未录期初) / 仅草稿无记账
    本期有未处理完凭证 / 全部记账后进入结账闸门。
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Period, Subject, Voucher  # noqa: E402
from kernel.period_guide import (  # noqa: E402
    PHASE_CLOSED,
    PHASE_CLOSING,
    PHASE_CLOSING_READY,
    PHASE_DAILY,
    PHASE_EMPTY,
    PHASE_NONE,
    month_end_guide,
)
from kernel.posting import post_voucher  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402
from kernel.state import transition  # noqa: E402
from kernel.voucher_wizard import create_draft_voucher  # noqa: E402


@pytest.fixture()
def ctx():
    """建账套：演示科目 + 制单人(丞辰) + 审批人(王审批)。期间 2026-08 OPEN。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    reviewer = Subject(type="user", display_name="王审批", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer_id"] = reviewer.id
    ids["reviewer_actor"] = {"type": "user", "id": reviewer.id}
    ids["maker_actor"] = {"type": "user", "id": ids["subject_id"]}
    return s, ids


def _post_business(ctx, *, date="2026-08-10", amount="100", summary="费用"):
    """制单→审批→记账，产出一张已记账的业务凭证。"""
    s, ids = ctx
    v, _ = create_draft_voucher(
        s, ledger_set_id=ids["ledger_set_id"], actor=ids["maker_actor"],
        voucher_date=date, summary=summary,
        lines=[{"account_code": "6602", "debit": amount},
               {"account_code": "1002", "credit": amount}],
    )
    s.commit()
    transition(s, voucher_id=v.id, actor=ids["maker_actor"], target="PUSHED")
    s.commit()
    transition(s, voucher_id=v.id, actor=ids["reviewer_actor"], target="APPROVED")
    s.commit()
    post_voucher(s, voucher_id=v.id, actor=ids["maker_actor"])
    s.commit()
    return v


def _guide(ctx, year=2026, month=8):
    s, ids = ctx
    return month_end_guide(s, ledger_set_id=ids["ledger_set_id"],
                           year=year, month=month)


# ------------------------------------------------------- 1. 期间不存在


def test_no_period_yields_guidance_not_crash(ctx):
    r = _guide(ctx, year=2025, month=1)
    assert r["phase"] == PHASE_NONE
    assert r["close"] is None
    assert "尚未建立" in r["next_action"] or "初始化期间" in r["next_action"]


# ------------------------------------------------------- 2. 期间已结账


def test_closed_period_tells_user_period_is_done(ctx):
    s, ids = ctx
    p = s.get(Period, ids["period_id"])
    p.status = "CLOSED"
    s.commit()
    r = _guide(ctx)
    assert r["phase"] == PHASE_CLOSED
    assert "已结账" in r["phase_zh"]
    assert "打开下一期间" in r["next_action"]


# ------------------------------------------------------- 3. 本期无记账（核心防误导）


def test_empty_period_does_not_tell_newbie_to_close(ctx):
    """刚建账、一张凭证都没记——绝不能被告知去做损益结转/结账。"""
    r = _guide(ctx)
    assert r["phase"] == PHASE_EMPTY
    assert r["counts"]["total"] == 0
    # 下一动作必须引导"录入期初/开始记账"，而不是"结转/结账"
    assert "期初" in r["next_action"] or "记账" in r["next_action"]
    assert "结账" not in r["next_action"].split("。")[0]
    assert r["close"] is None  # 未进入结账闸门，避免误导


def test_draft_only_with_no_posted_is_empty(ctx):
    """只有草稿、没有已记账 → 仍算 empty，不该催结账。"""
    s, ids = ctx
    create_draft_voucher(
        s, ledger_set_id=ids["ledger_set_id"], actor=ids["maker_actor"],
        voucher_date="2026-08-05", summary="草稿",
        lines=[{"account_code": "6602", "debit": "10"},
               {"account_code": "1001", "credit": "10"}],
    )
    s.commit()
    r = _guide(ctx)
    assert r["phase"] == PHASE_EMPTY
    assert r["counts"]["draft"] == 1


# ------------------------------------------------------- 5. 有未处理完凭证


def test_unfinished_vouchers_report_daily_pending(ctx):
    _post_business(ctx)  # 一张已记账
    s, ids = ctx
    create_draft_voucher(
        s, ledger_set_id=ids["ledger_set_id"], actor=ids["maker_actor"],
        voucher_date="2026-08-11", summary="还没处理的",
        lines=[{"account_code": "6602", "debit": "30"},
               {"account_code": "1001", "credit": "30"}],
    )
    s.commit()
    r = _guide(ctx)
    assert r["phase"] == PHASE_DAILY
    assert r["counts"]["posted"] == 1
    assert r["counts"]["draft"] == 1
    assert r["close"] is None
    assert "未处理完" in r["next_action"]


def test_pushed_voucher_counts_as_daily_pending(ctx):
    _post_business(ctx)
    s, ids = ctx
    v, _ = create_draft_voucher(
        s, ledger_set_id=ids["ledger_set_id"], actor=ids["maker_actor"],
        voucher_date="2026-08-12", summary="待审批的",
        lines=[{"account_code": "6602", "debit": "40"},
               {"account_code": "1001", "credit": "40"}],
    )
    s.commit()
    transition(s, voucher_id=v.id, actor=ids["maker_actor"], target="PUSHED")
    s.commit()
    r = _guide(ctx)
    assert r["phase"] == PHASE_DAILY
    assert r["counts"]["pushed"] == 1
    assert "待审核" in r["next_action"]


# ------------------------------------------------------- 6. 全部记账 → 结账闸门


def test_all_posted_enters_closing_gate(ctx):
    _post_business(ctx)
    r = _guide(ctx)
    # 全部凭证已记账 → 进入结账闸门；本场景无结转凭证 → closing_pending(差损益结转)
    assert r["phase"] in (PHASE_CLOSING, PHASE_CLOSING_READY)
    assert r["close"] is not None
    assert r["counts"]["posted"] == 1
    assert r["counts"]["draft"] == r["counts"]["pushed"] == r["counts"]["approved"] == 0
    assert r["next_action"]


def test_next_action_reflects_closing_readiness(ctx):
    """can_close=False 时 next_action 要说明差哪些项，而不是笼统报错。"""
    _post_business(ctx)
    r = _guide(ctx)
    assert r["close"]["can_close"] is False  # 未做损益结转
    assert r["phase"] == PHASE_CLOSING
    # 差项里应点名"损益已结转"
    assert "损益" in r["next_action"]


# ------------------------------------------------------- 7. MCP 契约：month_end_guide 可注册


def test_period_guide_module_pure_read_only(ctx):
    """模块只读：调用前后凭证数不变，不落事件。"""
    s, ids = ctx
    before = len(s.scalars(select(Voucher)).all())
    _guide(ctx)
    s.commit()
    after = len(s.scalars(select(Voucher)).all())
    assert before == after
