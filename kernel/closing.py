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

from kernel.db.models import Account, Balance, Period, Voucher, VoucherLine, utcnow
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


def _collect_pl_rows(
    session: Session, *, ledger_set_id: str, period: Period, standard: str
) -> list[tuple[Account, Decimal, Decimal, str]]:
    """汇总损益类科目本期发生额（单一真源）。

    预览（preview_closing）与执行（close_period）**必须共用这一份取数**——
    否则「预览看到的净利润」和「实际结转的净利润」会漂移，那比没有预览更糟：
    用户会因为信任预览而点下去。
    """
    from kernel.db.models import Balance

    mp = M.get_mapping(standard)
    accounts = {a.code: a for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ).all()}
    acc_by_id = {a.id: a for a in accounts.values()}
    rows: list[tuple[Account, Decimal, Decimal, str]] = []
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
        rows.append((acc, dr, cr, side))
    return rows


def _build_closing_lines(
    pl_rows: list[tuple[Account, Decimal, Decimal, str]], profit_account_id: int
) -> tuple[list[tuple[int, int, Decimal, Decimal]], Decimal]:
    """把损益发生额配成结转分录（单一真源，预览与执行共用）。

    收入类（side=credit）借方结出、费用类贷方结出，差额进本年利润。
    返回 (分录行 [(line_no, account_id, debit, credit)], 净利润 贷-借)。
    """
    lines: list[tuple[int, int, Decimal, Decimal]] = []
    net = ZERO
    line_no = 1
    for acc, dr, cr, side in sorted(pl_rows, key=lambda x: x[0].code):
        if side == "credit":          # 收入类：借方结出
            amount = cr - dr
            if amount <= ZERO:
                continue
            lines.append((line_no, acc.id, amount, ZERO))
            net += amount
        else:                          # 费用/成本类：贷方结出
            amount = dr - cr
            if amount <= ZERO:
                continue
            lines.append((line_no, acc.id, ZERO, amount))
            net -= amount
        line_no += 1
    if net > ZERO:
        lines.append((line_no, profit_account_id, ZERO, net))
    elif net < ZERO:
        lines.append((line_no, profit_account_id, -net, ZERO))
    # net == 0（盈亏平衡）也要落一张结转凭证，保证期间状态可追溯
    return lines, net


def preview_closing(
    session: Session, *, ledger_set_id: str, year: int, month: int,
    standard: str = "small_business",
) -> dict:
    """期末结转预览（只读）：把"点下去会发生什么"在结账前摊开给人看。

    老会计最在意的从来不是「结账」这个动作，而是**结之前能不能先看清楚**：
    哪些损益科目会被结走、各自金额多少、净利润进哪个科目、会生成什么凭证号。

    只读、不写、不抛账务错误；与 close_period 共用取数与配平逻辑，
    因此预览结果 = 真执行结果（除并发写入外）。

    返回：
        {year, month, period_status, already_closed, closing_voucher_no,
         profit_account, will_generate, lines[], total_income, total_expense,
         net_profit, nothing_to_close, hint}
    """
    mp = M.get_mapping(standard)
    profit_code = mp["closing"]["profit_account"]
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise PostingError("PERIOD_NOT_FOUND", f"期间 {year}-{month:02d} 不存在")

    prefix = f"结转-{year}{month:02d}-"
    existing_no = session.scalars(
        select(Voucher.voucher_no).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(prefix + "%"),
        )
    ).first()

    profit_acc = session.scalars(
        select(Account).where(
            Account.ledger_set_id == ledger_set_id, Account.code == profit_code
        )
    ).first()
    if profit_acc is None:
        raise PostingError("ACCOUNT_NOT_FOUND", f"结转目标科目 {profit_code} 不存在")

    accounts = {a.id: a for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ).all()}

    if existing_no is not None:
        # 已结转：必须**从结转凭证本身回放**，不能拿余额重算——
        # 结转后损益科目投影行已净零清理，重算出来的净利润恒为 0，
        # 那会让"这是历史回放"这句文案变成彻头彻尾的谎言。
        done = session.scalars(
            select(Voucher).where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.voucher_no == existing_no,
            )
        ).first()
        lines = (
            [(ln.line_no, ln.account_id, Decimal(str(ln.debit)),
              Decimal(str(ln.credit))) for ln in done.lines]
            if done is not None else []
        )
        net = sum(
            (cr - dr) for _no, acc_id, dr, cr in lines if acc_id == profit_acc.id
        ) or ZERO
        nothing = False
    else:
        pl_rows = _collect_pl_rows(
            session, ledger_set_id=ledger_set_id, period=period, standard=standard
        )
        lines, net = _build_closing_lines(pl_rows, profit_acc.id)
        nothing = not pl_rows

    acc_by_id = dict(accounts)
    acc_by_id[profit_acc.id] = profit_acc

    out_lines = []
    total_income = ZERO
    total_expense = ZERO
    for ln_no, acc_id, dr, cr in lines:
        acc = acc_by_id.get(acc_id)
        if acc is None:
            continue
        is_profit = acc_id == profit_acc.id
        amount = cr if dr == ZERO else dr
        direction = "贷" if dr == ZERO else "借"
        out_lines.append({
            "line_no": ln_no,
            "account_code": acc.code,
            "account_name": acc.name,
            "side": "profit" if is_profit else (
                "income" if cr == ZERO else "expense"),
            "amount": str(amount),
            "direction": direction,   # 该行记在哪一方（结转的对方方）
        })
        if not is_profit:
            if cr == ZERO:      # 收入类：借方结出
                total_income += amount
            else:               # 费用类：贷方结出
                total_expense += amount

    if existing_no is not None:
        hint = (f"{year}-{month:02d} 已执行过期末结转（{existing_no}），"
                "不可重复执行。下面为已结转内容的历史回放。")
    elif nothing:
        hint = "本期无损益类科目发生额，无需结转（点结账也不会生成凭证）。"
    else:
        hint = (
            f"执行后将生成凭证 {prefix}001：{len(out_lines) - 1} 个损益科目结出，"
            f"净利润 {net} 结转至 {profit_code} 本年利润。"
        )

    return {
        "year": year,
        "month": month,
        "period_status": period.status,
        "already_closed": existing_no is not None,
        "closing_voucher_no": existing_no,
        "profit_account": profit_code,
        "will_generate": None if (existing_no or nothing) else f"{prefix}001",
        "lines": out_lines,
        "total_income": str(total_income),
        "total_expense": str(total_expense),
        "net_profit": str(net),
        "nothing_to_close": nothing,
        "hint": hint,
    }


def close_period(session: Session, *, ledger_set_id: str, year: int, month: int,
                 actor: dict, standard: str = "small_business") -> Voucher:
    """执行期末结转并锁期：损益类科目余额 → 本年利润（3103），期间置 CLOSED。

    结转即结账：生成 POSTED 结转凭证后把期间状态置为 CLOSED（precheck_close
    闸门1 依赖上月 = CLOSED 才能连续闭合）。返回结转凭证。
    幂等：同期间已有结转凭证 → ALREADY_CLOSED；已锁期 → PERIOD_NOT_OPEN。
    """
    mp = M.get_mapping(standard)
    profit_code = mp["closing"]["profit_account"]
    period = _pick_period(session, ledger_set_id, year, month)
    prefix = f"结转-{year}{month:02d}-"
    exists = session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(prefix + "%"),
        )
    ).first()
    if exists is not None:
        # 历史兼容：修复前 close_period 只生成结转凭证、从不锁期，导致已结转
        # 期间仍 OPEN。此处补锁（幂等），保证「有结转凭证即 CLOSED」单一事实，
        # precheck_close 闸门1 才能连续闭合。仍抛 ALREADY_CLOSED 保留契约。
        period.status = "CLOSED"
        session.flush()
        raise PostingError(
            "ALREADY_CLOSED",
            f"{year}-{month:02d} 已执行期末结转（{prefix}…），不可重复",
        )
    if period.status != "OPEN":
        raise PostingError(
            "PERIOD_NOT_OPEN",
            f"期间 {year}-{month:02d} 状态为 {period.status}，仅未结账期间可结转",
        )

    accounts = {a.code: a for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ).all()}
    if profit_code not in accounts:
        raise PostingError("ACCOUNT_NOT_FOUND", f"结转目标科目 {profit_code} 不存在")
    profit_acc = accounts[profit_code]
    acc_by_id = {a.id: a for a in accounts.values()}

    # 取数与配平均走单一真源 helper，与 preview_closing 完全一致
    pl_rows = _collect_pl_rows(
        session, ledger_set_id=ledger_set_id, period=period, standard=standard
    )
    if not pl_rows:
        raise PostingError("NOTHING_TO_CLOSE", "本期无损益类科目发生额，无需结转")

    raw_lines, net = _build_closing_lines(pl_rows, profit_acc.id)
    closing_date = date(year, month, calendar.monthrange(year, month)[1])
    lines = [
        VoucherLine(line_no=ln_no, account_id=acc_id, debit=dr, credit=cr)
        for ln_no, acc_id, dr, cr in raw_lines
    ]

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

    # 锁期：结转完成即结账，期间置 CLOSED（单一真源；precheck_close 闸门1
    # 依赖上月 = CLOSED 才能连续闭合，否则期间链在次月断裂）。
    period.status = "CLOSED"
    session.flush()
    return voucher
