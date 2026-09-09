"""Web 三表预测页契约测试（P1-01 Web 入口）。

锁定：
1. 账套工具条有「三表预测」入口；
2. /ledger/{id}/forecast 默认取最近 OPEN 期为基准，渲染基准情景三表
   （期间为列），且预测收入来自驱动推导（1000 × 1.03 = 1,030.00）；
3. 情景切换 best/worst/all 与期数 horizon 参数生效；
4. 非法情景回退基准、horizon 越界夹紧 1..36；
5. 报表页有指向预测页的深链。
"""

import tempfile
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Period, Subject
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webforecast.db"
    engine = create_engine(
        url, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        approver = Subject(type="user", display_name="审批人")
        s.add(approver)
        s.flush()
        ls_id = ids["ledger_set_id"]
        # 基准期：2026-09 OPEN + 一笔已过账收入 1000
        period = Period(ledger_set_id=ls_id, year=2026, month=9, status="OPEN")
        s.add(period)
        s.flush()
        v, _ = create_draft_voucher(
            s,
            ledger_set_id=ls_id,
            actor={"type": "user", "id": ids["subject_id"]},
            voucher_date=date(2026, 9, 10),
            summary="销售收入",
            lines=[
                {"account_code": "1002", "debit": "1000.00", "credit": ""},
                {"account_code": "6001", "debit": "", "credit": "1000.00"},
            ],
        )
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["subject_id"]},
                   target="PUSHED")
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": approver.id}, target="APPROVED")
        post_voucher(s, voucher_id=v.id,
                     actor={"type": "user", "id": approver.id})
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


def _client(env) -> TestClient:
    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": env["ids"]["subject_id"],
                               "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture()
def client(env):
    return _client(env)


LS = None  # 运行时取 env 的 ledger_set_id


def test_toolbar_has_forecast_entry(client, env):
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}")
    assert r.status_code == 200
    assert f"/ledger/{env['ids']['ledger_set_id']}/forecast" in r.text


def test_reports_page_links_to_forecast(client, env):
    r = client.get(f"/ledger/{env['ids']['ledger_set_id']}/reports")
    assert r.status_code == 200
    assert "/forecast" in r.text


def test_forecast_page_default_base_scenario(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast")
    assert r.status_code == 200
    assert "基准情景三表预测" in r.text
    assert "利润表" in r.text and "资产负债表" in r.text and "现金流量表" in r.text
    assert "全期平衡" in r.text
    # 预测期从基准期下月开始：2026-10；收入 = 1000 × (1+3%) = 1,030.00
    assert "2026-10" in r.text
    assert "1,030.00" in r.text
    # 基准期本身不作为预测期出现
    assert "2026-09</th>" not in r.text


def test_forecast_horizon_param(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast?horizon=3")
    assert r.status_code == 200
    assert "2026-12" in r.text   # 第 3 期
    assert "2027-01" not in r.text  # 第 4 期不出现


def test_forecast_scenario_best(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast?scenario=best")
    assert r.status_code == 200
    assert "乐观情景三表预测" in r.text


def test_forecast_scenario_all_comparison(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast?scenario=all")
    assert r.status_code == 200
    assert "三情景对比" in r.text
    assert "乐观 · 净利润" in r.text
    assert "悲观 · 期末现金" in r.text
    assert "三情景全期平衡" in r.text
    # 单情景明细表不应出现在对比视图
    assert "基准情景三表预测" not in r.text


def test_forecast_invalid_scenario_falls_back(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast?scenario=pivot")
    assert r.status_code == 200
    assert "基准情景三表预测" in r.text


def test_forecast_horizon_clamped(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast?horizon=999")
    assert r.status_code == 200
    assert "36 期" in r.text


def test_forecast_explicit_base_period(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/forecast?year=2026&month=9")
    assert r.status_code == 200
    assert "自 2026-09 起" in r.text


# ---------- P1 深化 · Web 假设编辑器 + 趋势 sparkline ----------

def test_forecast_form_assumption_editor_present(client, env):
    """表单含全部驱动假设输入（% 单位）与恢复默认链接。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/forecast").text
    for name in ("rev_growth", "gross_margin", "opex_ratio", "tax_rate",
                 "capex_pct", "dep_rate", "ar_days", "ap_days", "inv_days"):
        assert f'name={name}' in t, name
    assert 'name=custom value="1"' in t
    assert "恢复默认推导" in t


def test_forecast_default_badge_auto_derived(client, env):
    """默认进入显示「自动推导假设」徽标。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/forecast").text
    assert "自动推导假设" in t and "自定义假设" not in t


def test_forecast_custom_assumptions_applied(client, env):
    """custom=1 + 表单值 → 自定义徽标 + 假设回显为折算后小数。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(
        f"/ledger/{ls}/forecast?custom=1&rev_growth=5&gross_margin=40"
        "&ar_days=45&horizon=3"
    ).text
    assert "自定义假设" in t
    assert "收入增速 0.05" in t
    assert "毛利率 0.4" in t
    assert "应收 45 天" in t
    assert "自动推导假设" not in t


def test_forecast_custom_partial_fills_rest_from_derived(client, env):
    """只填部分假设：未填项回退推导默认，预测不拒服务。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(
        f"/ledger/{ls}/forecast?custom=1&rev_growth=10&horizon=2"
    ).text
    assert "自定义假设" in t
    assert "收入增速 0.1" in t


def test_forecast_custom_invalid_input_falls_back(client, env):
    """非法输入（abc）→ 该项回退推导默认，页面正常渲染。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(
        f"/ledger/{ls}/forecast?custom=1&rev_growth=abc&horizon=2"
    ).text
    assert "自动推导假设" in t  # 全部无效 = 无 override


def test_forecast_trend_sparkline(client, env):
    """单情景页含趋势区：净利润/期末现金 spark 条。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/forecast").text
    assert "<h3>趋势</h3>" in t
    assert 'class="sp sp-pos"' in t or 'class="sp sp-neg"' in t
