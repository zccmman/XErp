"""v2.1 / B3 小规模纳税人增值税及附加税费季报准备：只读生成申报草稿。

验证（守 ADR-002：复用 amounts_by_code / ending_balance / scan_risks 单一口径，
不复制配平/对账逻辑）：
- 免税场景：季度销售额 ≤ 30 万 → 免征增值税及附加（vat_due=0，summary ✅）。
- 应税场景：季度销售额 > 30 万 → 按 1% 征收率计 vat_due，附加税费同步计算。
- 季度聚合：跨 3 个月（7/8/9）的 6001+6051 本期净额正确求和。
- 附加税费：城建税 7% / 教育费附加 3% / 地方教育附加 2%，以实际增值税为计税依据；
  增值税免征时附加亦为 0。
- 申报前置检查：复用 B1 风险扫描，alert 级（表不平/负现金）阻断申报。
- 无可用期间返回 NO_PERIOD（不臆测、不报错），draft 全 0、filing_ready=False。
- yr/mo=0 自动取最新 OPEN 期间并计算其所属季度。
- 工具**绝对只读**：调用前后 Voucher / Balance 行数不变，session 无脏写。
- tax_vat_prep 已接入 standard 档位（与 profiles 契约一致）。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import mkdtemp

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

# 让 xerp_mcp 可被导入（与 test_tool_profiles 同源路径注入）
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
from kernel.reporting.tax_vat_small import (
    EXEMPT_QUARTER_THRESHOLD,
    prep_vat_small,
)


# ---------------------------------------------------------------- 基础设施

STANDARD = "small_business"
QUARTER = (2026, 7, 8, 9)  # Q3：起月 7、止月 9

REQUIRED_ACCOUNTS = [
    # code, name, direction, category
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
    url = f"sqlite:///{d}/tax.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


def _seed_base(engine: "create_engine", months=(8,)) -> dict:
    """建账套 + 指定 OPEN 期间 + 全部所需科目 + 一个 Subject。"""
    with Session(engine) as s:
        ls = LedgerSet(name="报税测试账套", accounting_standard=STANDARD)
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


def _set_balance(s: Session, ls_id: str, per_id: str, acc_id: str, dr: str, cr: str):
    b = Balance(
        ledger_set_id=ls_id, period_id=per_id, account_id=acc_id, dims_key="",
        debit_total=Decimal(dr), credit_total=Decimal(cr),
    )
    s.add(b)


def _seed_sales_month(engine: "create_engine", m: int, sales: str):
    """在某月写入「收入(sales) + 对应货币资金 + 实收资本」使资产负债表平衡。

    注：balance_sheet 的权益侧取自利润表(net_profit, 走凭证明细)，本测试仅用
    Balance 投影塞收入、不制证，故用真实权益科目 3001 贷方补平，避免误触发
    BS_UNBALANCED（与 risk_scan 干净账套同法）。6001 仍作为报税销售额取数来源。
    """
    ids = _seed_base(engine, months=(m,))
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["periods"][m], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], sales, "0")   # 收现（资产）
        _set_balance(s, ls, per, acc["6001"], "0", sales)   # 主营业务收入（报税取数）
        _set_balance(s, ls, per, acc["3001"], "0", sales)   # 实收资本（权益，补平 BS）
        s.commit()
    return ids


def _seed_quarter(engine: "create_engine", sales_by_month: dict[int, str]):
    """建 Q3 三个月期间，并按 dict 写入各月销售额（缺省月为 0）。"""
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


def _seed_blocking_risk(engine: "create_engine", m: int = 9, sales: str = "200000"):
    """目标月负现金（NEGATIVE_CASH）+ 表不平（alert），用于阻断申报测试。"""
    ids = _seed_base(engine, months=(m,))
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["periods"][m], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], "100", "500")  # 银行 -400（负现金）
        _set_balance(s, ls, per, acc["6001"], "0", sales)    # 收入（但资产已失衡）
        s.commit()
    return ids


# ---------------------------------------------------------------- 测试


def test_exempt_when_quarter_le_300k():
    """季销售额 20 万 ≤ 30 万 → 免征增值税及附加。"""
    engine = _new_session()
    ids = _seed_sales_month(engine, 8, "200000")
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    assert r["sales"]["total_sales"] == Decimal("200000")
    assert r["vat"]["exempt"] is True
    assert r["vat"]["vat_due"] == Decimal("0.00")
    assert r["surcharge"]["total"] == Decimal("0.00")
    assert r["precheck"]["filing_ready"] is True
    assert "✅" in r["summary"], r["summary"]


def test_taxable_quarter_levy_1pct():
    """季销售额 50 万 > 30 万 → vat_due = 50万 × 1% = 5000，附加 600。"""
    engine = _new_session()
    ids = _seed_quarter(engine, {7: "166667", 8: "166666", 9: "166667"})
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 2026, 9, STANDARD)
    assert r["sales"]["total_sales"] == Decimal("500000"), r["sales"]
    assert r["vat"]["exempt"] is False
    assert r["vat"]["vat_due"] == Decimal("5000.00"), r["vat"]
    assert r["surcharge"]["city_construction"] == Decimal("350.00")
    assert r["surcharge"]["edu_surcharge"] == Decimal("150.00")
    assert r["surcharge"]["local_edu_surcharge"] == Decimal("100.00")
    assert r["surcharge"]["total"] == Decimal("600.00")
    assert r["precheck"]["filing_ready"] is True
    assert "🧾" in r["summary"], r["summary"]


def test_quarter_aggregation_across_three_months():
    """跨 7/8/9 三个月的销售额正确求和（边界：恰好 30 万 → 免税）。"""
    engine = _new_session()
    ids = _seed_quarter(engine, {7: "100000", 8: "100000", 9: "100000"})
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 2026, 9, STANDARD)
    assert r["sales"]["total_sales"] == EXEMPT_QUARTER_THRESHOLD
    assert r["vat"]["exempt"] is True
    assert r["vat"]["vat_due"] == Decimal("0.00")
    assert r["period"]["quarter"] == 3
    assert r["quarter_months"] == {"start": 7, "end": 9}


def test_above_threshold_is_taxable():
    """边界上方：季 300001 → 应税。"""
    engine = _new_session()
    ids = _seed_quarter(engine, {7: "100000", 8: "100000", 9: "100001"})
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 2026, 9, STANDARD)
    assert r["sales"]["total_sales"] == Decimal("300001")
    assert r["vat"]["exempt"] is False
    assert r["vat"]["vat_due"] == Decimal("3000.01"), r["vat"]


def test_blocking_on_alert_risk():
    """alert 级风险（负现金/表不平）阻断申报：filing_ready=False。"""
    engine = _new_session()
    ids = _seed_blocking_risk(engine, 9, "200000")
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 2026, 9, STANDARD)
    assert r["precheck"]["filing_ready"] is False
    blocking = r["precheck"]["blocking"]
    assert blocking, "应包含 alert 级阻断项"
    assert all(f["severity"] == "alert" for f in blocking)
    assert "⛔" in r["summary"], r["summary"]


def test_no_period_returns_draft_zero():
    """查询不存在的期间 → NO_PERIOD，draft 全 0、filing_ready=False。"""
    engine = _new_session()
    ids = _seed_base(engine, months=(8,))  # 只有 2026-8
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 2025, 1, STANDARD)
    assert r["sales"]["total_sales"] == Decimal("0.00")
    assert r["vat"]["vat_due"] == Decimal("0.00")
    assert r["precheck"]["filing_ready"] is False
    assert r["period"]["quarter"] == 0
    assert "无可用会计期间" in r["summary"], r["summary"]


def test_resolve_zero_to_latest_open():
    """yr/mo=0 自动取最新 OPEN 期间（2026-8，属 Q3）并计算。"""
    engine = _new_session()
    ids = _seed_sales_month(engine, 8, "150000")
    with Session(engine) as s:
        r = prep_vat_small(s, ids["ledger_set_id"], 0, 0, STANDARD)
    assert r["period"]["month"] == 9  # Q3 止月
    assert r["period"]["quarter"] == 3
    assert r["sales"]["total_sales"] == Decimal("150000")
    assert r["precheck"]["filing_ready"] is True


def test_prep_is_readonly_no_writes():
    """申报准备是只读生成：调用前后 Voucher / Balance 行数不变，session 无脏写。"""
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
            prep_vat_small(s, ids["ledger_set_id"], 2026, 9, STANDARD)
        after_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ids["ledger_set_id"])
        ).first()
        after_b = s.scalars(
            select(func.count(Balance.id)).where(Balance.ledger_set_id == ids["ledger_set_id"])
        ).first()
        assert before_v == after_v, "prep_vat_small 不应新增凭证"
        assert before_b == after_b, "prep_vat_small 不应新增余额投影"
        assert len(s.new) == 0 and len(s.dirty) == 0, "prep_vat_small 不应产生待写脏数据"


def test_tax_vat_prep_wired_to_standard_tier():
    """报税准备必须归入 standard 档（与工具分层契约一致）。"""
    from xerp_mcp import profiles

    assert "tax_vat_prep" in profiles.STANDARD_EXTRA, "tax_vat_prep 应归入 STANDARD_EXTRA"
    assert "tax_vat_prep" in profiles.enabled_for("standard")
    assert "tax_vat_prep" not in profiles.enabled_for("minimal"), "报税准备非极简档能力"
