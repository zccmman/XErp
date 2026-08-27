"""期初余额导入（P0-10 Drill 向导内核侧）。

设计：期初以特殊凭证（凭证号 期初-NNNN，直接 POSTED）入账——
与其他凭证同走事件账本与余额投影，不引入第二套余额体系；
新账套无历史数据，行业惯例允许创建人直接导入，不经审批链。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Period, Voucher, VoucherLine, utcnow
from kernel.ledger import append_event
from kernel.posting import PostingError, _accumulate_balances

ZERO = Decimal("0")


def _amount(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value if value not in (None, "") else "0")).quantize(
            Decimal("0.01")
        )
    except Exception:
        raise PostingError("AMOUNT_INVALID", f"{field} 不是合法金额: {value!r}") from None
    return d


def _pick_period(
    session: Session, ledger_set_id: str, year: int | None, month: int | None
) -> Period:
    q = select(Period).where(Period.ledger_set_id == ledger_set_id)
    if year and month:
        period = session.scalars(
            q.where(Period.year == year, Period.month == month)
        ).first()
    else:
        period = session.scalars(
            q.where(Period.status == "OPEN").order_by(Period.year.desc(), Period.month.desc())
        ).first()
    if period is None:
        raise PostingError("PERIOD_NOT_FOUND", "没有可用的 OPEN 期间，请先初始化期间")
    return period


def import_opening_balances(
    session: Session,
    *,
    ledger_set_id: str,
    actor: dict,
    lines: list[dict],
    period_year: int | None = None,
    period_month: int | None = None,
) -> Voucher:
    """导入期初余额：生成直接过账的「期初-NNNN」凭证并累计余额投影。

    试算不平衡（TRIAL_BALANCE_UNBALANCED）时整体拒绝，不落任何数据。
    """
    period = _pick_period(session, ledger_set_id, period_year, period_month)
    if period.status != "OPEN":
        raise PostingError(
            "PERIOD_NOT_OPEN",
            f"期间 {period.year}-{period.month:02d} 状态为 {period.status}，不可导入期初",
        )
    accounts = {
        a.code: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }

    parsed: list[tuple[Account, Decimal, Decimal, dict]] = []
    total_debit = ZERO
    total_credit = ZERO
    for i, ln in enumerate(lines or [], start=1):
        code = (ln.get("account_code") or "").strip()
        acc = accounts.get(code)
        if acc is None:
            raise PostingError("ACCOUNT_NOT_FOUND", f"第 {i} 行科目不存在: {code!r}")
        dr = _amount(ln.get("debit"), f"第{i}行借方")
        cr = _amount(ln.get("credit"), f"第{i}行贷方")
        if dr > ZERO and cr > ZERO:
            raise PostingError("LINE_BOTH_SIDES", f"第 {i} 行借贷双方同时有金额")
        if dr == ZERO and cr == ZERO:
            raise PostingError("AMOUNT_INVALID", f"第 {i} 行借贷金额均为零")
        dims = ln.get("aux_dims") or {}
        parsed.append((acc, dr, cr, dims))
        total_debit += dr
        total_credit += cr

    if total_debit != total_credit:
        raise PostingError(
            "TRIAL_BALANCE_UNBALANCED",
            f"期初试算不平衡：借 {total_debit} ≠ 贷 {total_credit}",
            {"total_debit": str(total_debit), "total_credit": str(total_credit)},
        )

    seq = (
        len(
            session.scalars(
                select(Voucher.id).where(
                    Voucher.ledger_set_id == ledger_set_id,
                    Voucher.voucher_no.like("期初-%"),
                )
            ).all()
        )
        + 1
    )
    voucher = Voucher(
        ledger_set_id=ledger_set_id,
        period_id=period.id,
        voucher_no=f"期初-{seq:04d}",
        voucher_date=utcnow().date(),
        status="POSTED",
        summary="期初余额导入",
        created_by=str(actor.get("id") or ""),
        posted_at=utcnow(),
        lines=[
            VoucherLine(
                line_no=i + 1,
                account_id=acc.id,
                debit=dr,
                credit=cr,
                aux_dims=dims or None,
            )
            for i, (acc, dr, cr, dims) in enumerate(parsed)
        ],
    )
    session.add(voucher)
    session.flush()

    from kernel.posting import PostingLine

    append_event(
        session,
        ledger_set_id=ledger_set_id,
        event_type="opening_balance.imported",
        aggregate_id=voucher.id,
        payload={
            "voucher_no": voucher.voucher_no,
            "lines": [
                {
                    "account_code": acc.code,
                    "debit": str(dr),
                    "credit": str(cr),
                    "aux_dims": dims,
                }
                for acc, dr, cr, dims in parsed
            ],
        },
        actor=actor,
    )
    _accumulate_balances(
        session,
        voucher=voucher,
        lines=[
            PostingLine(account_id=acc.id, debit=dr, credit=cr, aux_dims=dims)
            for acc, dr, cr, dims in parsed
        ],
    )
    session.flush()
    return voucher
