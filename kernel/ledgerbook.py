"""明细账（复盘 D2）：按科目逐笔流水 + 滚动余额 + 凭证联查。

基准总账里这是会计日常使用频率最高的账簿：选定科目与期间，
逐笔列出该科目的分录（日期/凭证号/摘要/借/贷），并给出逐行滚动余额。

口径：
- 只取 POSTED 凭证（明细账是法定账簿，在途凭证不进）；
- 「方向」取科目档案的余额方向（debit: 借方余额为正；credit 反之）；
- 期初余额 = 同账套该科目自开账至期初的全部 POSTED 分录净额累计
  （事件可重放，无需依赖投影）；
- 逐行余额 = 期初余额 ± 当行借贷。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, LedgerSet, Period, Voucher, VoucherLine
from kernel.coa import attr_is
from kernel.opening import is_opening_voucher

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


class LedgerBookError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _fmt(x: Decimal) -> str:
    return f"{x.quantize(CENT):,.2f}"


def _fmtq(x: Decimal) -> str:
    """数量格式化（3 位小数）。"""
    return f"{x.quantize(Decimal('0.001')):,.3f}"


def _fmt_rate(x) -> str:
    """汇率格式化：去尾零，最多 6 位小数。"""
    if x is None:
        return ""
    d = Decimal(str(x)).quantize(Decimal("0.000001"))
    s = f"{d:,.6f}".rstrip("0").rstrip(".")
    return s


def ledger_detail(
    session: Session,
    *,
    ledger_set_id: str,
    account_code: str,
    year: int,
    month: int,
) -> dict[str, Any]:
    """科目明细账：期初余额 + 逐笔分录（滚动余额）+ 期末合计。"""
    account = session.scalars(
        select(Account).where(
            Account.ledger_set_id == ledger_set_id, Account.code == account_code
        )
    ).first()
    if account is None:
        raise LedgerBookError(
            "ACCOUNT_NOT_FOUND",
            f"账套缺少科目 {account_code}",
            {"account_code": account_code},
        )
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year, Period.month == month,
        )
    ).first()
    if period is None:
        raise LedgerBookError(
            "PERIOD_NOT_FOUND",
            f"账套不存在 {year}-{month:02d} 期间",
        )
    ledger_set = session.get(LedgerSet, ledger_set_id)
    func_ccy = (ledger_set.functional_currency if ledger_set else "CNY")

    # ② 数量核算：科目声明 quantity=yes 时，明细账以「数量金额式」呈现。
    qty_enabled = attr_is(account.attrs, "quantity")

    # 该科目全部 POSTED 分录，按（凭证日期, 凭证号, 行号）排序
    rows = session.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status == "POSTED",
            VoucherLine.account_id == account.id,
        )
        .order_by(Voucher.voucher_date, Voucher.voucher_no, VoucherLine.line_no)
    ).all()

    is_debit_dir = account.direction == "debit"

    def signed(dr: Decimal, cr: Decimal) -> Decimal:
        return dr - cr if is_debit_dir else cr - dr

    def signed_qty(ln: VoucherLine) -> Decimal:
        """数量随金额方向记：借方行数量为正、贷方行为负（库存商品等资产科目）。"""
        q = ln.quantity or ZERO
        return q if ln.debit > ZERO else -q

    # 期初余额 = 期间开始前的全部净额 + 期初导入凭证（建账基线，非业务发生）
    opening = ZERO
    opening_qty = ZERO
    in_period: list[dict] = []
    for ln, v in rows:
        entry = {
            "voucher_id": v.id,
            "voucher_no": v.voucher_no,
            "date": v.voucher_date.isoformat(),
            "summary": v.summary or "",
            "debit": Decimal(str(ln.debit)),
            "credit": Decimal(str(ln.credit)),
        }
        if qty_enabled and ln.quantity is not None:
            entry["quantity"] = Decimal(str(ln.quantity))
            entry["unit"] = ln.unit or ""
            entry["side"] = "借" if ln.debit > ZERO else "贷"
        if ln.currency and ln.currency != func_ccy:
            entry["currency"] = ln.currency
            entry["fx_rate"] = _fmt_rate(ln.fx_rate) if ln.fx_rate is not None else ""
            entry["foreign_debit"] = Decimal(str(ln.foreign_debit))
            entry["foreign_credit"] = Decimal(str(ln.foreign_credit))
        if is_opening_voucher(v.voucher_no):
            opening += signed(entry["debit"], entry["credit"])
            if qty_enabled and ln.quantity is not None:
                opening_qty += signed_qty(ln)
        elif (v.voucher_date.year, v.voucher_date.month) < (year, month):
            opening += signed(entry["debit"], entry["credit"])
            if qty_enabled and ln.quantity is not None:
                opening_qty += signed_qty(ln)
        elif (v.voucher_date.year, v.voucher_date.month) == (year, month):
            in_period.append(entry)

    # 逐行滚动余额
    running = opening
    running_qty = opening_qty
    detail_rows = []
    total_debit = total_credit = ZERO
    for e in in_period:
        running += signed(e["debit"], e["credit"])
        total_debit += e["debit"]
        total_credit += e["credit"]
        row = {
            "voucher_no": e["voucher_no"],
            "date": e["date"],
            "summary": e["summary"],
            "debit": _fmt(e["debit"]),
            "credit": _fmt(e["credit"]),
            "balance": _fmt(running),
            # 方向标注按科目余额方向：借方科目正余额=借，贷方科目正余额=贷
            "direction": ("借" if running >= ZERO else "贷")
            if is_debit_dir else ("贷" if running >= ZERO else "借"),
        }
        if qty_enabled and "quantity" in e:
            running_qty += signed_qty_from_entry(e, is_debit_dir)
            row["quantity"] = _fmtq(e["quantity"]) if e.get("side") == "借" else ""
            row["quantity_credit"] = _fmtq(e["quantity"]) if e.get("side") == "贷" else ""
            row["unit"] = e["unit"]
            row["balance_quantity"] = _fmtq(abs(running_qty)) + (e["unit"] or "")
        if "currency" in e:
            row["currency"] = e["currency"]
            row["fx_rate"] = e["fx_rate"]
            row["foreign_debit"] = _fmt(e["foreign_debit"])
            row["foreign_credit"] = _fmt(e["foreign_credit"])
        detail_rows.append(row)

    result = {
        "account": {"code": account.code, "name": account.name,
                    "direction": account.direction},
        "period": {"year": year, "month": month},
        "functional_currency": func_ccy,
        "quantity_enabled": qty_enabled,
        "opening_balance": _fmt(opening),
        "opening_quantity": _fmtq(opening_qty) if qty_enabled else None,
        "rows": detail_rows,
        "totals": {"debit": _fmt(total_debit), "credit": _fmt(total_credit)},
        "closing_balance": _fmt(running),
        "closing_direction": ("借" if running >= ZERO else "贷")
        if is_debit_dir else ("贷" if running >= ZERO else "借"),
        "basis": "仅 POSTED 凭证；明细账为法定账簿口径",
    }
    if qty_enabled:
        result["closing_quantity"] = _fmtq(running_qty)
    return result


def signed_qty_from_entry(e: dict, is_debit_dir: bool) -> Decimal:
    """从已构造的 entry 还原数量的方向符号（与 signed_qty 一致）。"""
    q = e.get("quantity") or ZERO
    return q if e.get("side") == "借" else -q

