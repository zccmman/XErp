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

from kernel.db.models import Account, Period, Voucher, VoucherLine

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

    # 期初余额 = 期间开始前的全部净额
    opening = ZERO
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
        if (v.voucher_date.year, v.voucher_date.month) < (year, month):
            opening += signed(entry["debit"], entry["credit"])
        elif (v.voucher_date.year, v.voucher_date.month) == (year, month):
            in_period.append(entry)

    # 逐行滚动余额
    running = opening
    detail_rows = []
    total_debit = total_credit = ZERO
    for e in in_period:
        running += signed(e["debit"], e["credit"])
        total_debit += e["debit"]
        total_credit += e["credit"]
        detail_rows.append({
            "voucher_no": e["voucher_no"],
            "date": e["date"],
            "summary": e["summary"],
            "debit": _fmt(e["debit"]),
            "credit": _fmt(e["credit"]),
            "balance": _fmt(running),
            # 方向标注按科目余额方向：借方科目正余额=借，贷方科目正余额=贷
            "direction": ("借" if running >= ZERO else "贷")
            if is_debit_dir else ("贷" if running >= ZERO else "借"),
        })

    return {
        "account": {"code": account.code, "name": account.name,
                    "direction": account.direction},
        "period": {"year": year, "month": month},
        "opening_balance": _fmt(opening),
        "rows": detail_rows,
        "totals": {"debit": _fmt(total_debit), "credit": _fmt(total_credit)},
        "closing_balance": _fmt(running),
        "closing_direction": ("借" if running >= ZERO else "贷")
        if is_debit_dir else ("贷" if running >= ZERO else "借"),
        "basis": "仅 POSTED 凭证；明细账为法定账簿口径",
    }
