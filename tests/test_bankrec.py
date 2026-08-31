"""P2-04 TDD：银行对账——CSV 导入幂等、自动勾对、未达账项报告。

覆盖：表头校验、流水号幂等、贪心匹配（金额+方向+日期窗）、
银行有账上无、账上有银行无（在途）、勾对事件落链、试算模式。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.bankrec import BankRecError, import_csv, reconcile
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Subject, Voucher, VoucherLine
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
    return {"s": s, "ids": ids,
            "actor": {"type": "user", "id": ids["subject_id"]},
            "reviewer": {"type": "user", "id": reviewer.id}}


def _book(s, ids, ctx, no, d, summary, bank_dr, bank_cr):
    """记一笔银行分录并过账（默认 100201 基本户）。"""
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
        voucher_no=no, voucher_date=d, status="DRAFT", summary=summary,
        created_by=ids["subject_id"],
    )
    # 银行行 + 对方行（收入 6001 / 费用 660202，金额同银行行）
    other = "6001" if bank_dr else "660202"
    v.lines = [
        VoucherLine(line_no=1, account_id=accs["100201"].id,
                    debit=Decimal(bank_dr or 0), credit=Decimal(bank_cr or 0)),
        VoucherLine(line_no=2, account_id=accs[other].id,
                    debit=Decimal(bank_cr or 0), credit=Decimal(bank_dr or 0)),
    ]
    s.add(v)
    s.flush()
    transition(s, voucher_id=v.id, actor=ctx["actor"], target="PUSHED")
    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="APPROVED")
    from kernel.posting import post_voucher

    post_voucher(s, voucher_id=v.id, actor=ctx["actor"])
    s.commit()
    return v


from decimal import Decimal  # noqa: E402  （置于 fixture 后便于阅读）


def test_import_csv_idempotent(ctx):
    s, ids = ctx["s"], ctx["ids"]
    csv_text = (
        "date,amount,counterparty,summary,txn_id\n"
        "2026-08-02,19800.00,学员甲,AI课程收款,TXN-001\n"
        "2026-08-04,-3600.00,阿里云,云服务费,TXN-002\n"
    )
    r1 = import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=csv_text,
                    actor=ctx["actor"])
    s.commit()
    assert r1 == {"imported": 2, "skipped": 0}
    r2 = import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=csv_text,
                    actor=ctx["actor"])
    s.commit()
    assert r2 == {"imported": 0, "skipped": 2}  # 幂等：全部跳过


def test_import_csv_bad_header(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(BankRecError) as ei:
        import_csv(s, ledger_set_id=ids["ledger_set_id"],
                   csv_text="foo,bar\n1,2\n", actor=ctx["actor"])
    assert ei.value.code == "BAD_CSV_HEADER"


def test_reconcile_full_match(ctx):
    """银行流水与账面完全一致 → 全部勾对，无未达账项。"""
    s, ids, ctx_ = ctx["s"], ctx["ids"], ctx
    _book(s, ids, ctx_, "记-0101", __import__("datetime").date(2026, 8, 2),
          "AI课程收款", bank_dr="19800.00", bank_cr="")
    _book(s, ids, ctx_, "记-0102", __import__("datetime").date(2026, 8, 4),
          "云服务费", bank_dr="", bank_cr="3600.00")
    import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=(
        "date,amount,counterparty,summary,txn_id\n"
        "2026-08-02,19800.00,学员甲,AI课程收款,TXN-A\n"
        "2026-08-04,-3600.00,阿里云,云服务费,TXN-B\n"
    ), actor=ctx_["actor"])
    s.commit()
    rep = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"])
    s.commit()
    assert rep["summary"] == {"matched_count": 2, "bank_only_count": 0,
                              "book_only_count": 0}
    assert {m["txn_id"] for m in rep["matched"]} == {"TXN-A", "TXN-B"}


def test_reconcile_outstanding_items(ctx):
    """未达账项双向：银行有账上无 + 账上有银行无（在途）。"""
    s, ids, ctx_ = ctx["s"], ctx["ids"], ctx
    # 账上记了 5,000 收款，但银行流水中没有（在途）
    _book(s, ids, ctx_, "记-0201", __import__("datetime").date(2026, 8, 10),
          "客户回款（在途）", bank_dr="5000.00", bank_cr="")
    # 银行扣了 200 手续费，账上没记（银行已付企业未记账）
    import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=(
        "date,amount,counterparty,summary,txn_id\n"
        "2026-08-12,-200.00,银行,手续费,TXN-FEE\n"
    ), actor=ctx_["actor"])
    s.commit()
    rep = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"])
    s.commit()
    assert rep["summary"]["bank_only_count"] == 1
    assert rep["summary"]["book_only_count"] == 1
    assert rep["bank_only"][0]["txn_id"] == "TXN-FEE"
    assert rep["bank_only"][0]["amount"] == "-200.00"
    assert rep["book_only"][0]["voucher_no"] == "记-0201"


def test_reconcile_persists_matched_events(ctx):
    """勾对结果落 bank.txn.matched 事件，且已勾对流水退出下次候选。"""
    s, ids, ctx_ = ctx["s"], ctx["ids"], ctx
    _book(s, ids, ctx_, "记-0301", __import__("datetime").date(2026, 8, 2),
          "收款", bank_dr="1000.00", bank_cr="")
    import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=(
        "date,amount,summary,txn_id\n2026-08-02,1000.00,收款,TXN-M\n"
    ), actor=ctx_["actor"])
    s.commit()
    rep1 = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"])
    s.commit()
    assert rep1["summary"]["matched_count"] == 1
    # 第二次勾对：该流水已 matched，不再出现
    rep2 = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"])
    s.commit()
    assert rep2["summary"]["matched_count"] == 0
    assert rep2["summary"]["book_only_count"] == 1  # 账侧条目仍在（流水已消耗）
    events = s.scalars(select(Event).where(
        Event.event_type == "bank.txn.matched")).all()
    assert len(events) == 1
    assert events[0].payload["voucher_no"] == "记-0301"


def test_reconcile_dry_run(ctx):
    """persist=False 试算不落事件。"""
    s, ids, ctx_ = ctx["s"], ctx["ids"], ctx
    _book(s, ids, ctx_, "记-0401", __import__("datetime").date(2026, 8, 2),
          "收款", bank_dr="800.00", bank_cr="")
    import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=(
        "date,amount,summary,txn_id\n2026-08-02,800.00,收款,TXN-D\n"
    ), actor=ctx_["actor"])
    s.commit()
    rep = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"],
                    persist=False)
    s.commit()
    assert rep["summary"]["matched_count"] == 1
    assert s.scalars(select(Event).where(
        Event.event_type == "bank.txn.matched")).all() == []


def test_reconcile_date_window_respected(ctx):
    """日期差超过窗口 → 不勾对，进未达账项。"""
    s, ids, ctx_ = ctx["s"], ctx["ids"], ctx
    _book(s, ids, ctx_, "记-0501", __import__("datetime").date(2026, 8, 1),
          "收款", bank_dr="900.00", bank_cr="")
    import_csv(s, ledger_set_id=ids["ledger_set_id"], csv_text=(
        "date,amount,summary,txn_id\n2026-08-30,900.00,收款,TXN-LATE\n"
    ), actor=ctx_["actor"])
    s.commit()
    rep = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"],
                    persist=False)
    s.commit()
    assert rep["summary"]["matched_count"] == 0
    assert rep["summary"]["bank_only_count"] == 1
    assert rep["summary"]["book_only_count"] == 1


def test_reconcile_skips_opening_and_closing_vouchers(ctx):
    """期初/结转凭证是系统规则凭证，不进在途未达账项。"""
    s, ids, ctx_ = ctx["s"], ctx["ids"], ctx
    from kernel.carryforward import open_next_period
    from kernel.closing import close_period

    _book(s, ids, ctx_, "记-0601", __import__("datetime").date(2026, 8, 5),
          "收款", bank_dr="500.00", bank_cr="")
    close_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                 actor=ctx_["actor"])
    s.commit()
    open_next_period(s, ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                     actor=ctx_["actor"])
    s.commit()
    rep = reconcile(s, ledger_set_id=ids["ledger_set_id"], actor=ctx_["actor"],
                    persist=False)
    s.commit()
    nos = {b["voucher_no"] for b in rep["book_only"]}
    assert not any(n.startswith(("期初-", "结转-")) for n in nos)
