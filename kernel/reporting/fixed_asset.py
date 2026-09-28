"""P0-1 固定资产：直线法折旧 + 处置（纯函数、只读）。

设计铁律（ADR-002 单一真源）：
- 折旧额是只读算子；折旧真源 = 1602 累计折旧凭证明细（卡片 accumulated_depreciation
  仅作展示，不在此处作为余额真源）。
- 处置草稿同样只读，落库经既有 create_voucher HITL。

链接约定：折旧/处置凭证行在 aux_dims 带 {"asset_no": "<资产编号>"}，便于后续由
凭证明细重建累计折旧与清理状态。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, AssetCard, Voucher, VoucherLine

ZERO = Decimal("0")


class AssetError(RuntimeError):
    pass


def _fmt(d: Decimal) -> str:
    return f"{(d or ZERO):.2f}"


def _get_card(session: Session, ledger_set_id: str, asset_no: str) -> AssetCard:
    card = session.scalars(
        select(AssetCard).where(
            AssetCard.ledger_set_id == ledger_set_id,
            AssetCard.asset_no == asset_no,
        )
    ).first()
    if card is None:
        raise AssetError(f"资产卡片 {asset_no} 在账套 {ledger_set_id} 下不存在")
    return card


def _accumulated_depreciation(session: Session, ledger_set_id: str, asset_no: str) -> Decimal:
    """由 1602 累计折旧 POSTED 凭证明细重建累计折旧（单一真源）。

    aux_dims["asset_no"] == 资产编号 的行：贷方累加 − 借方(处置冲回)。
    """
    rows = session.execute(
        select(Voucher, VoucherLine)
        .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
        .join(Account, Account.id == VoucherLine.account_id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status == "POSTED",
            Account.code == "1602",
        )
    ).all()
    total = ZERO
    for v, ln in rows:
        aux = ln.aux_dims or {}
        if aux.get("asset_no") != asset_no:
            continue
        total += (ln.credit - ln.debit)
    return total


def _months_elapsed(start: date, year: int, month: int) -> int:
    return (year - start.year) * 12 + (month - start.month)


def depreciation_schedule(
    session: Session, ledger_set_id: str, year: int, month: int
) -> dict:
    """各 active 卡片当月直线法折旧额 + 折旧凭证草稿 lines（只读）。

    月折旧 = original_value × (1 − salvage_rate) / useful_life_months。
    条件：start_date 所在月 ≤ 目标月，且已提月数 < 使用年限（提满不再计提）。
    """
    cards = session.scalars(
        select(AssetCard).where(
            AssetCard.ledger_set_id == ledger_set_id,
            AssetCard.status == "active",
        )
    ).all()
    details: list[dict] = []
    lines: list[dict] = []
    total_depr = ZERO
    for c in cards:
        elapsed = _months_elapsed(c.start_date, year, month)
        if elapsed < 0:
            status = "not_started"
            monthly = ZERO
        elif elapsed >= c.useful_life_months:
            status = "fully_depreciated"
            monthly = ZERO
        else:
            status = "accruing"
            monthly = (
                c.original_value * (Decimal(1) - c.salvage_rate)
            ) / Decimal(c.useful_life_months)
        if monthly <= 0:
            details.append({
                "asset_no": c.asset_no, "name": c.name,
                "monthly_depreciation": _fmt(ZERO), "status": status,
            })
            continue
        total_depr += monthly
        exp = (c.aux_dims or {}).get("expense_account") or "6602"  # 默认管理费用
        # 每卡片一对：借费用(6602) / 贷累计折旧(1602) 并挂 asset_no；
        # 累计折旧行(1602)挂 asset_no 才是一致重建累计折旧(ADR-002)的关键链接，
        # 费用行不挂（6602 仅声明 department），避免污染费用科目维度。
        lines.append({
            "account_code": exp, "debit": _fmt(monthly), "credit": "",
            "summary": f"{c.name} 折旧",
        })
        lines.append({
            "account_code": "1602", "debit": "", "credit": _fmt(monthly),
            "summary": f"{c.name} 计提折旧（累计折旧）",
            "aux_dims": {"asset_no": c.asset_no},
        })
        details.append({
            "asset_no": c.asset_no, "name": c.name,
            "monthly_depreciation": _fmt(monthly), "status": status,
            "expense_account": exp,
        })
    return {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "total_depreciation": _fmt(total_depr),
        "details": details,
        "lines": lines,
        "summary": (
            f"{year}-{month:02d} 应计提折旧合计 {_fmt(total_depr)}，"
            f"涉及 {len(details)} 张卡片"
        ),
    }


def asset_dispose_draft(
    session: Session,
    ledger_set_id: str,
    asset_no: str,
    dispose_date: str,
    proceeds: Decimal = ZERO,
    proceed_account: str = "1002",
) -> dict:
    """资产处置凭证草稿（只读）：转入清理 → 收款 → 处置损益。

    累计折旧由 1602 凭证明细重建（单一真源）。处置损益：小企业准则下
    收益走 6301 营业外收入、损失走 6711 营业外支出。
    """
    c = _get_card(session, ledger_set_id, asset_no)
    accum = _accumulated_depreciation(session, ledger_set_id, asset_no)
    net_book = c.original_value - accum  # 账面价值
    lines: list[dict] = []

    # ① 转入清理
    lines.append({
        "account_code": "1606", "debit": _fmt(net_book), "credit": "",
        "summary": f"{c.name} 转入清理", "aux_dims": {"asset_no": asset_no},
    })
    if accum > 0:
        lines.append({
            "account_code": "1602", "debit": _fmt(accum), "credit": "",
            "summary": f"{c.name} 累计折旧转出", "aux_dims": {"asset_no": asset_no},
        })
    lines.append({
        "account_code": c.category_code or "160101", "debit": "",
        "credit": _fmt(c.original_value),
        "summary": f"{c.name} 固定资产转出", "aux_dims": {"asset_no": asset_no},
    })

    # ② 处置收款
    if proceeds > 0:
        lines.append({
            "account_code": proceed_account, "debit": _fmt(proceeds), "credit": "",
            "summary": f"收到 {c.name} 处置款", "aux_dims": {"asset_no": asset_no},
        })
        lines.append({
            "account_code": "1606", "debit": "", "credit": _fmt(proceeds),
            "summary": f"{c.name} 清理收款", "aux_dims": {"asset_no": asset_no},
        })

    # ③ 处置损益：1606 余额（net_book − proceeds）结转到营业外收支
    cleanup_balance = net_book - proceeds
    if cleanup_balance > 0:  # 清理账户余额在贷方 → 损失
        lines.append({
            "account_code": "1606", "debit": _fmt(cleanup_balance), "credit": "",
            "summary": f"{c.name} 处置损失结转", "aux_dims": {"asset_no": asset_no},
        })
        lines.append({
            "account_code": "6711", "debit": "", "credit": _fmt(cleanup_balance),
            "summary": "营业外支出-处置损失", "aux_dims": {"asset_no": asset_no},
        })
    elif cleanup_balance < 0:  # 清理账户借方余额 → 收益
        gain = -cleanup_balance
        lines.append({
            "account_code": "1606", "debit": "", "credit": _fmt(gain),
            "summary": f"{c.name} 处置收益结转", "aux_dims": {"asset_no": asset_no},
        })
        lines.append({
            "account_code": "6301", "debit": _fmt(gain), "credit": "",
            "summary": "营业外收入-处置收益", "aux_dims": {"asset_no": asset_no},
        })

    return {
        "asset_no": asset_no,
        "name": c.name,
        "original_value": _fmt(c.original_value),
        "accumulated_depreciation": _fmt(accum),
        "net_book_value": _fmt(net_book),
        "proceeds": _fmt(proceeds),
        "dispose_date": dispose_date,
        "lines": lines,
        "summary": (
            f"{c.name} 处置：原值 {_fmt(c.original_value)}，累计折旧 {_fmt(accum)}，"
            f"账面净值 {_fmt(net_book)}，处置收款 {_fmt(proceeds)}"
        ),
    }
