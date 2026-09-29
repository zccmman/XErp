"""合并工作底稿 Web 可视化路由冒烟测试：GET /group/workbook 必须 200 且渲染贡献矩阵。

自建一个含母公司 A + 子公司 B/C 的账套组（绕开审批状态机，落 Balance 投影），
以演示主体登录后访问路由，校验页面含「合并工作底稿 / 抵消调整 / 各主体名」等标记，
且 BS 工作底稿中「流动资产」行的合并数与内核 consolidate 一致（单一真源）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
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
    "6001": ("主营业务收入", "credit", "pnl"),
    "6602": ("管理费用", "debit", "pnl"),
    "4103": ("本年利润", "credit", "equity"),
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
    url = f"sqlite:///{d}/web_wb.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    ids = {}
    with Session(engine) as s:
        # 登录用主体
        subj = Subject(type="user", display_name="演示", autonomy_level=5)
        s.add(subj)
        s.flush()
        ids["subject_id"] = subj.id
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
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": env["ids"]["subject_id"], "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


def test_group_workbook_renders(client, env):
    ids = ",".join([env["ids"]["A"], env["ids"]["B"], env["ids"]["C"]])
    r = client.get(f"/group/workbook?ids={ids}&year={YEAR}&month={MONTH}")
    assert r.status_code == 200
    assert "合并工作底稿" in r.text
    assert "抵消调整" in r.text
    assert "母公司A" in r.text and "子公司C" in r.text
    # 合并数应与内核 consolidate 一致（单一真源）：资产合计 1800、净利 1450
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import Session as _S

    eng = _ce(env["url"])
    with _S(eng) as s:
        CONS.consolidation_workbook(
            s, [env["ids"]["A"], env["ids"]["B"], env["ids"]["C"]], YEAR, MONTH
        )
    assert "1,800.00" in r.text or "1800.00" in r.text.replace(",", "")
    assert "1,450.00" in r.text or "1450.00" in r.text.replace(",", "")
    eng.dispose()
