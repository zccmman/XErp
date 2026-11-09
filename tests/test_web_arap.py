"""往来管理与账龄 Web 视图（`/ledger/{ls_id}/arap`）契约测试。

DoD：
- 账龄分桶、合计金额全部来自内核 `aging_analysis` / `open_items`（ADR-002），
  页面不自行配平；
- FIFO 账龄 vs 单据级未清项的口径差额要如实披露，并指路核销（不静默吞掉）；
- 授信敞口超额要有显式告警，且仅客户维度出现；
- 维度切换 / 单位过滤 / 空态安全降级 / 全程只读（渲染不写任何表）。
"""

from tempfile import mkdtemp

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kernel.adapters import clear, ingest_event
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import ArapClearing, Party, Period, Voucher, VoucherLine
from kernel.reporting.credit import set_credit_limit
from kernel.seed import seed_demo_ledger

AS_OF = "2026-11-09"


def _ar_invoice(s, ids, actor, *, eid, customer, total, when):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="invoice.issued", event_id=eid, actor=actor,
        event={"invoice_no": eid, "customer": customer, "issued_at": when,
               "net_amount": f"{float(total) - 10:.2f}",
               "tax_amount": "10.00", "total_amount": f"{float(total):.2f}"},
    )


def _ar_payment(s, ids, actor, *, eid, customer, amount, when):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="payment.received", event_id=eid, actor=actor,
        event={"customer": customer, "received_at": when,
               "amount": f"{float(amount):.2f}"},
    )


def _ap_invoice(s, ids, actor, *, eid, supplier, total, when):
    return ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ocr",
        event_type="invoice.received", event_id=eid, actor=actor,
        event={"invoice_no": eid, "supplier": supplier, "invoice_date": when,
               "expense_category": "办公费", "net_amount": f"{float(total):.2f}",
               "tax_amount": "0.00", "total_amount": f"{float(total):.2f}"},
    )


def _make_env(name: str, with_data: bool = True) -> dict:
    clear()
    url = f"sqlite:///{mkdtemp()}/{name}.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        # 事件规则按业务日期找期间；补上跨月期间（status=CLOSED，避免影响
        # 预算页等「取首个 OPEN 期间」的既有页面默认口径）。
        for m in (6, 9, 10):
            s.add(Period(ledger_set_id=ids["ledger_set_id"], year=2026,
                         month=m, status="CLOSED"))
        s.flush()
        if with_data:
            actor = {"type": "user", "id": ids["subject_id"]}
            # 甲公司：两笔发票（6/10 与 10/20）+ 一笔回款，FIFO 冲最早发票
            _ar_invoice(s, ids, actor, eid="INV-A1", customer="甲公司",
                        total=12000, when="2026-06-10")
            _ar_invoice(s, ids, actor, eid="INV-A2", customer="甲公司",
                        total=30000, when="2026-10-20")
            _ar_payment(s, ids, actor, eid="PAY-A1", customer="甲公司",
                        amount=5000, when="2026-10-25")
            # 乙公司：临近/超授信；丙公司：60–90 天桶
            _ar_invoice(s, ids, actor, eid="INV-B1", customer="乙公司",
                        total=18000, when="2026-09-25")
            _ar_invoice(s, ids, actor, eid="INV-C1", customer="丙公司",
                        total=25000, when="2026-09-05")
            # 供应商维度
            _ap_invoice(s, ids, actor, eid="AP-1", supplier="丁供应商",
                        total=8000, when="2026-09-10")
            set_credit_limit(s, ledger_set_id=ids["ledger_set_id"],
                             dim_key="customer", partner="甲公司", limit="50000")
            set_credit_limit(s, ledger_set_id=ids["ledger_set_id"],
                             dim_key="customer", partner="乙公司", limit="10000")
            set_credit_limit(s, ledger_set_id=ids["ledger_set_id"],
                             dim_key="customer", partner="丙公司", limit="40000")
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture(scope="module")
def env():
    return _make_env("arap_web")


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login",
               data={"subject_id": env["ids"]["subject_id"], "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


def _get(client, env, q: str = ""):
    return client.get(f"/ledger/{env['ids']['ledger_set_id']}/arap"
                      f"?as_of={AS_OF}{q}")


def test_arap_page_renders_aging_buckets(client, env) -> None:
    r = _get(client, env)
    assert r.status_code == 200, r.text
    t = r.text
    assert "往来管理与账龄" in t and "账龄分析" in t
    assert "0–30 天" in t and "31–60 天" in t and "61–90 天" in t and "90 天以上" in t
    # FIFO：甲 30000(20天)+7000(152天)、乙 18000(45天)、丙 25000(75天)
    assert "30000.00" in t and "18000.00" in t and "25000.00" in t
    assert "7000.00" in t
    # 合计未结清 = 80000
    assert "80000.00" in t
    for name in ("甲公司", "乙公司", "丙公司"):
        assert name in t


def test_arap_page_discloses_reconciliation_gap(client, env) -> None:
    """回款未做单据级核销 → 两口径差额必须披露，不能静默对齐。"""
    t = _get(client, env).text
    assert "口径勾稽" in t
    assert "账龄未结清 80000.00" in t and "单据级未清项 85000.00" in t
    assert "差额 -5000.00" in t  # 回款已按 FIFO 冲减，但未做单据级核销
    assert "尚未做单据级核销" in t
    assert "本页不代执行" in t


def test_arap_page_credit_exposure_and_breach(client, env) -> None:
    t = _get(client, env).text
    assert "授信敞口" in t and "超额授信" in t
    assert "乙公司" in t and "10000.00" in t
    assert "超额" in t and "利用率" in t
    assert "额度是你在对话中配置" in t


def test_arap_page_supplier_dimension(client, env) -> None:
    t = _get(client, env, "&dim=supplier").text
    assert "应付 · 供应商" in t
    assert "丁供应商" in t
    assert "授信敞口" not in t  # 授信仅限客户维度
    assert "8000.00" in t


def test_arap_page_partner_filter(client, env) -> None:
    t = _get(client, env, "&partner=乙公司").text
    assert "乙公司" in t
    assert "甲公司" not in t and "丙公司" not in t
    assert "未清项明细" in t


def test_arap_page_is_readonly(client, env) -> None:
    engine = create_engine(env["url"])
    with Session(engine) as s:
        before = tuple(s.scalar(select(func.count()).select_from(m))
                       for m in (Voucher, VoucherLine, ArapClearing, Party, Period))
    r = _get(client, env)
    assert r.status_code == 200
    r2 = _get(client, env, "&dim=supplier")
    assert r2.status_code == 200
    with Session(engine) as s:
        after = tuple(s.scalar(select(func.count()).select_from(m))
                      for m in (Voucher, VoucherLine, ArapClearing, Party, Period))
    engine.dispose()
    assert before == after


def test_arap_page_empty_state() -> None:
    """无任何往来凭证：安全降级，不 500、不臆造余额。"""
    empty = _make_env("arap_web_empty", with_data=False)
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(empty["url"]))
    c.post("/login", data={"subject_id": empty["ids"]["subject_id"],
                           "password": ""}, follow_redirects=False)
    r = c.get(f"/ledger/{empty['ids']['ledger_set_id']}/arap?as_of={AS_OF}")
    assert r.status_code == 200, r.text
    assert "暂无往来数据" in r.text and "只读" in r.text
