"""P-12 合并报表 Web 可视化深化路由测试：现金流量表 / 抵消建议 / 血缘下钻。

自建含母公司 A（持股 100）+ 子公司 B（持股 80）的账套组（绕开审批状态机，落 Balance
投影，并写入内部往来应收/应付、长期股权投资、子公司权益），以演示主体登录后访问三个
新路由，校验 200 + 关键 token，且合并数以内核 consolidate/consolidated_cash_flow/
consolidation_lineage 单一真源为准。
"""

from datetime import date
from decimal import Decimal
import json

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
from kernel.webapp import _fmt

COA = {
    "1002": ("银行存款", "debit", "asset"),
    "1122": ("应收账款", "debit", "asset"),
    "1511": ("长期股权投资", "debit", "asset"),
    "2202": ("应付账款", "credit", "liability"),
    "4001": ("实收资本", "credit", "equity"),
    "6001": ("主营业务收入", "credit", "pnl"),
    "6602": ("管理费用", "debit", "pnl"),
}

YEAR, MONTH = 2026, 9


def _make_entity(s: Session, name: str, lines: list[dict]) -> str:
    ls = LedgerSet(name=name, accounting_standard="small_business",
                   functional_currency="CNY")
    s.add(ls)
    s.flush()
    accs = {}
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
    agg = {}
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
    url = f"sqlite:///{d}/web_group_p12.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    ids = {}
    with Session(engine) as s:
        subj = Subject(type="user", display_name="演示", autonomy_level=5)
        s.add(subj)
        s.flush()
        ids["subject_id"] = subj.id
        # 母公司 A：实收资本 5000 入账、投资子公司 1511=1000、内部应收 1122=300/应付 2202=200
        ids["A"] = _make_entity(s, "母公司A", [
            {"code": "1002", "dr": "5000", "cr": "1000"},
            {"code": "4001", "dr": "0", "cr": "5000"},
            {"code": "1122", "dr": "300", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "300"},
            {"code": "2202", "dr": "0", "cr": "200"},
            {"code": "6602", "dr": "200", "cr": "0"},
            {"code": "1511", "dr": "1000", "cr": "0"},
        ])
        # 子公司 B：实收资本 1000、内部应付 2202=100/应收 1122=100
        ids["B"] = _make_entity(s, "子公司B", [
            {"code": "1002", "dr": "1000", "cr": "0"},
            {"code": "4001", "dr": "0", "cr": "1000"},
            {"code": "1122", "dr": "100", "cr": "0"},
            {"code": "6001", "dr": "0", "cr": "100"},
            {"code": "2202", "dr": "0", "cr": "100"},
            {"code": "6602", "dr": "100", "cr": "0"},
        ])
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": env["ids"]["subject_id"], "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


def _ids(env):
    return ",".join([env["ids"]["A"], env["ids"]["B"]])


def _own(env):
    return json.dumps({env["ids"]["B"]: "80"})  # 子公司 B 持股 80%


# ---------- 合并现金流量表 ----------

def test_group_cashflow_renders(client, env):
    r = client.get("/group/cashflow", params={
        "ids": _ids(env), "year": YEAR, "month": MONTH, "ownership": _own(env)})
    assert r.status_code == 200, r.text[:500]
    assert "合并现金流量表" in r.text
    assert "经营活动现金流" in r.text
    assert "投资活动现金流" in r.text
    assert "筹资活动现金流" in r.text
    assert "现金勾稽" in r.text
    assert "期初现金" in r.text and "期末现金" in r.text


def test_group_cashflow_single_source(client, env):
    """页面期末现金必须与内核 consolidated_cash_flow 单一真源一致。"""
    r = client.get("/group/cashflow", params={
        "ids": _ids(env), "year": YEAR, "month": MONTH, "ownership": _own(env)})
    eng = create_engine(env["url"])
    with Session(eng) as s:
        res = CONS.consolidated_cash_flow(
            s, [env["ids"]["A"], env["ids"]["B"]], YEAR, MONTH,
            ownership={env["ids"]["B"]: Decimal("0.8")})
    eng.dispose()
    assert _fmt(res["reconcile"]["closing_cash"]) in r.text


# ---------- 内部抵消建议（ICP / COI / 现金流） ----------

def test_group_proposals_renders(client, env):
    r = client.get("/group/proposals", params={
        "ids": _ids(env), "year": YEAR, "month": MONTH, "ownership": _own(env)})
    assert r.status_code == 200, r.text[:500]
    assert "内部抵消建议" in r.text
    assert "AI 只产草稿" in r.text                    # HITL 铁律宣言
    assert "绝不自行应用" in r.text
    assert "内部往来配对" in r.text                    # ICP
    assert "长期股权投资 / 权益抵销" in r.text          # COI
    assert "内部投资现金流镜像配对" in r.text          # 现金流
    assert "dr_code" in r.text                        # 可喂回草稿 JSON


def test_group_proposals_icp_coi_values(client, env):
    """ICP 可配对 = min(应收 400, 应付 300) = 300；COI 母公司=A、商誉=200。"""
    r = client.get("/group/proposals", params={
        "ids": _ids(env), "year": YEAR, "month": MONTH, "ownership": _own(env)})
    assert "母公司A" in r.text
    assert "300.00" in r.text                         # ICP 可配对 300
    # COI：子公司 B 商誉 = 长投 1000 − 应享权益 800 = 200
    eng = create_engine(env["url"])
    with Session(eng) as s:
        coi = CONS.propose_coi_eliminations(
            s, [env["ids"]["A"], env["ids"]["B"]], YEAR, MONTH,
            ownership={env["ids"]["B"]: Decimal("0.8")})
    eng.dispose()
    assert coi["parent"]["name"] == "母公司A"
    # 商誉 = 长投 1000 − 应享权益(0.8×1000) = 200（余额 3 位小数精度）
    assert Decimal(coi["subsidiaries"][0]["goodwill"]) == Decimal("200")
    assert str(coi["subsidiaries"][0]["goodwill"]) in r.text


# ---------- 合并血缘下钻 ----------

def test_group_lineage_renders(client, env):
    r = client.get("/group/lineage", params={
        "ids": _ids(env), "year": YEAR, "month": MONTH, "group": "流动资产"})
    assert r.status_code == 200, r.text[:500]
    assert "合并血缘下钻" in r.text
    assert "流动资产" in r.text
    assert "源凭证" in r.text
    assert "记-0001" in r.text                          # 下钻到凭证流
    assert "母公司A" in r.text and "子公司B" in r.text  # 各主体分项


def test_group_lineage_single_source(client, env):
    """集团合并期末余额必须与内核 consolidation_lineage 单一真源一致。"""
    r = client.get("/group/lineage", params={
        "ids": _ids(env), "year": YEAR, "month": MONTH, "group": "流动资产"})
    eng = create_engine(env["url"])
    with Session(eng) as s:
        res = CONS.consolidation_lineage(
            s, [env["ids"]["A"], env["ids"]["B"]], YEAR, MONTH, group="流动资产")
    eng.dispose()
    # 流动资产 = 1002 + 1122；A: 4000+300，B: 1000+100 → 5400
    assert res["consolidated_ending"] == Decimal("5400")
    assert _fmt(res["consolidated_ending"]) in r.text
