"""外币试算平衡（②）：按（科目 × 币种）汇总本月 POSTED 凭证的本币与原币发生额。

数据源：POSTED 凭证的 VoucherLine（与 cash_flow 同一直读口径），不读余额投影
（投影不含原币字段）。只统计带 currency 的明细行——本币科目不混入，
因此本表天然就是「外币户/外汇交易」的专项试算。

口径铁律：
- 本币借/贷 = ln.debit / ln.credit（账面权威值，已含汇率折算后的本币金额）
- 原币借/贷 = ln.foreign_debit / ln.foreign_credit
- 同一科目理论上可跨多币种，按（科目, 币种）分组展示
- 纯增量、零内核状态机改动；可被账账核对独立验证
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, LedgerSet, Period, Voucher, VoucherLine
from kernel.reporting.statements import ReportError

ZERO = Decimal("0.00")


def foreign_trial_balance(
    session: Session, *, ledger_set_id: str, year: int, month: int
) -> dict:
    """按（科目 × 币种）汇总本月 POSTED 凭证明细的本币与原币借/贷。

    仅含带币种（currency 非空）的明细行。返回 rows（按科目编码排序）+ totals。
    """
    ls = session.get(LedgerSet, ledger_set_id)
    if ls is None:
        raise ReportError(f"账套 {ledger_set_id} 不存在")
    func = ls.functional_currency or "CNY"

    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise ReportError(f"期间 {year}-{month:02d} 不存在")

    accounts = {
        a.id: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
        )
    ).all()

    # (科目编码, 科目名, 币种) -> 发生额
    agg: dict[tuple[str, str, str], dict] = {}
    for v in vouchers:
        for ln in session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ).all():
            if not ln.currency:
                continue
            acc = accounts.get(ln.account_id)
            if acc is None:
                continue
            key = (acc.code, acc.name, ln.currency)
            bucket = agg.setdefault(
                key,
                {
                    "debit": ZERO,
                    "credit": ZERO,
                    "foreign_debit": ZERO,
                    "foreign_credit": ZERO,
                },
            )
            bucket["debit"] += Decimal(str(ln.debit))
            bucket["credit"] += Decimal(str(ln.credit))
            bucket["foreign_debit"] += Decimal(str(ln.foreign_debit))
            bucket["foreign_credit"] += Decimal(str(ln.foreign_credit))

    rows = [
        {
            "account_code": code,
            "account_name": name,
            "currency": ccy,
            "debit": str(b["debit"]),
            "credit": str(b["credit"]),
            "foreign_debit": str(b["foreign_debit"]),
            "foreign_credit": str(b["foreign_credit"]),
        }
        for (code, name, ccy), b in sorted(agg.items())
    ]
    total_debit = sum((Decimal(r["debit"]) for r in rows), ZERO)
    total_credit = sum((Decimal(r["credit"]) for r in rows), ZERO)
    total_fdebit = sum((Decimal(r["foreign_debit"]) for r in rows), ZERO)
    total_fcredit = sum((Decimal(r["foreign_credit"]) for r in rows), ZERO)
    return {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "functional_currency": func,
        "rows": rows,
        "totals": {
            "debit": str(total_debit),
            "credit": str(total_credit),
            "foreign_debit": str(total_fdebit),
            "foreign_credit": str(total_fcredit),
        },
        "basis": "仅 POSTED 凭证中带币种的明细行；本币=ln.debit/credit，原币=ln.foreign_debit/credit",
    }
