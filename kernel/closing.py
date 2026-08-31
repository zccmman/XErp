"""期末结转引擎（P1-02）：损益类科目余额按映射规则结转至本年利润。

- 规则声明式：由 reporting.mapping 的 income_statement 配置推导（收入贷方结出、
  费用借方结出），结转目标科目 closing.profit_account（默认 3103 本年利润）
- 结转凭证：voucher_no = 结转-YYYYMM-NNN，直接 POSTED（与期初导入同口径，
  系统规则执行，不经审批链），追加 closing.executed 事件（append-only）
- 幂等：同期间已有结转凭证 → ALREADY_CLOSED
- 投影同步：损益科目余额清零（零行删除），3103 累计净利润
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Period, Voucher, VoucherLine, utcnow
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import PostingError, PostingLine, _accumulate_balances
from kernel.reporting import mapping as M

ZERO = Decimal("0")


def _pick_period(session: Session, ledger_set_id: str, year: int, month: int) -> Period:
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise PostingError("PERIOD_NOT_FOUND", f"期间 {year}-{month:02d} 不存在")
    return period


def close_period(session: Session, *, ledger_set_id: str, year: int, month: int,
                 actor: dict, standard: str = "small_business") -> Voucher:
    """执行期末结转：损益类科目余额 → 本年利润（3103）。

    返回结转凭证（POSTED）。重复执行抛 ALREADY_CLOSED。
    """
    mp = M.get_mapping(standard)
    profit_code = mp["closing"]["profit_account"]
    period = _pick_period(session, ledger_set_id, year, month)
    if period.status != "OPEN":
        raise PostingError(
            "PERIOD_NOT_OPEN",
            f"期间 {year}-{month:02d} 状态为 {period.status}，仅未结账期间可结转",
        )
    prefix = f"结转-{year}{month:02d}-"
    exists = session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(prefix + "%"),
        )
    ).first()
    if exists is not None:
        raise PostingError(
            "ALREADY_CLOSED",
            f"{year}-{month:02d} 已执行期末结转（{prefix}…），不可重复",
        )

    accounts = {a.code: a for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ).all()}
    if profit_code not in accounts:
        raise PostingError("ACCOUNT_NOT_FOUND", f"结转目标科目 {profit_code} 不存在")
    profit_acc = accounts[profit_code]

    # 汇总损益类科目本期发生额（balances 投影）
    from kernel.db.models import Balance

    acc_by_id = {a.id: a for a in accounts.values()}
    pl_rows: list[tuple[Account, Decimal, Decimal, str]] = []  # (acc, dr, cr, side)
    for b in session.scalars(select(Balance).where(Balance.period_id == period.id)):
        acc = acc_by_id.get(b.account_id)
        if acc is None:
            continue
        hit = M.income_statement_item(mp, acc.code)
        if hit is None:
            continue
        _name, side = hit
        dr = Decimal(str(b.debit_total))
        cr = Decimal(str(b.credit_total))
        if dr == ZERO and cr == ZERO:
            continue
        pl_rows.append((acc, dr, cr, side))

    if not pl_rows:
        raise PostingError("NOTHING_TO_CLOSE", "本期无损益类科目发生额，无需结转")

    # 生成结转分录：收入类借方结出，费用类贷方结出，差额进本年利润
    lines: list[VoucherLine] = []
    line_no = 1
    net = ZERO  # 贷-借，正数为本期盈利
    closing_date = date(year, month, calendar.monthrange(year, month)[1])
    for acc, dr, cr, side in sorted(pl_rows, key=lambda x: x[0].code):
        if side == "credit":          # 收入类：借方结出
            amount = cr - dr
            if amount <= ZERO:
                continue
            lines.append(VoucherLine(line_no=line_no, account_id=acc.id,
                                     debit=amount, credit=ZERO))
            net += amount
        else:                          # 费用/成本类：贷方结出
            amount = dr - cr
            if amount <= ZERO:
                continue
            lines.append(VoucherLine(line_no=line_no, account_id=acc.id,
                                     debit=ZERO, credit=amount))
            net -= amount
        line_no += 1

    if net > ZERO:
        lines.append(VoucherLine(line_no=line_no, account_id=profit_acc.id,
                                 debit=ZERO, credit=net))
    elif net < ZERO:
        lines.append(VoucherLine(line_no=line_no, account_id=profit_acc.id,
                                 debit=-net, credit=ZERO))
    # net == 0（盈亏平衡）也要落一张结转凭证，保证期间状态可追溯

    seq = len(session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(prefix + "%"),
        )
    ).all()) + 1
    voucher = Voucher(
        ledger_set_id=ledger_set_id,
        period_id=period.id,
        voucher_no=f"{prefix}{seq:03d}",
        voucher_date=closing_date,
        status="POSTED",
        summary=f"期末结转 {year}-{month:02d}",
        created_by=str(actor.get("id") or ""),
        posted_at=utcnow(),
        lines=lines,
    )
    session.add(voucher)
    session.flush()

    append_event(
        session,
        ledger_set_id=ledger_set_id,
        event_type=E.CLOSING_EXECUTED,
        aggregate_id=voucher.id,
        payload={
            "period": f"{year}-{month:02d}",
            "net_profit": str(net),
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

    # 损益类科目投影行净零清理：结转后期间内借=贷，行删除保持投影紧凑

    mp2 = M.get_mapping(standard)
    for b in session.scalars(select(Balance).where(Balance.period_id == period.id)):
        acc = acc_by_id.get(b.account_id)
        if acc is None or M.income_statement_item(mp2, acc.code) is None:
            continue
        if b.debit_total == b.credit_total:
            session.delete(b)

    session.flush()
    return voucher
