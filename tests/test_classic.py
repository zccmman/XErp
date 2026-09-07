"""怀旧兼容层（kernel/classic.py）测试。

验证三件事：
    1. 凭证分类编号：收/付/转判定 + 类别内独立编号
    2. 状态术语映射：POSTED → 已记账，且不改变枚举本身
    3. 结账体检：四道闸门逐条给出结论，而不是在首个失败处抛异常

另有一条**回归红线**：不传 voucher_type 时编号规则必须与历史完全一致
（记-0001 起、按账套总数递增），否则会破坏已有账套的凭证号连续性。
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.classic import (  # noqa: E402
    classify_voucher_type,
    is_cash_bank,
    period_zh,
    precheck_close,
    status_zh,
    suggest_summaries,
    voucher_prefix,
)
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Period, Subject, Voucher  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402
from kernel.voucher_wizard import (  # noqa: E402
    create_draft_voucher,
    next_voucher_no,
)


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    s.commit()
    return s, ids


def _actor(ids):
    return {"type": "user", "id": ids["subject_id"]}


# ------------------------------------------------------------ 1. 分类编号


def test_is_cash_bank_recognizes_subaccounts():
    """明细科目（100201 工行存款）也要算现金银行类，否则分流会漏。"""
    assert is_cash_bank("1001") is True
    assert is_cash_bank("1002") is True
    assert is_cash_bank("100201") is True  # 银行存款-工行
    assert is_cash_bank("6602") is False
    assert is_cash_bank("") is False


def test_classify_by_cash_flow_direction():
    """资金流向决定类别：借现金=收，贷现金=付，都无=转。"""
    assert classify_voucher_type(
        [{"account_code": "1001", "debit": "100"},
         {"account_code": "1122", "credit": "100"}]
    ) == "收"

    assert classify_voucher_type(
        [{"account_code": "6602", "debit": "500"},
         {"account_code": "1002", "credit": "500"}]
    ) == "付"

    assert classify_voucher_type(
        [{"account_code": "6602", "debit": "800"},
         {"account_code": "2202", "credit": "800"}]
    ) == "转"


def test_classify_prefers_payment_when_both_sides_hit():
    """提现/内部划转两侧都涉及银行时归为付款——付款更需要被单独盯住。"""
    assert classify_voucher_type(
        [{"account_code": "1001", "debit": "1000"},
         {"account_code": "1002", "credit": "1000"}]
    ) == "付"


def test_classify_ignores_zero_amount_lines():
    """金额为 0 的行不参与判定，避免空行把转账误判成收/付。"""
    assert classify_voucher_type(
        [{"account_code": "1001", "debit": "0"},
         {"account_code": "6602", "debit": "10"},
         {"account_code": "2202", "credit": "10"}]
    ) == "转"


def test_prefix_mapping_degrades_to_uniform():
    assert voucher_prefix("付") == "付-"
    assert voucher_prefix(None) == "记-"
    assert voucher_prefix("乱写的") == "记-"  # 未知类别不应产生怪异编号


# ---------------------------------------------------- 2. 编号规则与回归红线


def test_next_voucher_no_default_behavior_unchanged(ctx):
    """回归红线：默认按账套总数递增，与历史完全一致。"""
    s, ids = ctx
    assert next_voucher_no(s, ids["ledger_set_id"]) == "记-0001"


def test_next_voucher_no_counts_per_prefix_independently(ctx):
    """经典模式：收/付/转各自从 1 号起，互不占用序号。"""
    s, ids = ctx
    ls = ids["ledger_set_id"]
    actor = _actor(ids)

    create_draft_voucher(
        s, ledger_set_id=ls, actor=actor, voucher_date="2026-08-05",
        summary="收款", lines=[{"account_code": "1001", "debit": "100"},
                               {"account_code": "1122", "credit": "100"}],
        prefix="收-", per_prefix=True,
    )
    create_draft_voucher(
        s, ledger_set_id=ls, actor=actor, voucher_date="2026-08-06",
        summary="付款", lines=[{"account_code": "6602", "debit": "50"},
                               {"account_code": "1002", "credit": "50"}],
        prefix="付-", per_prefix=True,
    )
    create_draft_voucher(
        s, ledger_set_id=ls, actor=actor, voucher_date="2026-08-07",
        summary="再收一笔", lines=[{"account_code": "1001", "debit": "20"},
                                   {"account_code": "1122", "credit": "20"}],
        prefix="收-", per_prefix=True,
    )

    nos = sorted(v.voucher_no for v in s.scalars(select(Voucher)).all())
    assert nos == ["付-0001", "收-0001", "收-0002"]


# -------------------------------------------------------------- 3. 状态术语


def test_status_zh_uses_accountant_vocabulary():
    """术语必须是老会计脱口而出的说法，而不是英文枚举的直译。"""
    assert status_zh("POSTED") == "已记账"
    assert status_zh("DRAFT") == "未审核"
    assert status_zh("PUSHED") == "待审核"
    assert status_zh("APPROVED") == "已审核"
    assert status_zh("REJECTED") == "已驳回"


def test_status_zh_passes_through_unknown_status():
    """未登记状态原样返回——宁可显示原始值，也不能静默吞掉新状态。"""
    assert status_zh("SOMETHING_NEW") == "SOMETHING_NEW"


def test_period_zh():
    assert period_zh("OPEN") == "未结账"
    assert period_zh("CLOSED") == "已结账"


# ------------------------------------------------------------ 4. 结账体检


def test_precheck_reports_missing_period():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    r = precheck_close(s, ledger_set_id="nope", year=2026, month=8)
    assert r["can_close"] is False
    assert "期间不存在" in r["summary"]


def test_precheck_lists_unposted_vouchers(ctx):
    """有未记账凭证时，体检要**点名**是哪几张，而不是笼统说"还有未记账"。"""
    s, ids = ctx
    ls = ids["ledger_set_id"]
    period = s.get(Period, ids["period_id"])

    create_draft_voucher(
        s, ledger_set_id=ls, actor=_actor(ids), voucher_date="2026-08-05",
        summary="未记账的一张",
        lines=[{"account_code": "6602", "debit": "10"},
               {"account_code": "1001", "credit": "10"}],
    )
    s.commit()

    r = precheck_close(s, ledger_set_id=ls, year=period.year, month=period.month)
    unposted = [c for c in r["checks"] if c["item"] == "本月凭证已全部记账"][0]
    assert unposted["passed"] is False
    assert "记-0001" in unposted["detail"]
    assert r["can_close"] is False


def test_precheck_summary_is_human_readable(ctx):
    """summary 要能原样念给用户听——这是体检报告存在的意义。"""
    s, ids = ctx
    period = s.get(Period, ids["period_id"])
    r = precheck_close(s, ledger_set_id=ids["ledger_set_id"],
                       year=period.year, month=period.month)
    assert isinstance(r["summary"], str) and r["summary"]
    assert r["period"] == f"{period.year}-{period.month:02d}"
    assert r["period_status_zh"] in ("未结账", "结账中", "已结账")


# ------------------------------------------------------------ 5. 摘要记忆


def test_suggest_summaries_ranks_by_usage(ctx):
    """同一摘要出现多次应排在前面——常用摘要的本质就是使用频率。"""
    s, ids = ctx
    ls = ids["ledger_set_id"]
    actor = _actor(ids)

    base = [{"account_code": "6602", "debit": "10"},
            {"account_code": "1001", "credit": "10"}]
    for i in range(3):
        create_draft_voucher(
            s, ledger_set_id=ls, actor=actor, voucher_date=f"2026-08-0{5+i}",
            summary="付房租", lines=base,
        )
    create_draft_voucher(
        s, ledger_set_id=ls, actor=actor, voucher_date="2026-08-09",
        summary="付水电", lines=base,
    )
    s.commit()

    items = suggest_summaries(s, ledger_set_id=ls, limit=5)
    assert items[0] == {"summary": "付房租", "used_count": 3}
    assert {"summary": "付水电", "used_count": 1} in items


def test_suggest_summaries_filters_by_account(ctx):
    s, ids = ctx
    ls = ids["ledger_set_id"]
    actor = _actor(ids)

    create_draft_voucher(
        s, ledger_set_id=ls, actor=actor, voucher_date="2026-08-05",
        summary="管理费摘要",
        lines=[{"account_code": "6602", "debit": "10"},
               {"account_code": "1001", "credit": "10"}],
    )
    s.commit()

    hit = suggest_summaries(s, ledger_set_id=ls, account_code="6602")
    assert any(i["summary"] == "管理费摘要" for i in hit)

    # 换一个没用过的科目，应为空
    assert suggest_summaries(s, ledger_set_id=ls, account_code="9999") == []


def test_suggest_summaries_on_empty_ledger_returns_empty(ctx):
    s, ids = ctx
    assert suggest_summaries(s, ledger_set_id=ids["ledger_set_id"]) == []


# ------------------------------------------------------- 6. MCP 工具已注册


def _tool_names() -> set[str]:
    import asyncio

    from xerp_mcp.server import build_server

    return {t.name for t in asyncio.run(build_server().list_tools())}


def test_classic_tools_registered():
    assert {"precheck_close", "suggest_summaries"} <= _tool_names()


def test_create_voucher_accepts_voucher_type():
    """分类编号开关必须落到签名上，否则 Skill 教 AI 传了也没用。"""
    import asyncio

    from xerp_mcp.server import build_server

    t = next(
        t for t in asyncio.run(build_server().list_tools()) if t.name == "create_voucher"
    )
    assert "voucher_type" in t.parameters.get("properties", {})
