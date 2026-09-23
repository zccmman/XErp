"""v2.0 多主体合并报表：code 级聚合 + 抵消(HITL) + 少数股权，全只读。

夹具直接落库（Voucher POSTED + VoucherLine + Balance），绕开审批状态机，
但真实驱动 statements 的两条取数链路：BS 读 Balance 投影、IS 读 POSTED 凭证。

场景经济含义（2026-09，小企业准则，OPEN 期间）：
- A（母公司，全资）：银行+800、收入+1000、费用+200 → 净利 800
- B（子公司，全资）：银行+400、收入+500、费用+100 → 净利 400
- C（子公司，持股 80%）：银行+250、收入+300、费用+50 → 净利 250
合并（全额 100%）：资产 1450 = 负债 0 + 权益 1450（净利注入）；
少数股权 = 0.2×250 = 50；归母净利 = 1450 − 50 = 1400。
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
from kernel.reporting.statements import (
    amounts_by_code,
    balance_sheet,
    ending_balance,
    income_statement,
)

COA = {
    "1001": ("库存现金", "debit", "asset"),
    "1002": ("银行存款", "debit", "asset"),
    "1122": ("应收账款", "debit", "asset"),
    "2202": ("应付账款", "credit", "liability"),
    "4001": ("实收资本", "credit", "equity"),
    "6001": ("主营业务收入", "credit", "pnl"),
    "6602": ("管理费用", "debit", "pnl"),
}

YEAR, MONTH = 2026, 9


def _make_entity(s: Session, name: str, lines: list[dict], ccy: str = "CNY") -> str:
    """造一个账套 + 科目 + OPEN 期间 + 一张 POSTED 凭证 + 对应 Balance 投影。

    lines: [{"code", "dr", "cr"}]；同 code 多行自动聚合为一条 Balances 投影行。
    """
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
    url = f"sqlite:///{d}/consol.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    ids = {}
    with Session(engine) as s:
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
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def session(env):
    engine = create_engine(env["url"])
    with Session(engine) as s:
        yield s


def _all_ids(env) -> list[str]:
    return [env["ids"]["A"], env["ids"]["B"], env["ids"]["C"]]


# ---------------------------------------------------------- 1. code 级聚合 + 少数股权


def test_consolidated_full_with_minority(session, env):
    ids = _all_ids(env)
    bs = CONS.consolidated_balance_sheet(
        session, ids, YEAR, MONTH, ownership={ids[2]: "0.8"}
    )
    cons = bs["consolidated"]
    assert cons["assets"]["total"] == Decimal("1450")      # 800+400+250
    assert cons["liabilities"]["total"] == Decimal("0")
    # OPEN 期间：净利（1800-350=1450）注入权益，表内平衡
    assert cons["equity"]["total"] == Decimal("1450")
    assert bs["balanced"] is True
    # 少数股权：0.2 × C 权益(250) = 50，仅披露、不二次加回总额
    assert bs["minority_interest"] == Decimal("50")
    assert bs["equity_parent"] == Decimal("1400")            # 1450 - 50

    inc = CONS.consolidated_income_statement(
        session, ids, YEAR, MONTH, ownership={ids[2]: "0.8"}
    )
    assert inc["revenue"] == Decimal("1800")                # 1000+500+300
    assert inc["expense"] == Decimal("350")                 # 200+100+50
    assert inc["net_profit"] == Decimal("1450")
    assert inc["minority_interest"] == Decimal("50")
    assert inc["net_profit_parent"] == Decimal("1400")


# ---------------------------------------------------------- 2. 抵消项(HITL) 生效


def test_consolidated_eliminations(session, env):
    ids = _all_ids(env)
    # 给 A 造一笔应收、B 造一笔应付（内部往来），再配对抵消
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        _make_entity(s, "内部A", [
            {"code": "1122", "dr": "100", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "100"},
        ])
        _make_entity(s, "内部B", [
            {"code": "1002", "dr": "100", "cr": "0"},
            {"code": "2202", "dr": "0", "cr": "100"},
        ])
        s.commit()
        a_id = s.scalars(select(LedgerSet).where(LedgerSet.name == "内部A")).first().id
        b_id = s.scalars(select(LedgerSet).where(LedgerSet.name == "内部B")).first().id

    bs = CONS.consolidated_balance_sheet(
        session,
        [a_id, b_id],
        YEAR, MONTH,
        eliminations=[{"dr_code": "1122", "cr_code": "2202", "amount": "100"}],
    )
    # 抵消后 1122(资产) 与 2202(负债) 净额为 0，不应出现在合并报表项目里
    dr_sum = sum(
        r["ending"] for grp in bs["consolidated"]["assets"]["items"]
        for r in grp["accounts"] if r["code"] == "1122"
    )
    cr_sum = sum(
        r["ending"] for grp in bs["consolidated"]["liabilities"]["items"]
        for r in grp["accounts"] if r["code"] == "2202"
    )
    assert dr_sum == Decimal("0")
    assert cr_sum == Decimal("0")


# ---------------------------------------------------------- 3. 只读不变量


def test_consolidate_is_readonly(session, env):
    ids = _all_ids(env)
    before_v = session.scalars(select(Voucher)).all()
    before_b = session.scalars(select(Balance)).all()
    n_v, n_b = len(before_v), len(before_b)

    # 调用两次，确认既不写凭证/过账/结账，也不改余额投影
    CONS.consolidate(session, ids, YEAR, MONTH)
    CONS.consolidate(session, ids, YEAR, MONTH, ownership={ids[2]: "0.8"})

    after_v = session.scalars(select(Voucher)).all()
    after_b = session.scalars(select(Balance)).all()
    assert len(after_v) == n_v
    assert len(after_b) == n_b


# ---------------------------------------------------------- 4. 币种一致性校验


def test_consolidated_currency_mismatch_raises(session, env):
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        usd = _make_entity(s, "美元公司", [
            {"code": "1002", "dr": "100", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "100"},
        ], ccy="USD")
        cny = _make_entity(s, "人民币公司", [
            {"code": "1002", "dr": "100", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "100"},
        ], ccy="CNY")
        s.commit()

    # 多币种且无 fx_rates → 应抛 ConsolidationError（不臆测汇率）
    try:
        CONS.consolidated_balance_sheet(session, [usd, cny], YEAR, MONTH)
        assert False, "应因多币种缺少 fx_rates 而报错"
    except CONS.ConsolidationError:
        pass


# ---------------------------------------------------------- 5. 单主体合并 == 单主体报表


def test_single_entity_consolidation_equals_report(session, env):
    a_id = env["ids"]["A"]
    bs_cons = CONS.consolidated_balance_sheet(session, [a_id], YEAR, MONTH)
    bs_single = balance_sheet(session, a_id, YEAR, MONTH)
    # 合并口径与单主体报表资产/负债/权益总额一致（全额 100% 且全资）
    assert bs_cons["consolidated"]["assets"]["total"] == bs_single["assets"]["total"]
    assert bs_cons["consolidated"]["liabilities"]["total"] == bs_single["liabilities"]["total"]
    assert bs_cons["consolidated"]["equity"]["total"] == bs_single["equity"]["total"]
    assert bs_cons["balanced"] == bs_single["balanced"]


# ---------------------------------------------------------- 6. 公共取数入口 amounts_by_code


def test_amounts_by_code_exposed(session, env):
    a_id = env["ids"]["A"]
    # 1002 两行：借 1000 + 贷 200 → (借合计 1000, 贷合计 200)；6001 贷 1000；6602 借 200
    amts = amounts_by_code(session, a_id, YEAR, MONTH)
    assert amts["1002"] == (Decimal("1000"), Decimal("200"))
    assert amts["6001"] == (Decimal("0"), Decimal("1000"))
    assert amts["6602"] == (Decimal("200"), Decimal("0"))


# ---------------------------------------------------------- 7. 阶段0 P0-1 血缘下钻


def test_lineage_by_code(session, env):
    ids = _all_ids(env)
    res = CONS.consolidation_lineage(session, ids, YEAR, MONTH, code="1002")
    # 1002：A 借1000贷200 → 期末 800；B 借500贷100 → 400；C 借300贷50 → 250
    assert res["consolidated_ending"] == Decimal("1450")
    assert res["scope"]["codes"] == ["1002"]
    for e in res["entities"]:
        d, c = amounts_by_code(session, e["ledger_set_id"], YEAR, MONTH)["1002"]
        assert e["ending"] == ending_balance("1002", d, c)
        # 源凭证：每个主体一张记-0001，1002 两行都命中
        assert len(e["vouchers"]) == 2
        assert all(v["account_code"] == "1002" for v in e["vouchers"])
        # 凭证借贷合计 == Balance 投影发生额（可重建，ADR-002）
        vd = sum(v["debit"] for v in e["vouchers"])
        vc = sum(v["credit"] for v in e["vouchers"])
        assert vd == d and vc == c


def test_lineage_by_group(session, env):
    ids = _all_ids(env)
    # 1002 归入 small_business 的「流动资产」大类
    res = CONS.consolidation_lineage(session, ids, YEAR, MONTH, group="流动资产")
    assert res["scope"]["codes"] == ["1002"]
    assert res["consolidated_ending"] == Decimal("1450")


def test_lineage_requires_scope(session, env):
    ids = _all_ids(env)
    try:
        CONS.consolidation_lineage(session, ids, YEAR, MONTH)
        assert False, "未指定 code/group 应抛 ConsolidationError"
    except CONS.ConsolidationError:
        pass


def test_lineage_is_readonly(session, env):
    ids = _all_ids(env)
    before = len(session.scalars(select(Voucher)).all())
    CONS.consolidation_lineage(session, ids, YEAR, MONTH, code="1002")
    CONS.consolidation_lineage(session, ids, YEAR, MONTH, group="流动资产")
    assert len(session.scalars(select(Voucher)).all()) == before


# ---------------------------------------------------------- 8. 阶段0 P0-2 posting level


def test_posting_levels_without_elim(session, env):
    ids = _all_ids(env)
    res = CONS.consolidated_posting_levels(session, ids, YEAR, MONTH)
    row = next(r for r in res["levels"] if r["code"] == "1002")
    # 无抵消：PL00 == 合并余额，PL20 == 0
    assert row["pl00_entity_reported"] == Decimal("1450")
    assert row["pl20_elimination"] == Decimal("0")
    assert row["consolidated"] == Decimal("1450")
    assert row["report_line"] == "流动资产"


def test_posting_levels_with_elim(session, env):
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        _make_entity(s, "内部A2", [
            {"code": "1122", "dr": "100", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "100"},
        ])
        _make_entity(s, "内部B2", [
            {"code": "1002", "dr": "100", "cr": "0"},
            {"code": "2202", "dr": "0", "cr": "100"},
        ])
        s.commit()
        a_id = s.scalars(select(LedgerSet).where(LedgerSet.name == "内部A2")).first().id
        b_id = s.scalars(select(LedgerSet).where(LedgerSet.name == "内部B2")).first().id

    res = CONS.consolidated_posting_levels(
        session, [a_id, b_id], YEAR, MONTH,
        eliminations=[{"dr_code": "1122", "cr_code": "2202", "amount": "100"}],
    )
    r1122 = next(r for r in res["levels"] if r["code"] == "1122")
    r2202 = next(r for r in res["levels"] if r["code"] == "2202")
    # 抵消前：1122(资产)=100，2202(负债)=100；抵消 100 后净额为 0
    assert r1122["pl00_entity_reported"] == Decimal("100")
    assert r1122["pl20_elimination"] == Decimal("100")    # 资产方抵消使余额下降（PL20 为正=被抵消额）
    assert r1122["consolidated"] == Decimal("0")
    assert r2202["pl00_entity_reported"] == Decimal("100")
    assert r2202["pl20_elimination"] == Decimal("100")     # 负债方抵消使余额回升
    assert r2202["consolidated"] == Decimal("0")


def test_posting_levels_is_readonly(session, env):
    ids = _all_ids(env)
    before = len(session.scalars(select(Voucher)).all())
    CONS.consolidated_posting_levels(session, ids, YEAR, MONTH)
    assert len(session.scalars(select(Voucher)).all()) == before
