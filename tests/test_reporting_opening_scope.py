"""回归：期初余额不得混入「本期发生额」——会计口径隔离。

缺陷（端到端真机验证时发现）：
    建账当期导入期初 20 万后，账套页「发生额投影」显示
        1002 银行存款  借 350,000  贷 200,000
    原因有两层：
      1. 期初凭证与本期凭证共用同一份 balances 投影，页面直接展示 →
         期初被当成本期发生额
      2. force 重导时旧期初被「借贷互换」红字冲销，再记新期初 →
         发生额被进一步放大（20 万 + 冲销 20 万 + 新 15 万）

    更严重的是利润表：income_statement 排除了「结转-」却没排除「期初-」，
    期初一旦含损益类科目（6001/6602 等），建账当期利润表直接失真。

修复约定：
    - 期初余额 = 建账期初数，属「存量」，不是本期经营成果
    - 利润表取数必须排除「期初-」前缀（与排除「结转-」同理）
    - 现金流量表原本已把期初归入 opening_cash，保持
    - 资产负债表要的是期末余额（= 期初 + 本期发生额），**必须包含**期初
    - Web 科目余额表按 期初 / 本期借方 / 本期贷方 / 期末余额 四栏分列
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Subject, Voucher
from kernel.opening import import_opening_balances
from kernel.posting import post_voucher
from kernel.reporting.statements import balance_sheet, income_statement
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    # 独立审批人：NO_SELF_APPROVAL 是硬红线，制单人不能自己审批
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer_id"] = reviewer.id
    return s, ids


def _import_opening(s, ids, lines, **kw):
    return import_opening_balances(
        s,
        ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=lines,
        **kw,
    )


def _post(s, ids, no, debit_code, credit_code, amount):
    """直接造一张本期 POSTED 凭证（走正常审批状态机）。"""
    from datetime import date

    from kernel.db.models import Period, Voucher, VoucherLine

    acc = {a.code: a.id for a in s.scalars(select(Account)).all()}
    p = s.scalars(select(Period).where(Period.ledger_set_id == ids["ledger_set_id"])).first()
    # 凭证日期必须落在该期间内，否则内核报 PERIOD_MISMATCH
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"],
        period_id=p.id,
        voucher_no=no,
        voucher_date=date(p.year, p.month, 15),
        status="DRAFT",
        summary="本期业务",
        created_by=ids["subject_id"],
    )
    v.lines = [
        VoucherLine(line_no=1, account_id=acc[debit_code],
                    debit=Decimal(amount), credit=Decimal("0.00")),
        VoucherLine(line_no=2, account_id=acc[credit_code],
                    debit=Decimal("0.00"), credit=Decimal(amount)),
    ]
    s.add(v)
    s.flush()
    transition(s, voucher_id=v.id,
               actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED")
    # 审批人必须另有其人（NO_SELF_APPROVAL 硬红线）
    transition(s, voucher_id=v.id,
               actor={"type": "user", "id": ids["reviewer_id"]}, target="APPROVED")
    post_voucher(s, voucher_id=v.id,
                 actor={"type": "user", "id": ids["subject_id"]})
    s.commit()


def _item(inc, name):
    return next((i["amount"] for i in inc["items"] if i["item"] == name), Decimal("0"))


# ════════════════════════════════════════════════════════════
# 利润表：期初不得计入本期损益
# ════════════════════════════════════════════════════════════


def test_opening_does_not_leak_into_income_statement(ctx):
    """期初含营业收入 6001 时，建账当期利润表营业收入必须为 0。"""
    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "1000.00", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "1000.00"},
    ])
    s.commit()
    p = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).first()

    inc = income_statement(s, ids["ledger_set_id"], p.voucher_date.year,
                           p.voucher_date.month, "small_business")
    # 修复前：营业收入 = 1000（期初被当成本期收入）
    assert _item(inc, "营业收入") == Decimal("0.00"), (
        f"期初不得计入本期损益，实为 {_item(inc, '营业收入')}"
    )
    assert inc["revenue"] == Decimal("0.00")
    assert inc["net_profit"] == Decimal("0.00")


def test_current_period_still_counts_in_income_statement(ctx):
    """排除期初的同时，本期真实业务必须照常计入（不能误杀）。"""
    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "1000.00", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "1000.00"},
    ])
    s.commit()
    _post(s, ids, "记-9001", "6602", "1001", "300.00")  # 管理费用 300

    p = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).first()
    inc = income_statement(s, ids["ledger_set_id"], p.voucher_date.year,
                           p.voucher_date.month, "small_business")
    assert _item(inc, "营业收入") == Decimal("0.00")
    assert _item(inc, "管理费用") == Decimal("300.00"), "本期业务必须计入"
    assert inc["net_profit"] == Decimal("-300.00")


def test_force_reimport_does_not_distort_income_statement(ctx):
    """force 重导（含红字冲销）后利润表仍为 0 —— 冲销额也不得进损益。"""
    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "1000.00", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "1000.00"},
    ])
    s.commit()
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "800.00", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "800.00"},
    ], force=True)
    s.commit()

    p = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).first()
    inc = income_statement(s, ids["ledger_set_id"], p.voucher_date.year,
                           p.voucher_date.month, "small_business")
    assert inc["revenue"] == Decimal("0.00"), "红字冲销额不得计入损益"
    assert inc["net_profit"] == Decimal("0.00")


# ════════════════════════════════════════════════════════════
# 资产负债表：必须包含期初（期末余额口径）
# ════════════════════════════════════════════════════════════


def test_balance_sheet_still_includes_opening(ctx):
    """资产负债表取期末余额，期初必须计入 —— 排除期初会把报表抽空。"""
    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "200000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "200000.00"},
    ])
    s.commit()
    p = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).first()

    bs = balance_sheet(s, ids["ledger_set_id"], p.voucher_date.year,
                       p.voucher_date.month, "small_business")
    assert bs["assets"]["total"] == Decimal("200000.00"), bs["check"]
    assert bs["balanced"] is True, bs["check"]


# ════════════════════════════════════════════════════════════
# force 重导后必须账账相符（投影 vs 凭证明细）
# ════════════════════════════════════════════════════════════


def _period_of(s, ids):
    from kernel.db.models import Period

    return s.scalars(select(Period).where(
        Period.ledger_set_id == ids["ledger_set_id"])).first()


def test_force_reimport_keeps_projection_reconstructable(ctx):
    """force 重导后对账必须干净。

    修复前 _reverse_opening 只改余额投影、不落凭证明细，导致
    「投影 15 万 / 凭证明细 35 万」两个真相并存，reconcile 必报
    PROJECTION_MISMATCH。这对会计是致命的——凭证明细对不上账。
    """
    from kernel.reconcile import reconcile_ledger

    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "200000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "200000.00"},
    ])
    s.commit()
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "150000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "150000.00"},
    ], force=True)
    s.commit()

    p = _period_of(s, ids)
    rec = reconcile_ledger(s, ids["ledger_set_id"], p.year, p.month, "small_business")
    kinds = [i["kind"] for i in rec["issues"]]
    assert "PROJECTION_MISMATCH" not in kinds, (
        f"force 重导后投影与凭证明细必须一致，实为 {rec['issues']}"
    )
    assert rec["ok"] is True, rec["issues"]


def test_reversal_creates_real_voucher(ctx):
    """红字冲销必须生成真实凭证（可见、可对账），而非只偷偷改投影。"""
    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "200000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "200000.00"},
    ])
    s.commit()
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "150000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "150000.00"},
    ], force=True)
    s.commit()

    revs = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"],
        Voucher.voucher_no.like("冲销-%"),
    )).all()
    assert len(revs) == 1, f"应生成 1 张冲销凭证，实为 {len(revs)}"
    rev = revs[0]
    assert rev.voucher_no == "冲销-期初-0001"
    assert rev.status == "POSTED"
    # 借贷互换
    lines = {ln.account_id: (ln.debit, ln.credit) for ln in rev.lines}
    acc = {a.code: a.id for a in s.scalars(select(Account)).all()}
    assert lines[acc["1002"]] == (Decimal("0.00"), Decimal("200000.00"))
    assert lines[acc["3001"]] == (Decimal("200000.00"), Decimal("0.00"))


def test_reversal_voucher_not_counted_as_opening(ctx):
    """冲销凭证不得被当成「生效中期初」，否则下次 force 会把冲销再冲一遍。"""
    from kernel.opening import _active_opening_vouchers

    s, ids = ctx
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "200000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "200000.00"},
    ])
    s.commit()
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "150000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "150000.00"},
    ], force=True)
    s.commit()

    active = _active_opening_vouchers(s, ids["ledger_set_id"])
    nos = [v.voucher_no for v in active]
    assert nos == ["期初-0002"], nos
    # 再 force 一次仍只冲销 0002，不会动已冲销的 0001，也不会冲销「冲销-」
    _import_opening(s, ids, [
        {"account_code": "1002", "debit": "80000.00", "credit": ""},
        {"account_code": "3001", "debit": "", "credit": "80000.00"},
    ], force=True)
    s.commit()
    revs = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"],
        Voucher.voucher_no.like("冲销-%"),
    )).all()
    assert sorted(v.voucher_no for v in revs) == ["冲销-期初-0001", "冲销-期初-0002"]


# ════════════════════════════════════════════════════════════
# Web 科目余额表：期初 / 本期发生额 / 期末余额 分列
# ════════════════════════════════════════════════════════════


@pytest.fixture()
def web_ctx(tmp_path):
    """文件库版 ctx —— TestClient 需要能被多个连接打开的库。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'rpt.db'}")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer_id"] = reviewer.id
    ids["url"] = str(engine.url)
    engine.dispose()
    s.close()
    return ids


def test_dashboard_shows_separated_balance_table(web_ctx):
    """账套页需给出期初/本期/期末分列的科目余额表，而非混账的发生额投影。"""
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    engine = create_engine(web_ctx["url"])
    with Session(engine) as s:
        _import_opening(s, web_ctx, [
            {"account_code": "1002", "debit": "200000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "200000.00"},
        ])
        s.commit()
        _post(s, web_ctx, "记-9002", "6602", "1001", "500.00")
    engine.dispose()

    c = TestClient(build_app(web_ctx["url"]))
    c.post("/login", data={"subject_id": web_ctx["subject_id"], "password": ""},
           follow_redirects=False)
    page = c.get(f"/ledger/{web_ctx['ledger_set_id']}").text

    assert "科目余额表" in page, "应为科目余额表，而非『发生额投影』"
    for col in ("期初余额", "本期发生额", "期末余额"):
        assert col in page, f"缺少列：{col}"

    # 页面不应再出现「发生额投影」这个误导性标题
    assert "发生额投影" not in page

    # 抽取 1002 / 6602 两行，核对四栏数值（期初 / 借 / 贷 / 期末）
    rows = re.findall(
        r"<tr><td>(\d{4})</td><td>([^<]+)</td>"
        r"<td style=text-align:right>([\d.\-]+)</td>"
        r"<td style=text-align:right>([\d.\-]+)</td>"
        r"<td style=text-align:right>([\d.\-]+)</td>"
        r"<td style=text-align:right><b>([\d.\-]+)</b></td></tr>",
        page,
    )
    table = {r[0]: [r[1]] + [Decimal(x) for x in r[2:]] for r in rows}
    assert "1002" in table, page[:800]

    # 列序：[1]期初余额 [2]本期借方 [3]本期贷方 [4]期末余额
    # 1002：期初 20 万，本期无业务 → 借/贷为 0，期末仍 20 万
    op_bal, dr, cr, end_bal = table["1002"][1], table["1002"][2], \
        table["1002"][3], table["1002"][4]
    assert op_bal == Decimal("200000.00"), table
    assert dr == Decimal("0.00"), f"本期借方被期初污染：{table['1002']}"
    assert cr == Decimal("0.00"), f"本期贷方被期初污染：{table['1002']}"
    assert end_bal == Decimal("200000.00"), table

    # 6602：本期发生 500，期初 0
    assert table["6602"][1] == Decimal("0.00"), table
    assert table["6602"][2] == Decimal("500.00"), table
    assert table["6602"][4] == Decimal("500.00"), table
