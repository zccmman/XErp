"""P0-1 TDD：存货 / 固定资产 / 成本核算（ADR-002 单一真源）。

DoD（与 XErp-设计-P0-1 存货固定资产成本.md 对齐）：
- 收发存台账 / 累计折旧 / 成本对象发生额全部由 POSTED 凭证明细重建，不建投影表；
  存货经 VoucherLine.aux_dims["inventory_item"] + quantity 链接；固定资产经
  aux_dims["asset_no"] 链接；成本对象经 aux_dims[project|department] 链接。
- 计价（weighted_avg/moving_avg/fifo）、直线法折旧、制造费用分摊、完工结转
  均为**只读算子**，产出凭证草稿 lines；落库一律由调用方经既有 HITL。
- 红线：全部草稿函数不改账（凭证明细行数不变）。
- 主数据写工具（inventory_item_register / asset_register）仅落主数据表，不碰账本。

测试分两层：
1. 内核纯函数（内存库，最快、最贴近契约）；
2. MCP 工具层（文件库，验证 tool 接线 + 单一真源端到端重建）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import (  # noqa: E402
    Account,
    AssetCard,
    InventoryItem,
    Period,
    Subject,
    Voucher,
    VoucherLine,
)
from kernel.posting import post_voucher  # noqa: E402
from kernel.reporting.costing import (  # noqa: E402
    CostingError,
    cost_allocation_draft,
    cost_settlement_draft,
)
from kernel.reporting.fixed_asset import (  # noqa: E402
    AssetError,
    asset_dispose_draft,
    depreciation_schedule,
)
from kernel.reporting.inventory import (  # noqa: E402
    InventoryError,
    inventory_valuation_draft,
    stockcard,
)
from kernel.reporting.statements import (  # noqa: E402
    balance_sheet,
    ending_balance,
)
from kernel.seed import seed_demo_ledger  # noqa: E402
from kernel.state import transition  # noqa: E402
from kernel.voucher_wizard import create_draft_voucher  # noqa: E402

ZERO = Decimal("0.00")


# ====================================================================== 内核夹具


@pytest.fixture()
def sess():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def env(sess):
    ids = seed_demo_ledger(sess)
    import_chart_of_accounts(sess, ids["ledger_set_id"], load_template_rows())
    for (y, m) in ((2026, 7), (2026, 8)):
        if sess.scalars(select(Period).where(
            Period.ledger_set_id == ids["ledger_set_id"],
            Period.year == y, Period.month == m,
        )).first() is None:
            sess.add(Period(ledger_set_id=ids["ledger_set_id"], year=y, month=m,
                            status="OPEN"))
    sess.flush()
    return ids


def _ensure_period(sess, env, y, m):
    p = sess.scalars(select(Period).where(
        Period.ledger_set_id == env["ledger_set_id"],
        Period.year == y, Period.month == m,
    )).first()
    if p is None:
        p = Period(ledger_set_id=env["ledger_set_id"], year=y, month=m, status="OPEN")
        sess.add(p)
        sess.flush()
    return p


def _post(sess, env, lines, voucher_date="2026-08-05"):
    _ensure_period(sess, env, int(voucher_date[:4]), int(voucher_date[5:7]))
    v, _ = create_draft_voucher(
        sess, ledger_set_id=env["ledger_set_id"], actor={"id": "u1"},
        voucher_date=voucher_date, summary="P0-1 测试凭证", lines=lines,
    )
    transition(sess, voucher_id=v.id, actor={"id": "u1"}, target="PUSHED")
    transition(sess, voucher_id=v.id, actor={"id": "u2"}, target="APPROVED")
    post_voucher(sess, voucher_id=v.id, actor={"id": "u2"})
    sess.commit()
    return v


def _make_item(sess, env, code="A1", name="测试货品", unit="件",
               method="weighted_avg", account="1405"):
    it = InventoryItem(
        ledger_set_id=env["ledger_set_id"], code=code, name=name, unit=unit,
        valuation_method=method, default_account_code=account,
    )
    sess.add(it)
    sess.flush()
    return it


def _make_card(sess, env, asset_no="FA01", name="机器设备",
               category="160101", ov="120000", salvage="0.1",
               life=60, start="2026-01-01"):
    c = AssetCard(
        ledger_set_id=env["ledger_set_id"], asset_no=asset_no, name=name,
        category_code=category, original_value=Decimal(ov),
        salvage_rate=Decimal(salvage), useful_life_months=life,
        start_date=date.fromisoformat(start), status="active",
    )
    sess.add(c)
    sess.flush()
    return c


# ====================================================================== 收发存台账


def test_stockcard_rebuilds_from_posted_lines(sess, env):
    _make_item(sess, env)
    # 期初：7 月入库 10 件 @100
    _post(sess, env, [
        {"account_code": "1405", "debit": "1000", "credit": "",
         "quantity": "10", "unit": "件", "aux_dims": {"inventory_item": "A1"}},
        {"account_code": "1002", "debit": "", "credit": "1000"},
    ], voucher_date="2026-07-05")
    # 本期：8 月入库 20 件 @100，发出 9 件 @100
    _post(sess, env, [
        {"account_code": "1405", "debit": "2000", "credit": "",
         "quantity": "20", "unit": "件", "aux_dims": {"inventory_item": "A1"}},
        {"account_code": "1002", "debit": "", "credit": "2000"},
    ], voucher_date="2026-08-03")
    _post(sess, env, [
        {"account_code": "6401", "debit": "900", "credit": ""},
        {"account_code": "1405", "debit": "", "credit": "900",
         "quantity": "9", "unit": "件", "aux_dims": {"inventory_item": "A1"}},
    ], voucher_date="2026-08-20")

    sc = stockcard(sess, env["ledger_set_id"], "A1", 2026, 8)
    assert sc["beginning"]["qty"] == Decimal("10")
    assert sc["beginning"]["amount"] == Decimal("1000.00")
    assert sc["receipts"]["qty"] == Decimal("20")
    assert sc["receipts"]["amount"] == Decimal("2000.00")
    assert sc["issues"]["qty"] == Decimal("9")
    assert sc["issues"]["amount"] == Decimal("900.00")
    assert sc["ending"]["qty"] == Decimal("21")
    assert sc["ending"]["amount"] == Decimal("2100.00")
    assert sc["transactions"] == 3


# ====================================================================== 计价


def _seed_valuation(sess, env):
    _make_item(sess, env)
    _post(sess, env, [
        {"account_code": "1405", "debit": "1000", "credit": "",
         "quantity": "10", "unit": "件", "aux_dims": {"inventory_item": "A1"}},
        {"account_code": "1002", "debit": "", "credit": "1000"},
    ], voucher_date="2026-08-03")
    _post(sess, env, [
        {"account_code": "1405", "debit": "2000", "credit": "",
         "quantity": "20", "unit": "件", "aux_dims": {"inventory_item": "A1"}},
        {"account_code": "1002", "debit": "", "credit": "2000"},
    ], voucher_date="2026-08-10")
    _post(sess, env, [
        {"account_code": "6401", "debit": "900", "credit": ""},
        {"account_code": "1405", "debit": "", "credit": "900",
         "quantity": "9", "unit": "件", "aux_dims": {"inventory_item": "A1"}},
    ], voucher_date="2026-08-20")


def test_valuation_weighted_avg(sess, env):
    _seed_valuation(sess, env)
    r = inventory_valuation_draft(sess, env["ledger_set_id"], "A1", 2026, 8,
                                  method="weighted_avg")
    assert r["method"] == "weighted_avg"
    assert r["unit_cost"] == "100.00"
    assert r["ending_qty"] == "21.00"
    assert r["ending_value"] == "2100.00"
    assert r["cogs"] == "900.00"
    # 结转草稿：借 6401 / 贷库存商品
    purposes = {ln["purpose"] for ln in r["lines"]}
    assert "periodic_cogs" in purposes
    assert any(ln["account_code"] == "6401" for ln in r["lines"])


def test_valuation_fifo(sess, env):
    _seed_valuation(sess, env)
    r = inventory_valuation_draft(sess, env["ledger_set_id"], "A1", 2026, 8,
                                  method="fifo")
    assert r["unit_cost"] == "100.00"
    assert r["ending_value"] == "2100.00"
    assert r["cogs"] == "900.00"


def test_valuation_moving_avg(sess, env):
    _seed_valuation(sess, env)
    r = inventory_valuation_draft(sess, env["ledger_set_id"], "A1", 2026, 8,
                                  method="moving_avg")
    assert r["unit_cost"] == "100.00"
    assert r["ending_value"] == "2100.00"
    assert r["cogs"] == "900.00"


def test_valuation_physical_count_gain(sess, env):
    _seed_valuation(sess, env)
    r = inventory_valuation_draft(sess, env["ledger_set_id"], "A1", 2026, 8,
                                  method="weighted_avg", physical_count_qty=Decimal("25"))
    assert r["variance"] == "4.00"
    purposes = {ln["purpose"] for ln in r["lines"]}
    assert "physical_gain" in purposes
    assert any(ln["account_code"] == "1901" for ln in r["lines"])


def test_valuation_unknown_item_raises(sess, env):
    with pytest.raises(InventoryError):
        stockcard(sess, env["ledger_set_id"], "NOPE", 2026, 8)


def test_valuation_unsupported_method_raises(sess, env):
    _make_item(sess, env)
    with pytest.raises(InventoryError):
        inventory_valuation_draft(sess, env["ledger_set_id"], "A1", 2026, 8,
                                  method="lifo")


# ====================================================================== 固定资产


def test_depreciation_schedule_straight_line(sess, env):
    _make_card(sess, env, ov="120000", salvage="0.1", life=60)
    r = depreciation_schedule(sess, env["ledger_set_id"], 2026, 8)
    assert r["total_depreciation"] == "1800.00"
    assert len(r["details"]) == 1
    d = r["details"][0]
    assert d["monthly_depreciation"] == "1800.00"
    assert d["status"] == "accruing"
    # 折旧草稿：借费用(6602) / 贷1602；累计折旧行(1602)挂 asset_no（ADR-002 重建关键）
    assert any(ln["account_code"] == "6602" and ln["debit"] == "1800.00"
               for ln in r["lines"])
    assert any(ln["account_code"] == "1602" and ln.get("aux_dims", {}).get("asset_no") == "FA01"
               for ln in r["lines"])


def test_depreciation_schedule_not_started(sess, env):
    _make_card(sess, env, start="2026-12-01")
    r = depreciation_schedule(sess, env["ledger_set_id"], 2026, 8)
    assert r["total_depreciation"] == "0.00"
    assert r["details"][0]["status"] == "not_started"


def test_asset_dispose_draft_reads_posted_1602(sess, env):
    _make_card(sess, env, ov="120000", salvage="0.1", life=60)
    # 先计提一期折旧（累计折旧真源），落库后经凭证明细重建。
    # 折旧费用行(6602)不挂 asset_no；仅累计折旧行(1602)挂 asset_no（ADR-002 关键链接）。
    _post(sess, env, [
        {"account_code": "6602", "debit": "1800", "credit": ""},
        {"account_code": "1602", "debit": "", "credit": "1800",
         "aux_dims": {"asset_no": "FA01"}},
    ], voucher_date="2026-08-05")
    r = asset_dispose_draft(sess, env["ledger_set_id"], "FA01", "2026-08-25",
                            proceeds=Decimal("5000"))
    assert r["original_value"] == "120000.00"
    assert r["accumulated_depreciation"] == "1800.00"
    assert r["net_book_value"] == "118200.00"
    assert r["proceeds"] == "5000.00"
    # 清理账户余额 118200-5000=113200（贷方）→ 损失结转 6711
    assert any(ln["account_code"] == "6711" and ln["debit"] == "" and ln["credit"] == "113200.00"
               for ln in r["lines"])
    # 转入清理：借 1606 = 账面净值，贷 1601 = 原值
    assert any(ln["account_code"] == "1606" and ln["debit"] == "118200.00" for ln in r["lines"])
    assert any(ln["account_code"] == "160101" and ln["credit"] == "120000.00" for ln in r["lines"])


def test_asset_dispose_unknown_raises(sess, env):
    with pytest.raises(AssetError):
        asset_dispose_draft(sess, env["ledger_set_id"], "FAX", "2026-08-25")


# ====================================================================== 成本核算


def test_cost_allocation_draft(sess, env):
    # 制造费用 5101 1000（无成本对象）+ 直接材料 500101（P1:5000 / P2:3000）
    _post(sess, env, [
        {"account_code": "5101", "debit": "1000", "credit": ""},
        {"account_code": "1002", "debit": "", "credit": "1000"},
    ], voucher_date="2026-08-10")
    _post(sess, env, [
        {"account_code": "500101", "debit": "5000", "credit": "",
         "aux_dims": {"project": "P1"}},
        {"account_code": "1002", "debit": "", "credit": "5000"},
    ], voucher_date="2026-08-12")
    _post(sess, env, [
        {"account_code": "500101", "debit": "3000", "credit": "",
         "aux_dims": {"project": "P2"}},
        {"account_code": "1002", "debit": "", "credit": "3000"},
    ], voucher_date="2026-08-12")
    r = cost_allocation_draft(sess, env["ledger_set_id"], 2026, 8,
                              base="direct_material")
    assert r["total_overhead"] == "1000.00"
    alloc = {a["cost_object"]: Decimal(a["allocated"]) for a in r["allocations"]}
    assert alloc["project:P1"] == Decimal("625.00")   # 1000 * 5000/8000
    assert alloc["project:P2"] == Decimal("375.00")   # 1000 * 3000/8000
    assert any(ln["account_code"] == "5101" and ln["credit"] == "1000.00"
               for ln in r["lines"])


def test_cost_allocation_unsupported_base_raises(sess, env):
    with pytest.raises(CostingError):
        cost_allocation_draft(sess, env["ledger_set_id"], 2026, 8, base="weird")


def test_cost_settlement_draft(sess, env):
    # P1 本期投入：直接材料 8000 + 直接人工 2000
    _post(sess, env, [
        {"account_code": "500101", "debit": "8000", "credit": "",
         "aux_dims": {"project": "P1"}},
        {"account_code": "500102", "debit": "2000", "credit": "",
         "aux_dims": {"project": "P1"}},
        {"account_code": "1002", "debit": "", "credit": "10000"},
    ], voucher_date="2026-08-15")
    r = cost_settlement_draft(sess, env["ledger_set_id"], 2026, 8, ending_wip=ZERO)
    assert r["total_completed"] == "10000.00"
    # 完工入库：借1405 / 贷5001，带成本对象
    assert any(ln["account_code"] == "1405" and ln["debit"] == "10000.00"
               and ln.get("aux_dims", {}).get("project") == "P1" for ln in r["lines"])
    assert any(ln["account_code"] == "5001" and ln["credit"] == "10000.00"
               and ln.get("aux_dims", {}).get("project") == "P1" for ln in r["lines"])


# ====================================================================== 红线：只读不变量


def test_readonly_invariant(sess, env):
    _make_card(sess, env)  # 资产卡片（折旧/处置草稿用）
    _seed_valuation(sess, env)  # 内含 _make_item，避免重复编码导致唯一约束冲突
    before = sess.scalars(select(VoucherLine)).all()
    n_before = len(before)
    # 依次调用全部只读草稿算子，均不得改账
    inventory_valuation_draft(sess, env["ledger_set_id"], "A1", 2026, 8, method="weighted_avg")
    depreciation_schedule(sess, env["ledger_set_id"], 2026, 8)
    asset_dispose_draft(sess, env["ledger_set_id"], "FA01", "2026-08-25")
    cost_allocation_draft(sess, env["ledger_set_id"], 2026, 8)
    cost_settlement_draft(sess, env["ledger_set_id"], 2026, 8)
    after = sess.scalars(select(VoucherLine)).all()
    assert len(after) == n_before, "草稿算子不得新增任何凭证明细行"


# ====================================================================== MCP 工具层


@pytest.fixture()
def mcp_env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/p0_1_mcp.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.flush()
        ids["reviewer_subject_id"] = reviewer.id
        s.commit()
        from kernel.authz import grant_ledger_role

        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=ids["subject_id"], role="admin")
        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=reviewer.id, role="reviewer")
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def mcp_server(mcp_env):
    from xerp_mcp.server import build_server

    return build_server(mcp_env["url"])


def _call(server, tool, **args):
    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(tool, args)
            if getattr(res, "data", None) is not None:
                return res.data
            import json

            return json.loads(res.content[0].text)

    return asyncio.run(inner())


def _mcp_post(server, env, lines, voucher_date="2026-08-05"):
    made = _call(
        server, "create_voucher",
        ledger_set_id=env["ids"]["ledger_set_id"], voucher_date=voucher_date,
        summary="P0-1 MCP 凭证", actor_id=env["ids"]["subject_id"],
        idempotency_key=None, lines=lines,
    )
    assert made["ok"], made
    vid = made["voucher"]["id"]
    _call(server, "push_voucher", voucher_id=vid, actor_id=env["ids"]["subject_id"])
    _call(server, "approve_voucher", voucher_id=vid, actor_id=env["ids"]["reviewer_subject_id"])
    _call(server, "post_voucher", voucher_id=vid, actor_id=env["ids"]["subject_id"])
    return vid


def test_inventory_item_register_crud(mcp_env, mcp_server):
    r = _call(mcp_server, "inventory_item_register",
              ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="create",
              code="M1", name="MCP货品", unit="箱", valuation_method="fifo")
    assert r["ok"] and r["action"] == "create"
    assert r["item"]["method"] == "fifo"

    g = _call(mcp_server, "inventory_item_register",
              ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="get", code="M1")
    assert g["ok"] and g["item"]["name"] == "MCP货品"

    lst = _call(mcp_server, "inventory_item_register",
                ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="list")
    assert lst["ok"] and any(it["code"] == "M1" for it in lst["items"])


def test_asset_register_crud(mcp_env, mcp_server):
    r = _call(mcp_server, "asset_register",
              ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="create",
              asset_no="FA-M1", name="MCP设备", category_code="160103",
              original_value="60000", salvage_rate="0.05",
              useful_life_months=48, start_date="2026-02-01")
    assert r["ok"] and r["action"] == "create"
    assert r["card"]["original_value"] == "60000"
    g = _call(mcp_server, "asset_register",
              ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="get",
              asset_no="FA-M1")
    assert g["ok"] and g["card"]["status"] == "active"


def test_inventory_stockcard_tool_end_to_end(mcp_env, mcp_server):
    # 主数据 + 经工具落库收发凭证，验证单一真源端到端重建
    _call(mcp_server, "inventory_item_register",
          ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="create",
          code="E2E", name="端到端货品", unit="件", valuation_method="weighted_avg")
    _mcp_post(mcp_server, mcp_env, [
        {"account_code": "1405", "debit": "1000", "credit": "",
         "quantity": "10", "unit": "件", "aux_dims": {"inventory_item": "E2E"}},
        {"account_code": "1002", "debit": "", "credit": "1000"},
    ], voucher_date="2026-08-03")
    _mcp_post(mcp_server, mcp_env, [
        {"account_code": "6401", "debit": "400", "credit": ""},
        {"account_code": "1405", "debit": "", "credit": "400",
         "quantity": "4", "unit": "件", "aux_dims": {"inventory_item": "E2E"}},
    ], voucher_date="2026-08-20")
    r = _call(mcp_server, "inventory_stockcard",
              ledger_set_id=mcp_env["ids"]["ledger_set_id"], item_code="E2E",
              period_year=2026, period_month=8)
    assert r["ok"]
    # stockcard 返回原始 Decimal（经 MCP 序列化为字符串），用 Decimal 比较避免精度格式差异
    assert Decimal(str(r["ending"]["qty"])) == 6
    assert Decimal(str(r["ending"]["amount"])) == 600
    # 计价草稿工具同样可产出结转 lines
    v = _call(mcp_server, "inventory_valuation_draft",
              ledger_set_id=mcp_env["ids"]["ledger_set_id"], item_code="E2E",
              period_year=2026, period_month=8, method="weighted_avg")
    assert v["ok"] and v["cogs"] == "400.00"


def test_depreciation_and_cost_tools_wiring(mcp_env, mcp_server):
    _call(mcp_server, "asset_register",
          ledger_set_id=mcp_env["ids"]["ledger_set_id"], action="create",
          asset_no="FA-W", name="接线设备", category_code="160103",
          original_value="120000", salvage_rate="0", useful_life_months=60,
          start_date="2026-01-01")
    dep = _call(mcp_server, "depreciation_schedule_draft",
                ledger_set_id=mcp_env["ids"]["ledger_set_id"],
                period_year=2026, period_month=8)
    assert dep["ok"] and dep["total_depreciation"] == "2000.00"

    alloc = _call(mcp_server, "cost_allocation_draft",
                  ledger_set_id=mcp_env["ids"]["ledger_set_id"],
                  period_year=2026, period_month=8, base="direct_material")
    assert alloc["ok"] and alloc["total_overhead"] == "0.00"

    settle = _call(mcp_server, "cost_settlement_draft",
                   ledger_set_id=mcp_env["ids"]["ledger_set_id"],
                   period_year=2026, period_month=8, ending_wip="0")
    assert settle["ok"] and settle["total_completed"] == "0.00"

    dispose = _call(mcp_server, "asset_dispose_draft",
                    ledger_set_id=mcp_env["ids"]["ledger_set_id"],
                    asset_no="FA-W", dispose_date="2026-08-25", proceeds="0")
    assert dispose["ok"] and dispose["net_book_value"] == "120000.00"


# ====================================================================== 资产负债表含在产品（WIP）回归
#
# P0-1 成本核算暴露的缺陷：生产成本(5001)/制造费用(5101) 期末未结转余额=在产品，
# 属存货类流动资产；此前映射漏将其列示、且 ending_balance 把 5 前缀按贷方为正取数，
# 导致资产负债表存货项出现负值、试算不平衡。此处钉死正确口径。


def test_ending_balance_cost_account_debit_normal():
    # 成本类(5 前缀)与资产/费用同为借方为正
    assert ending_balance("500101", Decimal("8000"), ZERO) == Decimal("8000.00")
    assert ending_balance("5101", Decimal("1000"), ZERO) == Decimal("1000.00")
    # 贷方余额（异常红冲场景）应为负
    assert ending_balance("500101", ZERO, Decimal("500")) == Decimal("-500.00")
    # 收入(6001)仍为贷方为正
    assert ending_balance("6001", Decimal("100"), Decimal("300")) == Decimal("200.00")


def test_balance_sheet_includes_wip_and_balances(sess, env):
    # 注资 + 成本投入（在产品），验证 5001/5101 作为存货类流动资产以正数列示、试算平衡。
    _post(sess, env, [
        {"account_code": "1002", "debit": "50000", "credit": ""},
        {"account_code": "4001", "debit": "", "credit": "50000"},
    ], voucher_date="2026-08-01")
    _post(sess, env, [
        {"account_code": "500101", "debit": "8000", "credit": "",
         "aux_dims": {"project": "P1"}},
        {"account_code": "500102", "debit": "2000", "credit": "",
         "aux_dims": {"project": "P1"}},
        {"account_code": "5101", "debit": "1000", "credit": ""},
        {"account_code": "1002", "debit": "", "credit": "11000"},
    ], voucher_date="2026-08-15")
    bs = balance_sheet(sess, env["ledger_set_id"], 2026, 8)
    assert bs["balanced"] is True, bs["check"]
    # 在产品（生产成本/制造费用）须列示在资产侧、且为正（借方余额）
    wip_accounts = {
        a["code"]: a["ending"]
        for it in bs["assets"]["items"] for a in it["accounts"]
    }
    for c in ("500101", "500102", "5101"):
        assert c in wip_accounts, f"{c} 未列示在资产负债表资产侧"
        assert wip_accounts[c] > 0, f"{c} 期末余额应为正（借方），实为 {wip_accounts[c]}"
    assert wip_accounts["500101"] == Decimal("8000.00")
    assert wip_accounts["500102"] == Decimal("2000.00")
    assert wip_accounts["5101"] == Decimal("1000.00")
