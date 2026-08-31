"""对账引擎（P1-04）：账账核对 —— 投影与凭证明细的一致性校验。

原则：余额是投影、不是事实（ADR-002）。投影必须能被凭证明细完全重算出来，
对不上就是缺陷或篡改，必须能被检出。

检查项：
1. 逐凭证借贷平衡（POSTED 凭证）
2. 投影 vs 凭证明细重算（逐科目借/贷发生额）
3. 试算平衡（全部科目借方余额合计 = 贷方余额合计）
4. 现金及等价物流水勾稽（现金流净增加 = 现金科目期末-期初）
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, Period, Voucher, VoucherLine
from kernel.reporting import mapping as M

ZERO = Decimal("0")


class ReconcileError(RuntimeError):
    pass


def _period(session: Session, ledger_set_id: str, year: int, month: int) -> Period:
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise ReconcileError(f"期间 {year}-{month:02d} 不存在")
    return period


def _direction(code: str) -> str:
    """余额方向：资产/成本/费用类借方为正。"""
    if code.startswith(("6001", "6051", "6301")):
        return "贷"
    return "借" if code.startswith(("1", "6")) else "贷"


def reconcile_ledger(
    session: Session,
    ledger_set_id: str,
    year: int,
    month: int,
    standard: str = "small_business",
) -> dict:
    """执行账账核对，返回 {ok, issues, checks, summary}。"""
    mp = M.get_mapping(standard)
    period = _period(session, ledger_set_id, year, month)
    accounts = {a.id: a for a in session.scalars(select(Account)).all()}
    issues: list[dict] = []

    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
        )
    ).all()

    # 1) 逐凭证借贷平衡
    for v in vouchers:
        lines = session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ).all()
        dr = sum((Decimal(str(ln.debit)) for ln in lines), ZERO)
        cr = sum((Decimal(str(ln.credit)) for ln in lines), ZERO)
        if dr != cr:
            issues.append({
                "kind": "VOUCHER_UNBALANCED",
                "voucher_no": v.voucher_no,
                "debit": dr,
                "credit": cr,
            })

    # 2) 投影 vs 凭证明细重算
    recomputed: dict[str, tuple[Decimal, Decimal]] = {}
    for v in vouchers:
        for ln in session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ):
            code = accounts[ln.account_id].code
            d, c = recomputed.get(code, (ZERO, ZERO))
            recomputed[code] = (
                d + Decimal(str(ln.debit)),
                c + Decimal(str(ln.credit)),
            )

    projected: dict[str, tuple[Decimal, Decimal]] = {}
    for b in session.scalars(select(Balance).where(Balance.period_id == period.id)):
        acc = accounts.get(b.account_id)
        if acc is None:
            issues.append({"kind": "ORPHAN_BALANCE_ROW", "balance_id": b.id})
            continue
        d, c = projected.get(acc.code, (ZERO, ZERO))
        projected[acc.code] = (
            d + Decimal(str(b.debit_total)),
            c + Decimal(str(b.credit_total)),
        )

    for code in sorted(set(recomputed) | set(projected)):
        rd, rc = recomputed.get(code, (ZERO, ZERO))
        pd_, pc = projected.get(code, (ZERO, ZERO))
        if (rd, rc) != (pd_, pc):
            issues.append({
                "kind": "PROJECTION_MISMATCH",
                "account": code,
                "from_vouchers": [rd, rc],
                "projection": [pd_, pc],
            })

    # 3) 试算平衡
    total_dr_balance = ZERO
    total_cr_balance = ZERO
    for code, (d, c) in projected.items():
        net = d - c
        if _direction(code) == "借":
            total_dr_balance += net
        else:
            total_cr_balance += -net  # 贷方余额 = -(借-贷)
    if total_dr_balance != total_cr_balance:
        issues.append({
            "kind": "TRIAL_BALANCE_UNBALANCED",
            "debit_balance": total_dr_balance,
            "credit_balance": total_cr_balance,
        })

    # 4) 现金流勾稽（现金及等价物）
    from kernel.reporting.statements import cash_flow

    cf = cash_flow(session, ledger_set_id, year, month, standard)
    cash_codes = [c for c in projected if M.is_cash_account(mp, c)]
    cash_begin = ZERO
    for v in vouchers:
        if v.voucher_no.startswith("期初-"):
            for ln in session.scalars(
                select(VoucherLine).where(VoucherLine.voucher_id == v.id)
            ):
                code = accounts[ln.account_id].code
                if M.is_cash_account(mp, code):
                    cash_begin += Decimal(str(ln.debit)) - Decimal(str(ln.credit))
    cash_end = sum(
        (projected[c][0] - projected[c][1] if _direction(c) == "借"
         else projected[c][1] - projected[c][0])
        for c in cash_codes
    )
    if cash_begin + cf["net_increase"] != cash_end:
        issues.append({
            "kind": "CASH_FLOW_MISMATCH",
            "opening_cash": cash_begin,
            "net_increase": cf["net_increase"],
            "closing_cash": cash_end,
        })

    return {
        "ledger_set": ledger_set_id,
        "period": {"year": year, "month": month},
        "ok": not issues,
        "issues": issues,
        "checks": {
            "vouchers": len(vouchers),
            "accounts_with_balance": len(projected),
            "trial_balance": [total_dr_balance, total_cr_balance],
            "cash": [cash_begin, cf["net_increase"], cash_end],
        },
    }
