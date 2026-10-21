"""v2.1 / P-09 税务健康分 + 申报日历：只读生成（守 ADR-002，零写动作）。

验证：
- 健康分满分：季销售额 ≤ 30 万、账套干净 → score=100、grade=优、filing_ready=True。
- 阻断扣分：alert 级风险（负现金/表不平）→ filing_ready=False、score 显著下降。
- 逾期未结转：申报窗口已过且季度未结转 → overdue_unclear=True、扣 10 分。
- 申报日历：当年 4 个季度窗口 + 状态（no_data/upcoming/overdue/closed），
  交叉账套实际期间；小规模的 Q3 有数据则 data_ready 透传。
- 工具**绝对只读**：调用前后 Voucher / Balance 行数不变，session 无脏写。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import mkdtemp

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.db.base import Base
from kernel.db.models import (
    Account,
    Balance,
    LedgerSet,
    Period,
    Subject,
    Voucher,
    VoucherLine,
)
from kernel.reporting.tax_health import filing_calendar, tax_health_score

STANDARD = "small_business"

REQUIRED_ACCOUNTS = [
    ("1001", "库存现金", "debit", "asset"),
    ("1002", "银行存款", "debit", "asset"),
    ("1122", "应收账款", "debit", "asset"),
    ("1123", "预付账款", "debit", "asset"),
    ("1221", "其他应收", "debit", "asset"),
    ("2202", "应付账款", "credit", "liability"),
    ("2203", "预收账款", "credit", "liability"),
    ("2241", "其他应付", "credit", "liability"),
    ("3001", "实收资本", "credit", "equity"),
    ("6001", "主营业务收入", "credit", "pnl"),
    ("6051", "其他业务收入", "credit", "pnl"),
    ("6602", "管理费用", "debit", "pnl"),
]


def _new_session():
    d = mkdtemp()
    url = f"sqlite:///{d}/tax_health.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


def _seed_base(engine, months=(8,)):
    with Session(engine) as s:
        ls = LedgerSet(name="税务健康测试账套", accounting_standard=STANDARD)
        s.add(ls)
        s.flush()
        periods = {}
        for m in months:
            per = Period(ledger_set_id=ls.id, year=2026, month=m, status="OPEN")
            s.add(per)
            s.flush()
            periods[m] = per.id
        subj = Subject(type="user", display_name="测试", autonomy_level=3)
        s.add(subj)
        s.flush()
        accounts = {}
        for code, name, direction, category in REQUIRED_ACCOUNTS:
            a = Account(
                ledger_set_id=ls.id, code=code, name=name,
                direction=direction, category=category,
            )
            s.add(a)
            s.flush()
            accounts[code] = a
        s.commit()
        return {
            "ledger_set_id": ls.id,
            "periods": periods,
            "subject_id": subj.id,
            "accounts": {c: a.id for c, a in accounts.items()},
        }


def _set_balance(s, ls_id, per_id, acc_id, dr, cr):
    s.add(Balance(
        ledger_set_id=ls_id, period_id=per_id, account_id=acc_id, dims_key="",
        debit_total=Decimal(dr), credit_total=Decimal(cr),
    ))


def _seed_sales_month(engine, m, sales):
    ids = _seed_base(engine, months=(m,))
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["periods"][m], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], sales, "0")
        _set_balance(s, ls, per, acc["6001"], "0", sales)
        _set_balance(s, ls, per, acc["3001"], "0", sales)
        s.commit()
    return ids


def _seed_quarter(engine, sales_by_month):
    ids = _seed_base(engine, months=(7, 8, 9))
    with Session(engine) as s:
        ls, acc = ids["ledger_set_id"], ids["accounts"]
        for m, sales in sales_by_month.items():
            per = ids["periods"][m]
            _set_balance(s, ls, per, acc["1002"], sales, "0")
            _set_balance(s, ls, per, acc["6001"], "0", sales)
            _set_balance(s, ls, per, acc["3001"], "0", sales)
        s.commit()
    return ids


def _seed_blocking_risk(engine, m=9, sales="200000"):
    ids = _seed_base(engine, months=(m,))
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["periods"][m], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], "100", "500")  # 银行 -400（负现金）
        _set_balance(s, ls, per, acc["6001"], "0", sales)
        s.commit()
    return ids


def _seed_closed_quarter(engine, sales_by_month):
    """与 _seed_quarter 同，但把季度止月(9)期间标记 CLOSED。"""
    ids = _seed_quarter(engine, sales_by_month)
    with Session(engine) as s:
        per = s.scalars(
            select(Period).where(
                Period.ledger_set_id == ids["ledger_set_id"],
                Period.year == 2026, Period.month == 9,
            )
        ).first()
        per.status = "CLOSED"
        s.commit()
    return ids


# ---------------------------------------------------------------- 测试

def test_health_excellent_when_exempt_and_clean():
    """季销售额 20 万、账套干净 → score=100、优、filing_ready=True。"""
    engine = _new_session()
    ids = _seed_sales_month(engine, 8, "200000")
    with Session(engine) as s:
        r = tax_health_score(s, ids["ledger_set_id"], 2026, 8, STANDARD, today=date(2026, 9, 1))
    assert r["score"] == 100, r
    assert r["grade"] == "优", r
    assert r["filing_ready"] is True
    assert r["overdue_unclear"] is False
    assert r["factors"] == [], r["factors"]


def test_health_penalized_by_blocking_risk():
    """alert 级风险（负现金/表不平）→ filing_ready=False、score 显著下降。"""
    engine = _new_session()
    ids = _seed_blocking_risk(engine, 9, "200000")
    with Session(engine) as s:
        r = tax_health_score(s, ids["ledger_set_id"], 2026, 9, STANDARD, today=date(2026, 9, 1))
    assert r["filing_ready"] is False
    assert r["score"] < 100, r
    # 2 项 alert（负现金+表不平，封顶扣分）+ 阻断 -20，至少 -40
    assert r["score"] <= 60, r
    assert r["grade"] in ("差", "中"), r["grade"]


def test_health_overdue_unclear_when_window_passed_unclosed():
    """申报窗口已过且季度未结转 → overdue_unclear=True、扣 10。"""
    engine = _new_session()
    ids = _seed_sales_month(engine, 9, "200000")  # Q3 止月 9，截止 2026-10-15，仍 OPEN
    with Session(engine) as s:
        r = tax_health_score(s, ids["ledger_set_id"], 2026, 9, STANDARD, today=date(2026, 11, 1))
    assert r["overdue_unclear"] is True
    assert any(f["delta"] == -10 for f in r["factors"]), r["factors"]


def test_health_no_overdue_when_quarter_closed():
    """季度已结转（CLOSED）→ 即便窗口已过也不计逾期扣分（overdue_unclear=False）。"""
    engine = _new_session()
    ids = _seed_closed_quarter(engine, {7: "100000", 8: "100000", 9: "100000"})
    with Session(engine) as s:
        r = tax_health_score(s, ids["ledger_set_id"], 2026, 9, STANDARD, today=date(2026, 12, 1))
    assert r["overdue_unclear"] is False, r["factors"]
    # 注意：月份 7/8 仍 OPEN 会产生 UNCLOSED_HISTORY warn（非逾期扣分），与逾期逻辑无关
    assert r["score"] >= 75, r  # 仅 warn，无 alert、无阻断、无逾期


def test_calendar_reflects_quarter_data_and_states():
    """当年日历：Q3 有数据（data_ready 透传），其余 no_data；状态与 today 一致。

    注：申报准备以季度止月(9)解析，故需在账套建 2026-9 期间才有 Q3 数据。
    """
    engine = _new_session()
    ids = _seed_sales_month(engine, 9, "150000")  # 2026-9，属 Q3 止月
    with Session(engine) as s:
        cal = filing_calendar(s, ids["ledger_set_id"], 2026, STANDARD, today=date(2026, 9, 1))
    assert cal["year"] == 2026
    assert len(cal["quarters"]) == 4
    q3 = next(q for q in cal["quarters"] if q["quarter"] == 3)
    assert q3["state"] in ("upcoming", "due_soon"), q3
    assert q3["data_ready"] is True
    assert q3["total_sales"] == Decimal("150000")
    # 其余季度无数据
    others = [q for q in cal["quarters"] if q["quarter"] != 3]
    assert all(q["state"] == "no_data" for q in others), others


def test_calendar_overdue_state_for_unclosed_past_quarter():
    """Q3 窗口已过且未结转 → 该季度 state=overdue。"""
    engine = _new_session()
    ids = _seed_sales_month(engine, 9, "200000")  # Q3 止月 9，截止 2026-10-15，OPEN
    with Session(engine) as s:
        cal = filing_calendar(s, ids["ledger_set_id"], 2026, STANDARD, today=date(2026, 11, 1))
    q3 = next(q for q in cal["quarters"] if q["quarter"] == 3)
    assert q3["state"] == "overdue", q3
    assert cal["due_count"] >= 1


def test_engine_is_readonly_no_writes():
    """税务健康分 + 申报日历绝对只读：调用前后 Voucher / Balance 行数不变。"""
    engine = _new_session()
    ids = _seed_quarter(engine, {7: "100000", 8: "100000", 9: "100000"})
    with Session(engine) as s:
        before_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ids["ledger_set_id"])
        ).first()
        before_b = s.scalars(
            select(func.count(Balance.id)).where(Balance.ledger_set_id == ids["ledger_set_id"])
        ).first()
        for _ in range(3):
            tax_health_score(s, ids["ledger_set_id"], 2026, 9, STANDARD, today=date(2026, 9, 1))
            filing_calendar(s, ids["ledger_set_id"], 2026, STANDARD, today=date(2026, 9, 1))
        after_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ids["ledger_set_id"])
        ).first()
        after_b = s.scalars(
            select(func.count(Balance.id)).where(Balance.ledger_set_id == ids["ledger_set_id"])
        ).first()
        assert before_v == after_v, "tax_health 不应新增凭证"
        assert before_b == after_b, "tax_health 不应新增余额投影"
        assert len(s.new) == 0 and len(s.dirty) == 0, "tax_health 不应产生待写脏数据"
