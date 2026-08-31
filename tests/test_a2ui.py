"""P1-05 阶段2 TDD：A2UI v0.9 消息生成（协议结构校验）。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.a2ui import CATALOG_BASIC, build_ledger_messages
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Subject, Voucher, VoucherLine
from kernel.opening import import_opening_balances
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import transition


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    import_opening_balances(
        s, ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=[
            {"account_code": "100201", "debit": "10000.00", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "10000.00"},
        ],
    )
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
        voucher_no="记-A1", voucher_date=date(2026, 8, 5), status="DRAFT",
        summary="课程收入", created_by=ids["subject_id"],
    )
    v.lines = [
        VoucherLine(line_no=1, account_id=accs["100201"].id,
                    debit=Decimal("3000.00"), credit=Decimal("0.00")),
        VoucherLine(line_no=2, account_id=accs["6001"].id,
                    debit=Decimal("0.00"), credit=Decimal("3000.00")),
    ]
    s.add(v)
    s.flush()

    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.flush()
    transition(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED")
    transition(s, voucher_id=v.id, actor={"type": "user", "id": reviewer.id}, target="APPROVED")
    post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]})
    s.commit()
    yield {"s": s, "ids": ids}
    s.close()


def test_a2ui_messages_follow_v0_9_protocol(ctx):
    s, ids = ctx["s"], ctx["ids"]
    out = build_ledger_messages(s, ids["ledger_set_id"], 2026, 8, "测试账套")
    assert out["protocol"] == "v0.9"
    msgs = out["messages"]
    assert [m["version"] for m in msgs] == ["v0.9", "v0.9", "v0.9"]
    assert set(msgs[0]) == {"version", "createSurface"}
    assert msgs[0]["createSurface"]["catalogId"] == CATALOG_BASIC
    assert msgs[0]["createSurface"]["surfaceId"] == out["surfaceId"]
    assert "updateComponents" in msgs[1]
    assert "updateDataModel" in msgs[2]


def test_a2ui_component_tree_and_data_model(ctx):
    s, ids = ctx["s"], ctx["ids"]
    out = build_ledger_messages(s, ids["ledger_set_id"], 2026, 8, "测试账套")
    comps = out["messages"][1]["updateComponents"]["components"]
    root = next(c for c in comps if c["id"] == "root")
    assert root["component"] == "Column"
    ids_in_tree = {c["id"] for c in comps}
    assert set(root["children"]) <= ids_in_tree
    # 每行一个 Text 组件，绑定数据模型 path
    line_comps = [c for c in comps if c["id"].startswith("line-")]
    assert len(line_comps) >= 4 and all(c["component"] == "Text" for c in line_comps)
    assert line_comps[0]["text"]["path"].startswith("/lines/")
    data = out["messages"][2]["updateDataModel"]
    assert data["path"] == "/" and len(data["value"]["lines"]) == len(line_comps)
    assert "净利润" in "\n".join(data["value"]["lines"])
    assert data["value"]["net_profit"] == "3,000.00"


def test_components_use_only_official_a2ui_fields():
    """回归：组件字段必须符合官方 basicCatalog strict schema。

    @a2ui/react v0.9 的 Text 只接受 text/weight/variant/accessibility
    （variant 枚举 h1~h5/caption/body）。写 style/title 等未定义字段会被
    zod strict 校验整条消息拒绝，前端显示「加载失败」。
    """
    from sqlalchemy import create_engine as ce

    from kernel.a2ui import build_ledger_messages

    eng = ce("sqlite://")
    Base.metadata.create_all(eng)
    s2 = Session(eng)
    ids2 = seed_demo_ledger(s2)
    import_chart_of_accounts(s2, ids2["ledger_set_id"], load_template_rows())
    s2.commit()

    out = build_ledger_messages(s2, ids2["ledger_set_id"], 2026, 8,
                                "测试账套", "small_business")
    comps = out["messages"][1]["updateComponents"]["components"]
    allowed = {
        "Text": {"text", "weight", "variant", "accessibility"},
        "Column": {"children", "justify", "align", "weight", "accessibility"},
    }
    for c in comps:
        ctype = c["component"]
        if ctype in allowed:
            extra = set(c) - {"id", "component"} - allowed[ctype]
            assert not extra, f"组件 {ctype}({c['id']}) 含非官方字段: {extra}"
