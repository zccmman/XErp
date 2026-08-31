"""P1-04 TDD：审计增强——AI 决策留痕 + 账账核对。

留痕：默认只存 prompt 哈希（不落敏感原文）、工具调用要点、输出摘要；
对账：逐凭证平衡 / 投影 vs 凭证明细重算 / 试算平衡 / 现金流勾稽。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.agent_audit import log_agent_decision
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Subject, Voucher, VoucherLine
from kernel.events import E
from kernel.ledger import verify_chain
from kernel.opening import import_opening_balances
from kernel.posting import post_voucher
from kernel.reconcile import reconcile_ledger
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer"] = reviewer.id

    def book(no: str, lines, d: str = "2026-08-05"):
        accs = {a.code: a for a in s.scalars(select(Account)).all()}
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
            voucher_no=no, voucher_date=date.fromisoformat(d), status="DRAFT",
            summary=no, created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=i + 1, account_id=accs[code].id,
                        debit=Decimal(dr or "0"), credit=Decimal(cr or "0"))
            for i, (code, dr, cr) in enumerate(lines)
        ]
        s.add(v)
        s.flush()
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED")
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer"]}, target="APPROVED")
        post_voucher(s, voucher_id=v.id,
                     actor={"type": "user", "id": ids["subject_id"]})
        s.commit()
        return v.id

    ids["book"] = book
    # 期初 + 两笔业务
    import_opening_balances(
        s, ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=[
            {"account_code": "100201", "debit": "50000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "50000.00"},
        ],
    )
    book("记-B001", [("100201", "8000.00", ""), ("6001", "", "8000.00")])
    book("记-B002", [("660202", "1500.00", ""), ("100201", "", "1500.00")])
    s.commit()
    yield {"s": s, "ids": ids}
    s.close()


def test_agent_decision_logged_with_prompt_hash_only(ctx):
    s, ids = ctx["s"], ctx["ids"]
    evt = log_agent_decision(
        s,
        ledger_set_id=ids["ledger_set_id"],
        actor={"type": "agent", "id": "bot-1"},
        prompt="客户说报销招待费 800 元现金，请记账",
        tool_calls=[{"tool": "create_voucher", "args": "{...}",
                     "result_summary": "记-0001 已创建"}],
        output_summary="已创建凭证记-0001，待审批",
        model="demo-model",
    )
    s.commit()
    assert evt.event_type == E.AGENT_DECISION
    assert evt.payload["prompt_sha256"].startswith(  # sha256 长度 64
        evt.payload["prompt_sha256"][:10]
    ) and len(evt.payload["prompt_sha256"]) == 64
    assert "prompt" not in evt.payload            # 默认不落原文
    assert evt.payload["tool_calls"][0]["tool"] == "create_voucher"
    ok, problem = verify_chain(s, ids["ledger_set_id"])
    assert ok and problem is None


def test_agent_decision_include_prompt_opt_in(ctx):
    s, ids = ctx["s"], ctx["ids"]
    evt = log_agent_decision(
        s, ledger_set_id=ids["ledger_set_id"],
        actor={"type": "agent", "id": "bot-2"},
        prompt="含敏感信息的上下文", include_prompt=True,
        output_summary="x",
    )
    s.commit()
    assert evt.payload["prompt"] == "含敏感信息的上下文"


def test_reconcile_clean_ledger_ok(ctx):
    s, ids = ctx["s"], ctx["ids"]
    rep = reconcile_ledger(s, ids["ledger_set_id"], 2026, 8)
    assert rep["ok"] is True and rep["issues"] == []
    assert rep["checks"]["vouchers"] == 3  # 期初 + 2 笔


def test_reconcile_detects_projection_tamper(ctx):
    s, ids = ctx["s"], ctx["ids"]
    # 模拟绕过应用直改投影（篡改）
    b = s.scalars(select(Balance)).first()
    b.debit_total = Decimal(str(b.debit_total)) + Decimal("1.00")
    s.commit()
    rep = reconcile_ledger(s, ids["ledger_set_id"], 2026, 8)
    assert rep["ok"] is False
    kinds = {i["kind"] for i in rep["issues"]}
    assert kinds & {"PROJECTION_MISMATCH", "TRIAL_BALANCE_UNBALANCED"}


def test_reconcile_detects_orphan_balance_row(ctx):
    s, ids = ctx["s"], ctx["ids"]
    b = s.scalars(select(Balance)).first()
    b.account_id = "0" * 32  # 指向不存在的科目
    s.commit()
    rep = reconcile_ledger(s, ids["ledger_set_id"], 2026, 8)
    assert any(i["kind"] == "ORPHAN_BALANCE_ROW" for i in rep["issues"])
