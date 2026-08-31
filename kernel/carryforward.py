"""期初结转（P1-06）：把上一期间资产负债类科目期末余额滚入新期间的期初凭证。

与 kernel.closing 配套：close_period（期末结转）→ open_next_period（期初结转）。
放在独立模块是因为 Windows 下近期写过的源文件常被外部进程短暂锁定，
新文件可绕开锁冲突。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, Period, Voucher, VoucherLine, utcnow
from kernel.ledger import append_event
from kernel.posting import PostingError, PostingLine, _accumulate_balances
from kernel.reporting import mapping as M

ZERO = Decimal("0")


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def open_next_period(session: Session, *, ledger_set_id: str, year: int, month: int,
                     actor: dict, standard: str = "small_business") -> Voucher:
    """期初结转：资产负债类科目期末余额滚入下一期间的「期初-YYYYMM-NNN」凭证。

    前置：上一期间已执行期末结转（否则 PERIOD_NOT_CLOSED）。
    幂等：下一期间已有期初凭证 → ALREADY_OPENED。
    """
    mp = M.get_mapping(standard)
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise PostingError("PERIOD_NOT_FOUND", f"期间 {year}-{month:02d} 不存在")
    closed = session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(f"结转-{year}{month:02d}-%"),
        )
    ).first() is not None
    if not closed:
        raise PostingError(
            "PERIOD_NOT_CLOSED",
            f"{year}-{month:02d} 尚未执行期末结转，不能开新账期",
        )

    ny, nm = _next_month(year, month)
    next_prefix = f"期初-{ny}{nm:02d}-"
    if session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(next_prefix + "%"),
        )
    ).first() is not None:
        raise PostingError(
            "ALREADY_OPENED",
            f"{ny}-{nm:02d} 已有期初凭证，无需重复开账",
        )

    accounts = {a.code: a for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ).all()}
    acc_by_id = {a.id: a for a in accounts.values()}

    balances = session.scalars(
        select(Balance).where(Balance.period_id == period.id)
    ).all()
    lines: list[VoucherLine] = []
    line_no = 1
    for b in balances:
        acc = acc_by_id.get(b.account_id)
        if acc is None or M.balance_sheet_group(mp, acc.code) is None:
            continue
        net = Decimal(str(b.debit_total)) - Decimal(str(b.credit_total))
        if net == ZERO:
            continue
        if net > ZERO:      # 资产（借方余额）
            lines.append(VoucherLine(line_no=line_no, account_id=acc.id,
                                     debit=net, credit=ZERO))
        else:               # 负债/权益（贷方余额）
            lines.append(VoucherLine(line_no=line_no, account_id=acc.id,
                                     debit=ZERO, credit=-net))
        line_no += 1
    if not lines:
        raise PostingError("NOTHING_TO_CARRY", "上一期间无资产负债类余额，无需开账")

    nperiod = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id, Period.year == ny, Period.month == nm
        )
    ).first()
    if nperiod is None:
        nperiod = Period(ledger_set_id=ledger_set_id, year=ny, month=nm, status="OPEN")
        session.add(nperiod)
        session.flush()
    if nperiod.status != "OPEN":
        raise PostingError("PERIOD_NOT_OPEN", f"期间 {ny}-{nm:02d} 未处于 OPEN 状态")

    seq = len(session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(next_prefix + "%"),
        )
    ).all()) + 1
    voucher = Voucher(
        ledger_set_id=ledger_set_id,
        period_id=nperiod.id,
        voucher_no=f"{next_prefix}{seq:03d}",
        voucher_date=date(ny, nm, 1),
        status="POSTED",
        summary=f"期初结转（自 {year}-{month:02d}）",
        created_by=str(actor.get("id") or ""),
        posted_at=utcnow(),
        lines=lines,
    )
    session.add(voucher)
    session.flush()

    append_event(
        session,
        ledger_set_id=ledger_set_id,
        event_type="opening_balance.imported",
        aggregate_id=voucher.id,
        payload={
            "period": f"{ny}-{nm:02d}",
            "carry_from": f"{year}-{month:02d}",
            "lines": [
                {
                    "account_code": acc_by_id[ln.account_id].code,
                    "debit": str(ln.debit),
                    "credit": str(ln.credit),
                }
                for ln in lines
            ],
        },
        actor=actor,
    )
    _accumulate_balances(
        session,
        voucher=voucher,
        lines=[
            PostingLine(account_id=ln.account_id, debit=ln.debit, credit=ln.credit)
            for ln in lines
        ],
    )
    session.flush()
    return voucher
