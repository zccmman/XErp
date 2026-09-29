"""合并工作底稿（可视化层）单测：逐主体贡献矩阵 + 抵消明细，单一真源。

复用 test_consolidation 同款「直接落库」夹具（绕开审批状态机，真实驱动 Balance
投影与 POSTED 凭证两条取数链路）。重点验证 workbook 与 consolidate 同源：
主体小计 = Σ 各主体分项；合并数 = consolidate 的权威结果；抵消调整 = 主体小计 −
合并数（与 consolidated_posting_levels 的 PL20 勾稽）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import (
    Account,
    Balance,
    LedgerSet,
    Period,
    Subject,
    Voucher,
    VoucherLine,
    utcnow,
)
from kernel.reporting import consolidation as CONS

COA = {
    "1002": ("银行存款", "debit", "asset"),
    "1122": ("应收账款", "debit", "asset"),
    "1511": ("长期股权投资", "debit", "asset"),
    "1911": ("商誉", "debit", "asset"),
    "2202": ("应付账款", "credit", "liability"),
    "4001": ("实收资本", "credit", "equity"),
    "4103": ("本年利润", "credit", "equity"),
    "6001": ("主营业务收入", "credit", "pnl"),
    "6602": ("管理费用", "debit", "pnl"),
}

YEAR, MONTH = 2026, 9


def _make_entity(s: Session, name: str, lines: list[dict], ccy: str = "CNY") -> str:
    ls = LedgerSet(name=name, accounting_standard="small_business",
                   functional_currency=ccy)
    s.add(ls)
    s.flush()
    accs: dict[str, Account] = {}
    for code in {ln["code"] for ln in lines}:
        nm, dr, cat = COA[code]
        acc = Account(ledger_set_id=ls.id, code=code, name=nm,
                      direction=dr, category=cat)
        s.add(acc)
        s.flush()
        accs[code] = acc
    per = Period(ledger_set_id=ls.id, year=YEAR, month=MONTH, status="OPEN")
    s.add(per)
    s.flush()
    subj = Subject(type="user", display_name=f"{name}制单", autonomy_level=3)
    s.add(subj)
    s.flush()
    v = Voucher(ledger_set_id=ls.id, period_id=per.id, voucher_no="记-0001",
                voucher_date=date(YEAR, MONTH, 15), status="POSTED",
                summary="测试凭证", created_by=subj.id, posted_at=utcnow())
    s.add(v)
    s.flush()
    agg: dict[str, list[Decimal]] = {}
    for i, ln in enumerate(lines, 1):
        d = Decimal(str(ln["dr"]))
        c = Decimal(str(ln["cr"]))
        s.add(VoucherLine(voucher_id=v.id, line_no=i,
                          account_id=accs[ln["code"]].id, debit=d, credit=c))
        cur = agg.get(ln["code"], [Decimal("0"), Decimal("0")])
        agg[ln["code"]] = [cur[0] + d, cur[1] + c]
    for code, (d, c) in agg.items():
        s.add(Balance(ledger_set_id=ls.id, period_id=per.id,
                      account_id=accs[code].id, dims_key="",
                      debit_total=d, credit_total=c))
    s.flush()
    return ls.id


@pytest.fixture(scope="module")
def env():
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/wb.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    ids = {}
    with Session(engine) as s:
        # 少数股权场景：A 全资母 + B 全资子 + C 持股 80% 子
        ids["A"] = _make_entity(s, "母公司A", [
            {"code": "1002", "dr": "1000", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "1000"},
            {"code": "6602", "dr": "200", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "200"},
        ])
        ids["B"] = _make_entity(s, "子公司B", [
            {"code": "1002", "dr": "500", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "500"},
            {"code": "6602", "dr": "100", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "100"},
        ])
        ids["C"] = _make_entity(s, "子公司C", [
            {"code": "1002", "dr": "300", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "300"},
            {"code": "6602", "dr": "50", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "50"},
        ])
        # 内部往来抵消场景：P 应收 / S 应付 各 500
        ids["P"] = _make_entity(s, "内部P", [
            {"code": "1002", "dr": "1000", "cr": "0"},
            {"code": "1122", "dr": "500", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "1000"},
            {"code": "6602", "dr": "200", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "200"},
            {"code": "1002", "dr": "0", "cr": "500"},
        ])
        ids["S"] = _make_entity(s, "内部S", [
            {"code": "1002", "dr": "600", "cr": "0"},
            {"code": "2202", "dr": "0", "cr": "500"},
            {"code": "6001", "dr": "0", "cr": "600"},
            {"code": "6602", "dr": "100", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "100"},
        ])
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def session(env):
    engine = create_engine(env["url"])
    with Session(engine) as s:
        yield s


def _all(env, keys):
    return [env["ids"][k] for k in keys]


# ---------------------------------------------------------- 1. 贡献矩阵 + 单源勾稽


def test_workbook_contributions_sum_and_match_consolidate(session, env):
    ids = _all(env, ["A", "B", "C"])
    wb = CONS.consolidation_workbook(
        session, ids, YEAR, MONTH, ownership={env["ids"]["C"]: "0.8"}
    )
    assert wb["balanced"] is True
    c = CONS.consolidate(session, ids, YEAR, MONTH, ownership={env["ids"]["C"]: "0.8"})

    # 合并数必须 == consolidate 的权威结果（单一真源）
    assert Decimal(wb["balances"]["assets"]) == c["balance_sheet"]["consolidated"]["assets"]["total"]
    assert Decimal(wb["balances"]["liabilities"]) == c["balance_sheet"]["consolidated"]["liabilities"]["total"]
    assert Decimal(wb["balances"]["equity"]) == c["balance_sheet"]["consolidated"]["equity"]["total"]

    # 每个 BS 行：Σ 主体分项 == 主体小计；主体小计 − 合并数 == 抵消调整
    for major in ("assets", "liabilities", "equity"):
        sec = wb["balance_sheet"][major]
        sum_consolidated = Decimal("0")
        for row in sec["rows"]:
            contr = [Decimal(x) for x in row["contributions"]]
            assert sum(contr) == Decimal(row["subtotal"])
            assert Decimal(row["subtotal"]) - Decimal(row["consolidated"]) == Decimal(row["elimination"])
            sum_consolidated += Decimal(row["consolidated"])
        assert sum_consolidated == Decimal(sec["total"])

    # IS：净利的 100% 口径 == consolidate；少数股权 / 归母 披露正确
    inc = wb["income_statement"]
    assert Decimal(inc["net_profit"]) == Decimal("1450")
    assert Decimal(inc["minority_interest"]) == Decimal("50")
    assert Decimal(inc["net_profit_parent"]) == Decimal("1400")


def test_workbook_elimination_column_matches_posting_levels(session, env):
    ids = _all(env, ["P", "S"])
    elim = [{"dr_code": "1122", "cr_code": "2202", "amount": "500"}]
    wb = CONS.consolidation_workbook(
        session, ids, YEAR, MONTH, eliminations=elim
    )
    c = CONS.consolidate(session, ids, YEAR, MONTH, eliminations=elim)

    # 合并结果同源
    assert Decimal(wb["balances"]["assets"]) == c["balance_sheet"]["consolidated"]["assets"]["total"]
    assert Decimal(wb["balances"]["liabilities"]) == c["balance_sheet"]["consolidated"]["liabilities"]["total"]

    # 抵消后合并数：内部往来 500 被全额抵消
    # P(银行净 300 + 应收 500 = 800) + S(银行净 500)，应收抵消 → 资产合并 800；应付抵消 → 负债 0
    assert Decimal(wb["balances"]["assets"]) == Decimal("800")
    assert Decimal(wb["balances"]["liabilities"]) == Decimal("0")

    # 工作底稿抵消调整列（Σ over 所有 BS 行） == consolidated_posting_levels 的 Σ PL20
    ws_elim_total = Decimal("0")
    for major in ("assets", "liabilities", "equity"):
        for row in wb["balance_sheet"][major]["rows"]:
            ws_elim_total += Decimal(row["elimination"])
    pl20_total = sum((Decimal(lv["pl20_elimination"]) for lv in wb["posting_levels"]), Decimal("0"))
    assert ws_elim_total == pl20_total == Decimal("1000")  # 应收 500 + 应付 500

    # 已应用消除对如实回显
    assert wb["eliminations"] == [{"dr_code": "1122", "cr_code": "2202", "amount": "500"}]
    # 内部往来抵消在「流动资产 / 流动负债」大类行体现：抵消调整 = 500（应收/应付各抵消 500）
    ca = next(r for r in wb["balance_sheet"]["assets"]["rows"] if r["group"] == "流动资产")
    cl = next(r for r in wb["balance_sheet"]["liabilities"]["rows"] if r["group"] == "流动负债")
    assert Decimal(ca["elimination"]) == Decimal("500")
    assert Decimal(cl["elimination"]) == Decimal("500")


def test_workbook_entity_names_and_currency(session, env):
    ids = _all(env, ["A", "B", "C"])
    wb = CONS.consolidation_workbook(session, ids, YEAR, MONTH)
    assert wb["entity_names"] == ["母公司A", "子公司B", "子公司C"]
    assert wb["currency"] == "CNY"
    assert set(wb["owned"].keys()) == set(ids)
    assert all(v == "1" for v in wb["owned"].values())
    # 无抵消时 eliminations 为空，posting_levels 仅含 pl20=0 的 code 级行
    assert wb["eliminations"] == []
    assert all(lv["pl20_elimination"] == Decimal("0") for lv in wb["posting_levels"])
