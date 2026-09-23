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
    "1511": ("长期股权投资", "debit", "asset"),
    "1911": ("商誉", "debit", "asset"),
    "2202": ("应付账款", "credit", "liability"),
    "4001": ("实收资本", "credit", "equity"),
    "4002": ("资本公积", "credit", "equity"),
    "4101": ("盈余公积", "credit", "equity"),
    "4103": ("本年利润", "credit", "equity"),
    "4104": ("利润分配", "credit", "equity"),
    "6001": ("主营业务收入", "credit", "pnl"),
    "6602": ("管理费用", "debit", "pnl"),
}

YEAR, MONTH = 2026, 9


def _make_entity(s: Session, name: str, lines: list[dict], ccy: str = "CNY",
                attrs_map: dict | None = None) -> str:
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
                      direction=dr, category=cat,
                      attrs=(attrs_map or {}).get(code))
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


# ---------------------------------------------------------- 9. 阶段1 ICP 内部往来自动配对


def _make_icp_pair(env, ar_amt: str, ap_amt: str):
    """造一对内部账套：甲有应收 ar_amt，乙有应付 ap_amt（其余走权益/银行，账套平衡）。"""
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        # 甲（应收方）：借应收账款 ar_amt，贷实收资本 ar_amt（资产=权益，平衡）
        a_id = _make_entity(s, f"应收方{ar_amt}", [
            {"code": "1122", "dr": ar_amt, "cr": "0"},
            {"code": "4001", "dr": "0", "cr": ar_amt},
        ])
        # 乙（应付方）：借银行存款 ap_amt，贷应付账款 ap_amt（资产=负债，平衡）
        b_id = _make_entity(s, f"应付方{ap_amt}", [
            {"code": "1002", "dr": ap_amt, "cr": "0"},
            {"code": "2202", "dr": "0", "cr": ap_amt},
        ])
        s.commit()
    return a_id, b_id


def test_propose_icp_pairs_and_apply(session, env):
    a_id, b_id = _make_icp_pair(env, "100", "100")
    res = CONS.propose_icp_eliminations(session, [a_id, b_id], YEAR, MONTH)
    # 应收 100 == 应付 100 → 全额配对
    assert Decimal(res["total_receivables"]) == Decimal("100")
    assert Decimal(res["total_payables"]) == Decimal("100")
    assert Decimal(res["matched"]) == Decimal("100")
    assert len(res["draft_eliminations"]) == 1
    _d = res["draft_eliminations"][0]
    # dr_code=应收(1122 减借) / cr_code=应付(2202 减贷)，与 _apply_eliminations 语义一致
    assert _d["dr_code"] == "1122" and _d["cr_code"] == "2202"
    assert Decimal(_d["amount"]) == Decimal("100")
    # 草稿喂回 consolidate 后，1122 与 2202 应抵消归零且表平衡
    bs = CONS.consolidated_balance_sheet(
        session, [a_id, b_id], YEAR, MONTH, eliminations=res["draft_eliminations"]
    )
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
    assert bs["balanced"] is True


def test_propose_icp_asymmetric(session, env):
    a_id, b_id = _make_icp_pair(env, "150", "100")  # 应收 150 > 应付 100
    res = CONS.propose_icp_eliminations(session, [a_id, b_id], YEAR, MONTH)
    assert Decimal(res["total_receivables"]) == Decimal("150")
    assert Decimal(res["total_payables"]) == Decimal("100")
    assert Decimal(res["matched"]) == Decimal("100")
    assert Decimal(res["unmatched_receivables"]) == Decimal("50")
    # 应收>应付：提示不对称（差额可能为外部往来）
    assert any("不对称" in n for n in res["notes"])


def test_propose_icp_is_readonly(session, env):
    a_id, b_id = _make_icp_pair(env, "100", "100")
    before = len(session.scalars(select(Voucher)).all())
    CONS.propose_icp_eliminations(session, [a_id, b_id], YEAR, MONTH)
    assert len(session.scalars(select(Voucher)).all()) == before


# ---------------------------------------------------------- 10. 阶段1 COI 长投/权益抵销


def _make_coi_group(env, parent_invest: str, sub_equity: list[dict],
                    parent_has_goodwill: bool = True):
    """造母公司 + 子公司一对。

    parent_invest：母公司长投 1511 借方额（资金来自股东投入，资产内部转换，账套平衡）。
    sub_equity：子公司权益 lines，如 [{"code":"4001","cr":"1000"},{"code":"4103","cr":"200"}]
                子公司另收投资现金，借 1002 等额（保证账套平衡）。
    """
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        p_lines = [
            {"code": "1002", "dr": parent_invest, "cr": "0"},
            {"code": "4001", "dr": "0", "cr": parent_invest},
            {"code": "1511", "dr": parent_invest, "cr": "0"},
            {"code": "1002", "dr": "0", "cr": parent_invest},
        ]
        if parent_has_goodwill:
            p_lines.append({"code": "1911", "dr": "0", "cr": "0"})  # 仅建科目(余额0)
        p_id = _make_entity(s, "母公司P", p_lines)
        sub_amt = sum(Decimal(str(e["cr"])) for e in sub_equity)
        s_lines = [{"code": "1002", "dr": str(sub_amt), "cr": "0"}]
        for e in sub_equity:
            s_lines.append({"code": e["code"], "dr": "0", "cr": e["cr"]})
        s_id = _make_entity(s, "子公司S", s_lines)
        s.commit()
    return p_id, s_id


def test_propose_coi_wholly_owned_and_apply(session, env):
    p_id, s_id = _make_coi_group(
        env, parent_invest="1200", sub_equity=[
            {"code": "4001", "cr": "1000"}, {"code": "4103", "cr": "200"},
        ],
    )
    res = CONS.propose_coi_eliminations(
        session, [p_id, s_id], YEAR, MONTH, ownership={s_id: "1.0"}
    )
    assert Decimal(res["parent"]["investment"]) == Decimal("1200")
    sub = res["subsidiaries"][0]
    assert Decimal(sub["equity_total"]) == Decimal("1200")
    assert Decimal(sub["attributable"]) == Decimal("1200")
    assert Decimal(sub["goodwill"]) == Decimal("0")        # 长投 == 应享权益
    assert Decimal(sub["minority_interest"]) == Decimal("0")
    # 消除：借 1511 / 贷 子公司各权益（按比例 1.0）
    _elims = {(e["dr_code"], e["cr_code"]): Decimal(e["amount"])
              for e in sub["suggested_eliminations"]}
    assert _elims[("1511", "4001")] == Decimal("1000")
    assert _elims[("1511", "4103")] == Decimal("200")
    # 草稿喂回 consolidate：长投与子公司权益抵消，合并表平衡
    bs = CONS.consolidated_balance_sheet(
        session, [p_id, s_id], YEAR, MONTH, ownership={s_id: "1.0"},
        eliminations=res["all_suggested_eliminations"],
    )
    # 1511（长投）应完全抵消为 0
    inv_sum = sum(
        r["ending"] for grp in bs["consolidated"]["assets"]["items"]
        for r in grp["accounts"] if r["code"] == "1511"
    )
    assert inv_sum == Decimal("0")
    assert bs["balanced"] is True


def test_propose_coi_controlling_with_goodwill(session, env):
    p_id, s_id = _make_coi_group(
        env, parent_invest="1500", sub_equity=[
            {"code": "4001", "cr": "1000"}, {"code": "4103", "cr": "200"},
        ],
    )
    res = CONS.propose_coi_eliminations(
        session, [p_id, s_id], YEAR, MONTH, ownership={s_id: "0.8"}
    )
    sub = res["subsidiaries"][0]
    # 长投 1500，应享权益 0.8×1200=960，商誉=540
    assert Decimal(sub["equity_total"]) == Decimal("1200")
    assert Decimal(sub["attributable"]) == Decimal("960")
    assert Decimal(sub["goodwill"]) == Decimal("540")
    assert Decimal(sub["minority_interest"]) == Decimal("240")   # 0.2×1200
    # 消除：仅子公司权益按 0.8 比例配比（商誉改为披露，不再造分录）
    _elims = {(e["dr_code"], e["cr_code"]): Decimal(e["amount"])
              for e in sub["suggested_eliminations"]}
    assert _elims[("1511", "4001")] == Decimal("800")
    assert _elims[("1511", "4103")] == Decimal("160")
    assert ("1911", "1511") not in _elims   # 商誉不进消除，仅披露
    # 商誉（长投>应享权益）应当在 notes 中披露，提示 Boss 手工重分类
    assert any("商誉" in n and "540" in n for n in res["notes"])


def test_propose_coi_requires_parent_clarification(session, env):
    p1, s1 = _make_coi_group(
        env, parent_invest="1200", sub_equity=[{"code": "4001", "cr": "1000"}],
    )
    # parent_id 不在 ledger_set_ids 内 → 应抛 ConsolidationError
    try:
        CONS.propose_coi_eliminations(
            session, [p1, s1], YEAR, MONTH, parent_id="not_a_real_id"
        )
        assert False, "parent_id 非法时应抛 ConsolidationError"
    except CONS.ConsolidationError:
        pass


def test_propose_coi_is_readonly(session, env):
    p_id, s_id = _make_coi_group(
        env, parent_invest="1200", sub_equity=[{"code": "4001", "cr": "1000"}],
    )
    before = len(session.scalars(select(Voucher)).all())
    CONS.propose_coi_eliminations(
        session, [p_id, s_id], YEAR, MONTH, ownership={s_id: "1.0"}
    )
    assert len(session.scalars(select(Voucher)).all()) == before


# ---------------------------------------------------------- 6. 分层汇率（阶段2）

def test_layered_fx_close_vs_average(session, env):
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        a_id = env["ids"]["A"]                      # 母公司 A（CNY，基准币种）
        u_id = _make_entity(s, "美元U", [
            {"code": "1002", "dr": "500", "cr": "0"},    # 银行 500（资产）
            {"code": "6001", "dr": "0", "cr": "500"},    # 收入 500
            {"code": "6602", "dr": "100", "cr": "0"},    # 费用 100
            {"code": "1002", "dr": "0", "cr": "100"},
        ], ccy="USD")
        s.commit()

    # 分层汇率：USD 期末(closing)=2.0、平均(average)=1.0，拉开口径差异；
    # 基准币种 A(CNY) 也须显式声明（既有币种检查要求全体账套都入册）
    fx = {a_id: {"closing": "1", "average": "1"}, u_id: {"closing": "2", "average": "1"}}

    bs = CONS.consolidated_balance_sheet(
        session, [a_id, u_id], YEAR, MONTH, fx_rates=fx
    )
    # U 银行余额 400 (dr500-cr100) → closing 2.0 → 800；A 银行 800 → 1
    assert bs["consolidated"]["assets"]["total"] == Decimal("1600")

    inc = CONS.consolidated_income_statement(
        session, [a_id, u_id], YEAR, MONTH, fx_rates=fx
    )
    # U 收入 500 → average 1.0 → 500；A 收入 1000；U 费用 100 → 100；A 费用 200
    assert inc["revenue"] == Decimal("1500")
    assert inc["expense"] == Decimal("300")
    assert inc["net_profit"] == Decimal("1200")


def test_layered_fx_scalar_backward_compat(session, env):
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        a_id = env["ids"]["A"]
        u_id = _make_entity(s, "美元U2", [
            {"code": "1002", "dr": "500", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "500"},
            {"code": "6602", "dr": "100", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "100"},
        ], ccy="USD")
        s.commit()

    # 旧形态：标量汇率 2.0 —— BS 与 IS 统一按 2.0 折算（向后兼容）；
    # 基准币种 A(CNY) 也须显式声明
    fx = {a_id: "1", u_id: "2"}
    bs = CONS.consolidated_balance_sheet(
        session, [a_id, u_id], YEAR, MONTH, fx_rates=fx
    )
    assert bs["consolidated"]["assets"]["total"] == Decimal("1600")  # 800 + 400*2
    inc = CONS.consolidated_income_statement(
        session, [a_id, u_id], YEAR, MONTH, fx_rates=fx
    )
    # 标量：利润表也用 2.0 → U 收入 1000（而非分层的 500）
    assert inc["revenue"] == Decimal("2000")


# ---------------------------------------------------------- 7. 合并现金流量表（阶段2）

def _make_cashflow_group(env):
    """构造母子现金流场景：母经营+对子投资支付；子经营+吸收母投资。"""
    from sqlalchemy.orm import Session as _S

    engine = create_engine(env["url"])
    with _S(engine) as s:
        a_id = _make_entity(s, "母现金流A", [
            {"code": "1002", "dr": "1000", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "1000"},     # 经营流入 1000
            {"code": "6602", "dr": "200", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "200"},       # 经营流出 200
            {"code": "1511", "dr": "800", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "800"},       # 投资支付 800（内部）
        ], attrs_map={"1511": {"cash_flow_item": "投资支付的现金"}})
        b_id = _make_entity(s, "子现金流B", [
            {"code": "1002", "dr": "500", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "500"},       # 经营流入 500
            {"code": "6602", "dr": "100", "cr": "0"},
            {"code": "1002", "dr": "0", "cr": "100"},       # 经营流出 100
            {"code": "1002", "dr": "800", "cr": "0"},
            {"code": "4001", "dr": "0", "cr": "800"},       # 吸收投资 800（内部）
        ], attrs_map={"4001": {"cash_flow_item": "吸收投资收到的现金"}})
        s.commit()
    return a_id, b_id


def test_consolidated_cash_flow_sum_and_reconcile(session, env):
    a_id, b_id = _make_cashflow_group(env)
    cf = CONS.consolidated_cash_flow(session, [a_id, b_id], YEAR, MONTH)
    # 经营：流入 1500(1000+500) / 流出 300(200+100) / 净 1200
    assert cf["categories"]["operating"]["in"] == Decimal("1500")
    assert cf["categories"]["operating"]["out"] == Decimal("300")
    assert cf["categories"]["operating"]["net"] == Decimal("1200")
    # 投资：流出 800（母投资支付）；融资：流入 800（子吸收投资）
    assert cf["categories"]["investing"]["out"] == Decimal("800")
    assert cf["categories"]["financing"]["in"] == Decimal("800")
    # 净增加 = 1200 - 800 + 800 = 1200；勾稽：期初0 + 净增 = 期末
    assert cf["net_increase"] == Decimal("1200")
    assert cf["reconcile"]["closing_cash"] == Decimal("1200")
    assert cf["balanced"] is True
    assert cf["eliminations"] == []


def test_consolidated_cash_flow_apply_eliminations(session, env):
    a_id, b_id = _make_cashflow_group(env)
    elims = [
        {"category": "investing", "item": "投资支付的现金", "amount": "800"},
        {"category": "financing", "item": "吸收投资收到的现金", "amount": "800"},
    ]
    cf = CONS.consolidated_cash_flow(
        session, [a_id, b_id], YEAR, MONTH, eliminations=elims
    )
    # 抵消后：投资支付归零、吸收投资归零（内部现金往来从合并中剔除）
    assert cf["categories"]["investing"]["out"] == Decimal("0")
    assert cf["categories"]["financing"]["in"] == Decimal("0")
    # 勾稽仍成立：净增加 = 经营1200 + 投资0 + 融资0 = 1200
    assert cf["net_increase"] == Decimal("1200")
    assert cf["reconcile"]["closing_cash"] == Decimal("1200")
    assert cf["balanced"] is True
    assert len(cf["eliminations"]) == 2


def test_propose_cash_flow_eliminations_and_feed_back(session, env):
    a_id, b_id = _make_cashflow_group(env)
    prop = CONS.propose_cash_flow_eliminations(session, [a_id, b_id], YEAR, MONTH)
    # 识别内部权益投资镜像：投资支付 800 / 吸收投资 800 → 可抵消 800
    assert Decimal(prop["investing_out"]) == Decimal("800")
    assert Decimal(prop["financing_in"]) == Decimal("800")
    assert Decimal(prop["matched"]) == Decimal("800")
    assert len(prop["suggested_eliminations"]) == 2
    # 建议抵消项可直接喂回 consolidated_cash_flow
    cf = CONS.consolidated_cash_flow(
        session, [a_id, b_id], YEAR, MONTH,
        eliminations=prop["suggested_eliminations"],
    )
    assert cf["categories"]["investing"]["out"] == Decimal("0")
    assert cf["categories"]["financing"]["in"] == Decimal("0")
    assert cf["balanced"] is True


def test_consolidated_cash_flow_is_readonly(session, env):
    a_id, b_id = _make_cashflow_group(env)
    before = len(session.scalars(select(Voucher)).all())
    CONS.consolidated_cash_flow(session, [a_id, b_id], YEAR, MONTH)
    CONS.propose_cash_flow_eliminations(session, [a_id, b_id], YEAR, MONTH)
    assert len(session.scalars(select(Voucher)).all()) == before


