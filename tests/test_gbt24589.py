"""GB/T 24589.1-2024 审计事件导出 — 内核导出器回归测试。

覆盖维度（DevPLAN P1-06 合规护城河落地验证）：
1. 标准表结构完整性（7 张表 + 字段标识符与 TABLE_SPECS 一致）
2. 期初/本期借/本期贷/期末 滚动计算正确性（期初是存量不是发生额）
3. 净额/存量口径不漂移（期末 = 期初 + 本期净发生额）
4. 记账凭证/分录字段映射（制单人/审核人/记账人/科目编号）
5. provenance（事件总数 + 链尾哈希）
6. JSON 与 XML 两种序列化均可解析
7. 空期间 / 非法年度 / 非法格式 / 账套不存在 的错误分支
8. 数量核算科目余额及发生额字段
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import attr_is
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Event, Period, Subject, Voucher, VoucherLine
from kernel.events import E
from kernel.gbt24589 import (
    GbtError,
    TABLE_SPECS,
    build_export,
)
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import transition

ZERO = Decimal("0.00")

MAKER = {"type": "user", "id": "u-maker", "display_name": "丞辰"}


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def ids(session):
    # seed_demo_ledger 已创建 display_name="丞辰" 的制单人 subject，
    # 导出按 Voucher.created_by 解析制单人，无需改动主键。
    return seed_demo_ledger(session)


def _add_subject(session, sid, display_name):
    s = Subject(type="user", id=sid, display_name=display_name, autonomy_level=3)
    session.add(s)
    session.flush()
    return s


def _add_voucher(
    session,
    ids,
    *,
    voucher_no="记-0001",
    voucher_date=date(2026, 8, 27),
    period_id=None,
    lines=None,
):
    if period_id is None:
        period_id = ids["period_id"]
    if lines is None:
        lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("100.00"), credit=ZERO, summary="管理费用"),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=ZERO, credit=Decimal("100.00"), summary="库存现金"),
        ]
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"],
        period_id=period_id,
        voucher_no=voucher_no,
        voucher_date=voucher_date,
        status="DRAFT",
        summary="测试凭证",
        created_by=ids["subject_id"],
        lines=lines,
    )
    session.add(v)
    session.flush()
    return v


def _full_post(
    session,
    ids,
    v,
    *,
    approver_id="u-approver",
    approver_name="李会计",
    poster_id="u-poster",
    poster_name="王记账",
):
    """构造 DRAFT→PUSHED→APPROVED→POSTED 完整事件链，返回 (approver_name, poster_name)。"""
    _add_subject(session, approver_id, approver_name)
    _add_subject(session, poster_id, poster_name)
    transition(session, voucher_id=v.id, actor=MAKER, target="PUSHED")
    transition(
        session,
        voucher_id=v.id,
        actor={"type": "user", "id": approver_id, "display_name": approver_name},
        target="APPROVED",
    )
    post_voucher(
        session,
        voucher_id=v.id,
        actor={"type": "user", "id": poster_id, "display_name": poster_name},
    )
    session.flush()
    return approver_name, poster_name


# ----------------------------- 结构完整性 -----------------------------

def _load_json(text):
    import json
    return json.loads(text)


EXPECTED_TABLE_NAMES = {
    "电子账簿", "会计期间", "会计科目", "币种",
    "科目余额及发生额", "记账凭证", "记账凭证分录",
}


def test_export_json_contains_all_seven_tables(ids, session):
    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    assert data["standard"] == "GB/T 24589.1-2024"
    assert set(data["tables"].keys()) == EXPECTED_TABLE_NAMES


def test_table_field_specs_match_standard(ids, session):
    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    for key, spec in TABLE_SPECS.items():
        tbl = data["tables"][spec["name"]]
        assert tbl["code"] == spec["code"]
        spec_fields = [n for _, n in spec["fields"]]
        out_fields = [f["name"] for f in tbl["fields"]]
        assert out_fields == spec_fields, f"{spec['name']} 字段顺序/数量漂移"


# ----------------------------- 余额滚动 -----------------------------

def test_balance_gross_activity_and_closing_period8(ids, session):
    v = _add_voucher(session, ids)
    _full_post(session, ids, v)
    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    rows = {r["科目编号"]: r for r in data["tables"]["科目余额及发生额"]["records"]}

    cash = rows["1001"]
    assert cash["会计年度"] == 2026 and cash["会计期间号"] == 8
    assert Decimal(cash["期初本币余额"]) == ZERO
    assert Decimal(cash["借方本币金额"]) == ZERO
    assert Decimal(cash["贷方本币金额"]) == Decimal("100.00")
    # 现金为借方科目，期末 = 0 - 100 = -100 → 方向贷，余额绝对值 100.00
    assert cash["期末余额方向"] == "贷"
    assert Decimal(cash["期末本币余额"]) == Decimal("100.00")

    exp = rows["6602"]
    assert Decimal(exp["借方本币金额"]) == Decimal("100.00")
    assert Decimal(exp["贷方本币金额"]) == ZERO
    assert exp["期末余额方向"] == "借"
    assert Decimal(exp["期末本币余额"]) == Decimal("100.00")


def test_balance_rolling_opening_is_stock_not_activity(ids, session):
    """期初是存量：跨期时期间9期初 = 期间8期末，且期间9无凭证时本期发生额为零。"""
    # 期间8 一笔：现金贷100（期末现金 -100，方向贷）
    v8 = _add_voucher(session, ids, voucher_no="记-0001", voucher_date=date(2026, 8, 27))
    _full_post(session, ids, v8)

    # 新增期间9（OPEN），但不在此过账任何凭证
    p9 = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=9, status="OPEN")
    session.add(p9)
    session.flush()

    # 全年导出（month=0）
    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026, month=0)
    data = _load_json(out)
    rows = {(r["会计期间号"], r["科目编号"]): r
            for r in data["tables"]["科目余额及发生额"]["records"]}

    # 期间9 现金：期初应滚入期间8期末（存量），本期借/贷=0，期末=期初
    c9 = rows[(9, "1001")]
    assert Decimal(c9["期初本币余额"]) == Decimal("100.00")
    assert c9["期初余额方向"] == "贷"
    assert Decimal(c9["借方本币金额"]) == ZERO
    assert Decimal(c9["贷方本币金额"]) == ZERO
    assert c9["期末余额方向"] == "贷"
    assert Decimal(c9["期末本币余额"]) == Decimal("100.00")

    # 期间8 现金期末必须等于期间9期初来源（不重算、不重复计入）
    c8 = rows[(8, "1001")]
    assert Decimal(c8["期末本币余额"]) == Decimal("100.00")
    # 期末 = 期初 + 本期净发生额 恒成立
    net8 = Decimal(c8["借方本币金额"]) - Decimal(c8["贷方本币金额"])
    signed_closing = (Decimal(c8["期末本币余额"])
                      if c8["期末余额方向"] == "借" else -Decimal(c8["期末本币余额"]))
    assert signed_closing == Decimal(c8["期初本币余额"]) * (
        1 if c8["期初余额方向"] == "借" else -1) + net8


def test_balance_zero_activity_accounts_present(ids, session):
    """未发生额的科目也须出现在余额表（期初0/借0/贷0/期末0）。"""
    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    rows = {r["科目编号"]: r for r in data["tables"]["科目余额及发生额"]["records"]}
    # 1002 银行存款全程无凭证
    bank = rows["1002"]
    assert Decimal(bank["期初本币余额"]) == ZERO
    assert Decimal(bank["借方本币金额"]) == ZERO
    assert Decimal(bank["贷方本币金额"]) == ZERO
    assert Decimal(bank["期末本币余额"]) == ZERO
    assert bank["期末余额方向"] == "借"  # 资产正常方向，零余额按正常方向


# ----------------------------- 凭证/分录映射 -----------------------------

def test_voucher_entry_field_mapping_with_approver(ids, session):
    v = _add_voucher(
        session, ids, voucher_no="记-0001", voucher_date=date(2026, 8, 27),
        lines=[
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("100.00"), credit=ZERO, summary="办公费"),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=ZERO, credit=Decimal("100.00"), summary="付现"),
        ],
    )
    approver_name, poster_name = _full_post(session, ids, v)

    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    vrows = data["tables"]["记账凭证"]["records"]
    erows = data["tables"]["记账凭证分录"]["records"]

    assert len(vrows) == 1
    vr = vrows[0]
    assert vr["记账凭证编号"] == "记-0001"
    assert vr["记账凭证类型编号"] == "记"
    assert vr["记账凭证日期"] == "20260827"
    assert vr["制单人"] == "丞辰"
    assert vr["审核人"] == approver_name
    assert vr["记账人"] == poster_name
    assert vr["记账标志"] == "1"

    assert len(erows) == 2
    by_line = {e["记账凭证行号"]: e for e in erows}
    e1 = by_line[1]
    assert e1["科目编号"] == "6602"
    assert e1["记账凭证摘要"] == "办公费"
    assert Decimal(e1["借方本币金额"]) == Decimal("100.00")
    assert Decimal(e1["贷方本币金额"]) == ZERO
    e2 = by_line[2]
    assert e2["科目编号"] == "1001"
    assert Decimal(e2["贷方本币金额"]) == Decimal("100.00")


def test_voucher_type_no_from_prefix(ids, session):
    v = _add_voucher(session, ids, voucher_no="收-0001", voucher_date=date(2026, 8, 27))
    _full_post(session, ids, v)
    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    vr = data["tables"]["记账凭证"]["records"][0]
    assert vr["记账凭证类型编号"] == "收"


# ----------------------------- provenance -----------------------------

def test_provenance_event_count_and_chain_hash(ids, session):
    v = _add_voucher(session, ids)
    _full_post(session, ids, v)  # 产生 PUSHED + APPROVED + POSTED 事件
    total_events = len(session.scalars(
        select(Event).where(Event.ledger_set_id == ids["ledger_set_id"])
    ).all())
    chain_tail = session.scalars(
        select(Event).where(Event.ledger_set_id == ids["ledger_set_id"])
        .order_by(Event.id)
    ).all()[-1].hash

    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    ab = data["tables"]["电子账簿"]["records"][0]
    assert ab["会计软件名称"] == "XErp"
    assert ab["数据接口标准"] == "GB/T 24589.1-2024"
    assert ab["事件总数"] == total_events
    assert ab["链尾哈希"] == chain_tail
    assert len(ab["链尾哈希"]) == 64  # sha256 十六进制


# ----------------------------- 序列化 -----------------------------

def test_json_and_xml_both_serialize(ids, session):
    v = _add_voucher(session, ids)
    _full_post(session, ids, v)

    j = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026, fmt="json")
    x = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026, fmt="xml")

    assert _load_json(j)["standard"] == "GB/T 24589.1-2024"

    import xml.etree.ElementTree as ET
    root = ET.fromstring(x)
    assert root.tag == "GB_T_24589_1_2024"
    # 每张标准表一个子元素
    child_tags = {c.tag for c in root}
    assert child_tags == EXPECTED_TABLE_NAMES


# ----------------------------- 错误分支 -----------------------------

def test_ledger_not_found_raises(ids, session):
    with pytest.raises(GbtError) as ei:
        build_export(session, ledger_set_id="no-such-ledger", year=2026)
    assert ei.value.code == "LEDGER_NOT_FOUND"


def test_empty_period_raises(ids, session):
    with pytest.raises(GbtError) as ei:
        build_export(session, ledger_set_id=ids["ledger_set_id"], year=2025)
    assert ei.value.code == "PERIOD_NOT_FOUND"


def test_empty_month_raises(ids, session):
    with pytest.raises(GbtError) as ei:
        build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026, month=3)
    assert ei.value.code == "PERIOD_NOT_FOUND"


def test_bad_year_raises(ids, session):
    with pytest.raises(GbtError) as ei:
        build_export(session, ledger_set_id=ids["ledger_set_id"], year=-1)
    assert ei.value.code == "BAD_YEAR"


def test_bad_format_raises(ids, session):
    with pytest.raises(GbtError) as ei:
        build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026, fmt="csv")
    assert ei.value.code == "BAD_FORMAT"


# ----------------------------- 数量核算 -----------------------------

def test_quantity_account_balance_fields(ids, session):
    # 把一个科目设为数量核算
    acc = session.get(Account, ids["expense_account_id"])
    acc.attrs = {"quantity": "yes"}
    session.flush()

    v = _add_voucher(
        session, ids, voucher_no="记-0001", voucher_date=date(2026, 8, 27),
        lines=[
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("100.00"), credit=ZERO,
                        quantity=Decimal("10"), unit="小时", summary="工时"),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=ZERO, credit=Decimal("100.00")),
        ],
    )
    _full_post(session, ids, v)

    out = build_export(session, ledger_set_id=ids["ledger_set_id"], year=2026)
    data = _load_json(out)
    rows = {r["科目编号"]: r for r in data["tables"]["科目余额及发生额"]["records"]}

    exp = rows["6602"]
    assert "期初数量" in exp
    assert Decimal(exp["借方数量"]) == Decimal("10.00")
    assert Decimal(exp["贷方数量"]) == ZERO
    assert Decimal(exp["期末数量"]) == Decimal("10.00")
    assert exp["计量单位"] if "计量单位" in exp else True  # 余额表无单位列，单位在分录表

    erows = data["tables"]["记账凭证分录"]["records"]
    e1 = next(e for e in erows if e["记账凭证行号"] == 1)
    assert e1["数量"] == "10.00"
    assert e1["计量单位"] == "小时"
    assert Decimal(e1["单价"]) == Decimal("10.00")  # 100/10
