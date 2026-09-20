"""v2.1 / B1 AI 风险预警：只读扫描小微企业常见财务风险。

验证（守 ADR-002：复用内核既有只读取数，不复制配平/对账逻辑）：
- scan_risks 能命中全部 6 类风险：BS_UNBALANCED / NEGATIVE_CASH /
  AR_CREDIT_BALANCE / AP_DEBIT_BALANCE / LARGE_AMOUNT / UNCLOSED_HISTORY。
- 干净的账套返回零发现（findings 为空，summary 含 ✅）。
- 无可用会计期间返回 NO_PERIOD（不臆测、不报错）。
- 扫描**绝对只读**：调用前后 Voucher / Balance 行数不变，session 无待写脏数据。
- 风险扫描已接入 standard 档位（与 profiles 契约一致）。

设计原则「宁可漏报不误报」：缺数据就跳过（如样本不足 5 笔不报大额），
每条 finding 都应可被账套数据证伪。
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
from kernel.reporting.risk_scan import scan_risks


# ---------------------------------------------------------------- 基础设施

STANDARD = "small_business"

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
    ("6602", "管理费用", "debit", "pnl"),
]


def _new_session():
    d = mkdtemp()
    url = f"sqlite:///{d}/risk.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


def _seed_base(engine: "create_engine") -> dict:
    """建账套 + OPEN 期间(2026-8) + 全部所需科目 + 一个 Subject。

    返回关键 id，供后续按场景写入余额/凭证。
    """
    with Session(engine) as s:
        ls = LedgerSet(name="风险测试账套", accounting_standard=STANDARD)
        s.add(ls)
        s.flush()
        per = Period(ledger_set_id=ls.id, year=2026, month=8, status="OPEN")
        s.add(per)
        s.flush()
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
        # 在会话关闭前取走字符串 id，避免后续访问触发 DetachedInstanceError
        return {
            "ledger_set_id": ls.id,
            "period_id": per.id,
            "subject_id": subj.id,
            "accounts": {c: a.id for c, a in accounts.items()},
        }


def _set_balance(s: Session, ls_id: str, per_id: str, acc_id: str, dr: str, cr: str):
    b = Balance(
        ledger_set_id=ls_id, period_id=per_id, account_id=acc_id, dims_key="",
        debit_total=Decimal(dr), credit_total=Decimal(cr),
    )
    s.add(b)


def _add_voucher(s: Session, ls_id: str, per_id: str, subj_id: str, no: str,
                 lines: list[tuple[str, str, str]], status: str = "POSTED"):
    """lines: [(acc_code, debit, credit), ...]，按 REQuRED_ACCOUNTS 的 code 传。

    注意：本测试 helper 不强制借贷平衡（风险扫描只读，不校验凭证平衡），
    但为使 balance_sheet 口径干净，调用方应自行保持借贷相等。
    """
    v = Voucher(
        ledger_set_id=ls_id, period_id=per_id, voucher_no=no,
        voucher_date=date(2026, 8, 15), status=status, summary="t",
        created_by=subj_id,
    )
    s.add(v)
    s.flush()
    acc_ids = {a.code: a.id for a in s.scalars(select(Account)).all()}
    for i, (code, dr, cr) in enumerate(lines, 1):
        s.add(VoucherLine(
            voucher_id=v.id, line_no=i, account_id=acc_ids[code],
            debit=Decimal(dr), credit=Decimal(cr),
        ))


def _codes(findings: list[dict]) -> set[str]:
    return {f["code"] for f in findings}


# ---------------------------------------------------------------- 场景构造


def _seed_clean(engine: "create_engine") -> dict:
    """干净账套：资产负债表平衡，无任何方向异常、无凭证、无历史未结账。"""
    ids = _seed_base(engine)
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["period_id"], ids["accounts"]
        # 资产 15000 = 负债 3000 + 权益 12000（权益用 3001，映射入所有者权益）
        _set_balance(s, ls, per, acc["1002"], "10000", "0")   # 银行 +10000
        _set_balance(s, ls, per, acc["1122"], "5000", "0")    # 应收 +5000
        _set_balance(s, ls, per, acc["2202"], "0", "3000")    # 应付 +3000
        _set_balance(s, ls, per, acc["3001"], "0", "12000")   # 实收资本 +12000
        s.commit()
    return ids


def _seed_all_risks(engine: "create_engine") -> dict:
    """一次性触发全部 6 类风险（用于「全命中」与「只读不改账」测试）。"""
    ids = _seed_base(engine)
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["period_id"], ids["accounts"]
        subj = ids["subject_id"]
        # R2 负现金 + R3 应收贷方 + R4 应付借方 + R1 表内不平
        _set_balance(s, ls, per, acc["1002"], "100", "500")    # 银行 -400（负）
        _set_balance(s, ls, per, acc["1122"], "100", "500")    # 应收 -400（贷方）
        _set_balance(s, ls, per, acc["2202"], "500", "100")    # 应付 -400（借方）
        _set_balance(s, ls, per, acc["3001"], "0", "12000")    # 权益 +12000
        s.commit()
    # R6 历史期间未结账：早于目标 OPEN 且无结转凭证
    with Session(engine) as s:
        ls = ids["ledger_set_id"]
        prev = Period(ledger_set_id=ls, year=2026, month=7, status="OPEN")
        s.add(prev)
        s.commit()
    # R5 异常大额：≥5 笔 POSTED 行，含 1 笔远超中位数（阈值 floor=100000）
    with Session(engine) as s:
        ls, per, subj = ids["ledger_set_id"], ids["period_id"], ids["subject_id"]
        _add_voucher(s, ls, per, subj, "记-0001",
                     [("1002", "1000", "0"), ("6001", "0", "1000")])
        _add_voucher(s, ls, per, subj, "记-0002",
                     [("6602", "1000", "0"), ("1002", "0", "1000")])
        _add_voucher(s, ls, per, subj, "记-0003",
                     [("6602", "200000", "0"), ("1002", "0", "200000")])
        s.commit()
    return ids


def _seed_cash_negative(engine: "create_engine") -> dict:
    """仅负现金异常，其余平衡（资产 4600 = 负债 3000 + 权益 1600）。"""
    ids = _seed_base(engine)
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["period_id"], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], "100", "500")   # 银行 -400（负）
        _set_balance(s, ls, per, acc["1122"], "5000", "0")
        _set_balance(s, ls, per, acc["2202"], "0", "3000")
        _set_balance(s, ls, per, acc["3001"], "0", "1600")
        s.commit()
    return ids


def _seed_ar_credit(engine: "create_engine") -> dict:
    """仅应收贷方异常，其余平衡（资产 9600 = 负债 3000 + 权益 6600）。"""
    ids = _seed_base(engine)
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["period_id"], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], "10000", "0")
        _set_balance(s, ls, per, acc["1122"], "100", "500")   # 应收 -400（贷方）
        _set_balance(s, ls, per, acc["2202"], "0", "3000")
        _set_balance(s, ls, per, acc["3001"], "0", "6600")
        s.commit()
    return ids


def _seed_ap_debit(engine: "create_engine") -> dict:
    """仅应付借方异常，其余平衡（资产 15000 = 负债 -400 + 权益 15400）。"""
    ids = _seed_base(engine)
    with Session(engine) as s:
        ls, per, acc = ids["ledger_set_id"], ids["period_id"], ids["accounts"]
        _set_balance(s, ls, per, acc["1002"], "10000", "0")
        _set_balance(s, ls, per, acc["1122"], "5000", "0")
        _set_balance(s, ls, per, acc["2202"], "500", "100")   # 应付 -400（借方）
        _set_balance(s, ls, per, acc["3001"], "0", "15400")
        s.commit()
    return ids


def _seed_large_amount(engine: "create_engine") -> dict:
    ids = _seed_clean(engine)  # 先用干净平衡表，再补大额凭证
    with Session(engine) as s:
        ls, per, subj = ids["ledger_set_id"], ids["period_id"], ids["subject_id"]
        _add_voucher(s, ls, per, subj, "记-0001",
                     [("1002", "1000", "0"), ("6001", "0", "1000")])
        _add_voucher(s, ls, per, subj, "记-0002",
                     [("6602", "1000", "0"), ("1002", "0", "1000")])
        _add_voucher(s, ls, per, subj, "记-0003",
                     [("6602", "200000", "0"), ("1002", "0", "200000")])
        s.commit()
    return ids


def _seed_unclosed_history(engine: "create_engine") -> dict:
    ids = _seed_clean(engine)
    with Session(engine) as s:
        ls = ids["ledger_set_id"]
        prev = Period(ledger_set_id=ls, year=2026, month=7, status="OPEN")
        s.add(prev)
        s.commit()
    return ids


def _seed_no_period(engine: "create_engine") -> dict:
    """建账套但只有 2026-8，扫描一个不存在的期间 → NO_PERIOD。"""
    return _seed_base(engine)  # 仅 base，无额外期间


# ---------------------------------------------------------------- 测试


def test_clean_ledger_zero_findings():
    engine = _new_session()
    ids = _seed_clean(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    assert r["findings"] == [], f"干净账套不应有风险发现：{r['findings']}"
    assert r["severity_counts"] == {"alert": 0, "warn": 0, "info": 0}
    assert "✅" in r["summary"], r["summary"]
    assert r["period_status"] == "OPEN"


def test_detects_negative_cash():
    engine = _new_session()
    ids = _seed_cash_negative(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    codes = _codes(r["findings"])
    assert "NEGATIVE_CASH" in codes, r["findings"]
    f = next(x for x in r["findings"] if x["code"] == "NEGATIVE_CASH")
    assert f["severity"] == "alert"
    assert f["code"] == "NEGATIVE_CASH"


def test_detects_ar_credit_balance():
    engine = _new_session()
    ids = _seed_ar_credit(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    codes = _codes(r["findings"])
    assert "AR_CREDIT_BALANCE" in codes, r["findings"]
    f = next(x for x in r["findings"] if x["code"] == "AR_CREDIT_BALANCE")
    assert f["severity"] == "warn"
    assert "1122" in f["title"]


def test_detects_ap_debit_balance():
    engine = _new_session()
    ids = _seed_ap_debit(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    codes = _codes(r["findings"])
    assert "AP_DEBIT_BALANCE" in codes, r["findings"]
    f = next(x for x in r["findings"] if x["code"] == "AP_DEBIT_BALANCE")
    assert f["severity"] == "warn"
    assert "2202" in f["title"]


def test_detects_large_amount():
    engine = _new_session()
    ids = _seed_large_amount(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    codes = _codes(r["findings"])
    assert "LARGE_AMOUNT" in codes, r["findings"]
    f = next(x for x in r["findings"] if x["code"] == "LARGE_AMOUNT")
    assert f["severity"] == "warn"


def test_detects_unclosed_history():
    engine = _new_session()
    ids = _seed_unclosed_history(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    codes = _codes(r["findings"])
    assert "UNCLOSED_HISTORY" in codes, r["findings"]
    f = next(x for x in r["findings"] if x["code"] == "UNCLOSED_HISTORY")
    assert "2026-07" in f["title"], f["title"]


def test_all_six_risk_types_fire():
    engine = _new_session()
    ids = _seed_all_risks(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
    codes = _codes(r["findings"])
    for expected in (
        "BS_UNBALANCED", "NEGATIVE_CASH", "AR_CREDIT_BALANCE",
        "AP_DEBIT_BALANCE", "LARGE_AMOUNT", "UNCLOSED_HISTORY",
    ):
        assert expected in codes, f"未命中 {expected}；现有 {sorted(codes)}"
    # 严重度计数自洽：alert 至少含 负现金/表不平，warn 至少含其余
    assert r["severity_counts"]["alert"] >= 1
    assert r["severity_counts"]["warn"] >= 4


def test_no_period_returns_no_period():
    engine = _new_session()
    ids = _seed_no_period(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 2025, 1, STANDARD)
    codes = _codes(r["findings"])
    assert "NO_PERIOD" in codes, r["findings"]
    assert r["period_status"] == "none"
    assert r["severity_counts"]["warn"] == 1


def test_resolve_latest_open_when_zero():
    """yr/mo 传 0 时应自动取最新 OPEN 期间（不报错、不臆测）。"""
    engine = _new_session()
    ids = _seed_clean(engine)
    with Session(engine) as s:
        r = scan_risks(s, ids["ledger_set_id"], 0, 0, STANDARD)
    assert r["findings"] == [], "最新 OPEN 期间即 2026-8，干净账套应零发现"
    assert r["period_status"] == "OPEN"


def test_scan_is_readonly_no_writes():
    """风险扫描是只读生成：调用前后 Voucher / Balance 行数不变，session 无脏写。"""
    engine = _new_session()
    ids = _seed_all_risks(engine)
    with Session(engine) as s:
        before_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ids["ledger_set_id"])
        ).first()
        before_b = s.scalars(
            select(func.count(Balance.id)).where(Balance.ledger_set_id == ids["ledger_set_id"])
        ).first()
        # 反复调用，模拟多端多次拉取
        for _ in range(3):
            scan_risks(s, ids["ledger_set_id"], 2026, 8, STANDARD)
        after_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ids["ledger_set_id"])
        ).first()
        after_b = s.scalars(
            select(func.count(Balance.id)).where(Balance.ledger_set_id == ids["ledger_set_id"])
        ).first()
        assert before_v == after_v, "scan_risks 不应新增凭证"
        assert before_b == after_b, "scan_risks 不应新增余额投影"
        assert len(s.new) == 0 and len(s.dirty) == 0, "scan_risks 不应产生待写脏数据"


def test_risk_scan_wired_to_standard_tier():
    """风险扫描必须归入 standard 档（与工具分层契约一致）。"""
    from xerp_mcp import profiles

    assert "risk_scan" in profiles.STANDARD_EXTRA, "risk_scan 应归入 STANDARD_EXTRA"
    assert "risk_scan" in profiles.enabled_for("standard")
    assert "risk_scan" not in profiles.enabled_for("minimal"), "风险扫描非极简档能力"
