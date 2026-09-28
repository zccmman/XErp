"""P0-1 演示账套预置（幂等，可重复运行）。

向「演示账套」补：
- 存货档案（inventory_items）+ 固定资产卡片（asset_cards）主数据；
- 少量 POSTED 收发 / 折旧 / 成本投入凭证（带 aux_dims + quantity），
  供 inventory/fixed_asset/costing 只读算子即时演示单一真源重建。

铁律：全部幂等——主数据按唯一键跳过；凭证按 idempotency_key 复用；
期间/科目表按既有 upsert 语义补 attrs。重复运行不重复落数据、不改既有账。

用法：
    python scripts/seed_p0_1_demo.py [sqlite:///path/to/ledgeros_dev.db]
默认库：仓库根目录下的 ledgeros_dev.db（与 MCP 运行库一致）。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.models import Account, AssetCard, InventoryItem, LedgerSet, Period, Subject, Voucher  # noqa: E402
from kernel.posting import post_voucher  # noqa: E402
from kernel.state import transition  # noqa: E402
from kernel.voucher_wizard import create_draft_voucher  # noqa: E402

DEFAULT_URL = "sqlite:///" + str(ROOT / "ledgeros_dev.db")

# 演示主体：丞辰（制单）+ 审批人（审批，禁止自审）
MAKER_ID = "b42455e7e5b8476ba30df0b1be8f504f"
APPROVER_ID = "d5f05e2d26e34088a2c9b09fd2ad3b88"


def _get_or_create_ledger_set(s: Session) -> LedgerSet:
    ls = s.scalars(select(LedgerSet).where(LedgerSet.name == "演示账套")).first()
    if ls is None:
        ls = LedgerSet(name="演示账套", accounting_standard="small_business")
        s.add(ls)
        s.flush()
    return ls


def _ensure_period(s: Session, ls: LedgerSet, y: int, m: int) -> Period:
    p = s.scalars(select(Period).where(
        Period.ledger_set_id == ls.id, Period.year == y, Period.month == m
    )).first()
    if p is None:
        p = Period(ledger_set_id=ls.id, year=y, month=m, status="OPEN")
        s.add(p)
        s.flush()
    return p


def _ensure_subjects(s: Session) -> None:
    for sid, name in ((MAKER_ID, "丞辰"), (APPROVER_ID, "审批人")):
        if s.get(Subject, sid) is None:
            s.add(Subject(id=sid, type="user", display_name=name, autonomy_level=3))
    s.flush()


def _ensure_item(s: Session, ls: LedgerSet, code: str, name: str, unit: str,
                 method: str = "weighted_avg", account: str = "1405") -> InventoryItem:
    it = s.scalars(select(InventoryItem).where(
        InventoryItem.ledger_set_id == ls.id, InventoryItem.code == code
    )).first()
    if it is None:
        it = InventoryItem(
            ledger_set_id=ls.id, code=code, name=name, unit=unit,
            valuation_method=method, default_account_code=account,
        )
        s.add(it)
        s.flush()
    return it


def _ensure_card(s: Session, ls: LedgerSet, asset_no: str, name: str,
                 category: str, ov: str, salvage: str, life: int, start: str) -> AssetCard:
    c = s.scalars(select(AssetCard).where(
        AssetCard.ledger_set_id == ls.id, AssetCard.asset_no == asset_no
    )).first()
    if c is None:
        c = AssetCard(
            ledger_set_id=ls.id, asset_no=asset_no, name=name, category_code=category,
            original_value=Decimal(ov), salvage_rate=Decimal(salvage),
            useful_life_months=life, start_date=date.fromisoformat(start),
            status="active",
        )
        s.add(c)
        s.flush()
    return c


def _post_idempotent(s: Session, ls: LedgerSet, *, key: str, vdate: str,
                     summary: str, lines: list[dict]) -> str:
    existing = s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ls.id, Voucher.idempotency_key == key
    )).first()
    if existing is not None:
        if existing.status == "POSTED":
            return f"SKIP(posted) {existing.voucher_no}"
        v = existing
    else:
        v, _ = create_draft_voucher(
            s, ledger_set_id=ls.id, actor={"id": MAKER_ID}, voucher_date=vdate,
            summary=summary, lines=lines, idempotency_key=key,
        )
    if v.status == "DRAFT":
        transition(s, voucher_id=v.id, actor={"id": MAKER_ID}, target="PUSHED")
    if v.status == "PUSHED":
        transition(s, voucher_id=v.id, actor={"id": APPROVER_ID}, target="APPROVED")
    if v.status == "APPROVED":
        post_voucher(s, voucher_id=v.id, actor={"id": MAKER_ID})
    s.commit()
    return f"POSTED {v.voucher_no}"


def main(url: str) -> None:
    engine = create_engine(url)
    with Session(engine) as s:
        # 1) 账套 + 期间 + 主体 + 科目表 attrs 补齐（idempotent upsert）
        ls = _get_or_create_ledger_set(s)
        _ensure_period(s, ls, 2026, 8)
        _ensure_subjects(s)
        import_chart_of_accounts(s, ls.id, load_template_rows())
        s.commit()

        # 2) 主数据：存货 + 固定资产（按唯一键幂等）
        _ensure_item(s, ls, "IT-01", "机架服务器", "台", method="weighted_avg", account="1405")
        _ensure_item(s, ls, "IT-02", "显示器", "台", method="weighted_avg", account="1405")
        _ensure_card(s, ls, "FA-01", "数据中心机柜", "160101", "120000", "0.1", 60, "2026-01-01")
        _ensure_card(s, ls, "FA-02", "行政公务车", "160102", "80000", "0.05", 48, "2026-02-01")
        s.commit()

        # 3) POSTED 凭证（按 idempotency_key 幂等）
        log = []

        # 采购入库：IT-01 两批均 @5000 → 加权单位成本 5000
        log.append(_post_idempotent(s, ls, key="p0-1-seed-receipt-it01", vdate="2026-08-05",
            summary="采购入库 机架服务器 10台@5000", lines=[
                {"account_code": "1405", "debit": "50000", "credit": "", "quantity": "10",
                 "unit": "台", "aux_dims": {"inventory_item": "IT-01"}},
                {"account_code": "1002", "debit": "", "credit": "50000"},
            ]))
        log.append(_post_idempotent(s, ls, key="p0-1-seed-receipt-it01b", vdate="2026-08-12",
            summary="采购入库 机架服务器 5台@5000", lines=[
                {"account_code": "1405", "debit": "25000", "credit": "", "quantity": "5",
                 "unit": "台", "aux_dims": {"inventory_item": "IT-01"}},
                {"account_code": "1002", "debit": "", "credit": "25000"},
            ]))
        log.append(_post_idempotent(s, ls, key="p0-1-seed-receipt-it02", vdate="2026-08-08",
            summary="采购入库 显示器 20台@800", lines=[
                {"account_code": "1405", "debit": "16000", "credit": "", "quantity": "20",
                 "unit": "台", "aux_dims": {"inventory_item": "IT-02"}},
                {"account_code": "1002", "debit": "", "credit": "16000"},
            ]))

        # 销售发出：IT-01 4台@5000 / IT-02 5台@800
        log.append(_post_idempotent(s, ls, key="p0-1-seed-issue-it01", vdate="2026-08-20",
            summary="销售发出 机架服务器 4台（加权成本5000）", lines=[
                {"account_code": "6401", "debit": "20000", "credit": ""},
                {"account_code": "1405", "debit": "", "credit": "20000", "quantity": "4",
                 "unit": "台", "aux_dims": {"inventory_item": "IT-01"}},
            ]))
        log.append(_post_idempotent(s, ls, key="p0-1-seed-issue-it02", vdate="2026-08-18",
            summary="销售发出 显示器 5台（加权成本800）", lines=[
                {"account_code": "6401", "debit": "4000", "credit": ""},
                {"account_code": "1405", "debit": "", "credit": "4000", "quantity": "5",
                 "unit": "台", "aux_dims": {"inventory_item": "IT-02"}},
            ]))

        # 计提折旧（单一真源）：FA-01 1800 / FA-02 1583.33，1602 行挂 asset_no
        log.append(_post_idempotent(s, ls, key="p0-1-seed-depr-202608", vdate="2026-08-31",
            summary="计提折旧 2026-08（直线法）", lines=[
                {"account_code": "6602", "debit": "1800", "credit": ""},
                {"account_code": "1602", "debit": "", "credit": "1800",
                 "aux_dims": {"asset_no": "FA-01"}},
                {"account_code": "6602", "debit": "1583.33", "credit": ""},
                {"account_code": "1602", "debit": "", "credit": "1583.33",
                 "aux_dims": {"asset_no": "FA-02"}},
            ]))

        # 成本投入：直接材料 8000 + 直接人工 2000（项目 P1）+ 制造费用 1000
        log.append(_post_idempotent(s, ls, key="p0-1-seed-cost-input", vdate="2026-08-15",
            summary="生产成本投入（项目 P1）+ 制造费用", lines=[
                {"account_code": "500101", "debit": "8000", "credit": "",
                 "aux_dims": {"project": "P1"}},
                {"account_code": "500102", "debit": "2000", "credit": "",
                 "aux_dims": {"project": "P1"}},
                {"account_code": "5101", "debit": "1000", "credit": ""},
                {"account_code": "1002", "debit": "", "credit": "11000"},
            ]))

    print("P0-1 演示预置完成：")
    for line in log:
        print("  -", line)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    main(url)
