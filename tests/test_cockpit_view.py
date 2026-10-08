"""AI 原生财务驾驶舱（零日结账 + 持续预测 · 本地优先）渲染测试。

DoD：/cockpit 渲染出「本地优先」宣言 + 零日结账（持续对账）视图 + 持续预测（杠杆推演）
视图；全程只读、复用内核单一真源；缺种子时预测区安全降级而不 500。
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import LedgerSet, Period, Subject
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/cockpit.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()
        ids["reviewer_subject_id"] = reviewer.id
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post(
        "/login",
        data={"subject_id": env["ids"]["subject_id"], "password": ""},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture(scope="module")
def ls_info(env):
    engine = create_engine(env["url"])
    with Session(engine) as s:
        ls = s.get(LedgerSet, env["ids"]["ledger_set_id"])
        return (ls.id, ls.accounting_standard)


def test_cockpit_page_renders(client, ls_info):
    ls_id, _ = ls_info
    r = client.get(f"/ledger/{ls_id}/cockpit")
    assert r.status_code == 200, r.text[:500]
    assert "AI 原生财务驾驶舱" in r.text        # 页面标题
    assert "AI 驾驶舱" in r.text                # 侧边栏入口
    assert "零日结账" in r.text                  # 核心点①
    assert "持续预测" in r.text                  # 核心点②
    assert "本地优先" in r.text                  # XErp 差异化宣言
    assert "数据不出本机" in r.text              # 本地优先红线
    assert "哪个决策改结果" in r.text            # 持续预测主张
    assert "已对账" in r.text or "有差异" in r.text  # 子账↔总账对账健康度


def test_cockpit_data_aggregation(env, ls_info):
    from decimal import Decimal as _D

    from kernel.webapp import _cockpit_data

    engine = create_engine(env["url"])
    ls_id, std = ls_info
    with Session(engine) as s:
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ls_id)
        ).first()
        assert per is not None, "seed 账套应有期间"
        d = _cockpit_data(s, ls_id, per.year, per.month, std)
    # 零日结账区字段齐全
    assert isinstance(d["bs"], dict) and "assets" in d["bs"]
    assert isinstance(d["inc"], dict) and "net_profit" in d["inc"]
    assert isinstance(d["cf"], dict) and "operating" in d["cf"]
    assert isinstance(d["recv"], dict) and "ok" in d["recv"]
    assert isinstance(d["pay"], dict) and "ok" in d["pay"]
    assert isinstance(d["jev_items"], list)
    # 子账对账金额字段为数字或数字字符串（_fmt 能消费，不抛错）
    for key in ("subledger_total", "control_total", "difference"):
        _D(str(d["recv"].get(key, "0")))
        _D(str(d["pay"].get(key, "0")))
    # 持续预测：seed 账套应有实际种子 → forecast 非 None
    assert d["forecast"] is not None, "seed 账套应产出 what-if"
    fc = d["forecast"]
    assert "impact_summary" in fc and "variants" in fc
    # 6 个预设杠杆齐全
    assert set(fc["variants"].keys()) >= {
        "ar_acceleration", "ap_extension", "margin_compression",
        "growth_halt", "cost_inflation", "capex_surge",
    }


def test_cockpit_robust_on_empty_ledger(env):
    """无凭证的空账套：_cockpit_data 不抛异常，零日结账区字段仍正常返回。

    说明：what_if 对空账套不会抛异常（返回推导基准），故预测区不会进入降级分支；
    真正的降级分支（except）由异常触发——此处只验证空账套下聚合函数不崩、字段齐全。
    """
    from kernel.webapp import _cockpit_data

    engine = create_engine(env["url"])
    # 新建一个空账套（无凭证）
    with Session(engine) as s:
        ls = LedgerSet(name="空账套", accounting_standard="small_business")
        s.add(ls)
        s.commit()
        ls_id = ls.id
        from kernel.db.models import Period

        s.add(Period(ledger_set_id=ls_id, year=2026, month=9, status="OPEN"))
        s.commit()
    try:
        with Session(engine) as s:
            d = _cockpit_data(s, ls_id, 2026, 9, "small_business")
        # 零日结账区字段齐全（这是 cockpit 永远要能渲染的部分）
        assert isinstance(d["bs"], dict) and "assets" in d["bs"]
        assert isinstance(d["inc"], dict) and "net_profit" in d["inc"]
        assert isinstance(d["cf"], dict) and "operating" in d["cf"]
        assert isinstance(d["recv"], dict) and "ok" in d["recv"]
        assert isinstance(d["pay"], dict) and "ok" in d["pay"]
        assert isinstance(d["jev_items"], list)
        # 预测无论是否为 None 都不应抛错
        assert d["forecast"] is None or isinstance(d["forecast"], dict)
    finally:
        with Session(engine) as s:
            from kernel.db.models import Period

            s.query(Period).filter(Period.ledger_set_id == ls_id).delete()
            s.query(LedgerSet).filter(LedgerSet.id == ls_id).delete()
            s.commit()
