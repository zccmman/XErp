"""阶段1：往来重分类列报（本体 reclass_pairs 第一次进报表）。

断言重点是三件事：
  1. 科目对来自本体 relations.csv（改文件即改行为，不在代码里硬编码）
  2. 重分类只在资产/负债两边之间搬金额——资产=负债+权益仍成立
  3. 默认关闭不漂移；未挂往来维度的余额保守留在原报表项目
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Period, Subject, Voucher, VoucherLine
from kernel.reporting.reclass import reclass_pairs, reclassify
from kernel.reporting.statements import balance_sheet
from kernel.seed import seed_demo_ledger
from kernel.state import transition

Q = Decimal("0.01")


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="重分类审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    ids["reviewer"] = reviewer.id
    p = s.get(Period, ids["period_id"])
    ids["year"], ids["month"] = p.year, p.month

    def book(summary: str, lines: list[tuple]) -> str:
        """lines: (code, debit, credit, dims or None)；建一笔已过账凭证。"""
        accs = {a.code: a for a in s.scalars(select(Account)).all()}
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=f"记-RC{abs(hash(summary)) % 9000 + 1000}",
            voucher_date=date(ids["year"], ids["month"], 15),
            status="DRAFT",
            summary=summary,
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(
                line_no=i + 1,
                account_id=accs[code].id,
                debit=Decimal(dr or "0"),
                credit=Decimal(cr or "0"),
                aux_dims=dims,
            )
            for i, (code, dr, cr, dims) in enumerate(lines)
        ]
        s.add(v)
        s.flush()
        actor = {"type": "user", "id": ids["subject_id"]}
        transition(s, voucher_id=v.id, actor=actor, target="PUSHED")
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer"]}, target="APPROVED")
        from kernel.posting import post_voucher

        post_voucher(s, voucher_id=v.id, actor=actor)
        s.commit()
        return v.voucher_no

    ids["book"] = book
    ids["session"] = s
    yield ids
    s.close()


def _seed_partners(ctx):
    """构造含反向余额的往来账：客户乙预收、供应商丁预付、供应商庚多付。"""
    b = ctx["book"]
    b("甲赊销", [("1122", "1000", "", {"customer": "甲"}), ("1001", "", "1000", None)])
    b("乙预收", [("1122", "", "300", {"customer": "乙"}), ("1001", "300", "", None)])
    b("丙应付", [("2202", "", "500", {"supplier": "丙"}), ("1001", "500", "", None)])
    b("丁预付", [("2202", "200", "", {"supplier": "丁"}), ("1001", "", "200", None)])
    b("戊预付", [("1123", "400", "", {"supplier": "戊"}), ("1001", "", "400", None)])
    b("己预收", [("2203", "", "150", {"customer": "己"}), ("1001", "150", "", None)])
    b("庚多付", [("1123", "", "60", {"supplier": "庚"}), ("1001", "60", "", None)])
    b("无维度", [("1122", "", "80", None), ("1001", "80", "", None)])


# ---------- 1. 真源：科目对来自本体 ----------


def test_pairs_come_from_ontology():
    pairs = {(p["asset_code"], p["liability_code"]) for p in reclass_pairs()}
    assert ("1122", "2203") in pairs, pairs
    assert ("1123", "2202") in pairs, pairs


# ---------- 2. 重分类明细与方向 ----------


def test_reclassify_directions_and_untracked(ctx):
    _seed_partners(ctx)
    s, ids = ctx["session"], ctx
    r = reclassify(s, ids["ledger_set_id"], ids["year"], ids["month"])
    # 资产侧贷方余额（乙 300、庚 60）→ 负债；负债侧借方余额（丁 200）→ 资产
    assert r["to_liability"] == Decimal("360.00")
    assert r["to_asset"] == Decimal("200.00")
    assert r["to_liability"].quantize(Q) == r["to_liability"]
    # 未挂往来维度（80）保守留在原处，不参与重分类
    assert r["untracked"] == Decimal("80.00")
    got = {(i["account_code"], i["partner"], i["to_account_code"],
            str(i["amount"])) for i in r["items"]}
    assert ("1122", "乙", "2203", "300.00") in got, got
    assert ("1123", "庚", "2202", "60.00") in got, got
    assert ("2202", "丁", "1123", "200.00") in got, got
    # 方向正常的往来（甲/丙/戊/己）不出现在重分类明细里
    assert len(r["items"]) == 3, got


# ---------- 3. 报表消费：只搬金额，不破平衡 ----------


def test_balance_sheet_reclass_moves_between_sides(ctx):
    _seed_partners(ctx)
    s, ids = ctx["session"], ctx
    base = balance_sheet(s, ids["ledger_set_id"], ids["year"], ids["month"])
    rc = balance_sheet(s, ids["ledger_set_id"], ids["year"], ids["month"],
                       apply_reclass=True)
    assert base["balanced"] and rc["balanced"], "两种口径下都必须表内平衡"
    # 不重分类时，反向余额以负数留在科目净额里 → 资产与负债**同时虚减**；
    # 重分类把反向余额搬到对方科目后，两边同时增加 360（预收性质）+200（预付性质）
    assert rc["assets"]["total"] - base["assets"]["total"] == Decimal("560.00")
    assert rc["liabilities"]["total"] - base["liabilities"]["total"] == Decimal("560.00")
    # 未挂维度的 80 保守留在原处未搬：差额是 560 而非含它的 640
    assert rc["reclass"]["to_liability"] == Decimal("360.00")
    assert rc["reclass"]["untracked"] == Decimal("80.00")


def test_reclass_default_off_no_drift(ctx):
    """默认关闭：列报口径变更必须显式选择，升级不得让历史口径漂移。"""
    _seed_partners(ctx)
    s, ids = ctx["session"], ctx
    off = balance_sheet(s, ids["ledger_set_id"], ids["year"], ids["month"])
    assert off["reclass"] is None
    assert off["assets"]["total"] == balance_sheet(
        s, ids["ledger_set_id"], ids["year"], ids["month"], apply_reclass=False
    )["assets"]["total"]
