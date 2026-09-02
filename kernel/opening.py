"""期初余额导入（P0-10 Drill 向导内核侧）。

设计：期初以特殊凭证（凭证号 期初-NNNN，直接 POSTED）入账——
与其他凭证同走事件账本与余额投影，不引入第二套余额体系；
新账套无历史数据，行业惯例允许创建人直接导入，不经审批链。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Event, Period, Voucher, VoucherLine, utcnow
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import PostingError, _accumulate_balances

ZERO = Decimal("0")

# ── 凭证号前缀约定（全项目唯一真源，禁止在别处硬编码字符串）──
OPENING_PREFIX = "期初-"      # 期初余额凭证（含期结转生成的 期初-YYYYMM-NNN）
REVERSAL_PREFIX = "冲销-"     # 期初红字冲销凭证（force 重导时生成）


def is_opening_voucher(voucher_no: str | None) -> bool:
    """是否属「期初口径」凭证 —— 期初与其红字冲销都**不是本期发生额**。

    期初是存量、不是本期经营成果；红字冲销是对期初的调整，同样不是。
    凡取「本期发生额」的地方（利润表、现金流量表本期流量、科目余额表
    的本期借贷栏）都必须用本函数剔除，否则建账当期报表会失真。
    """
    return (voucher_no or "").startswith((OPENING_PREFIX, REVERSAL_PREFIX))


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


def _existing_opening_vouchers(session: Session, ledger_set_id: str) -> list[Voucher]:
    """列出本账套全部期初凭证（凭证号以「期初-」开头，含已被冲销的）。"""
    return list(
        session.scalars(
            select(Voucher)
            .where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.voucher_no.like("期初-%"),
            )
            .order_by(Voucher.voucher_no)
        ).all()
    )


def _active_opening_vouchers(session: Session, ledger_set_id: str) -> list[Voucher]:
    """列出**生效中**的期初凭证 —— 即尚未被红字冲销的那些。

    必须区分「全部」与「生效中」：force 重导后旧凭证仍留在库里（append-only，
    审计链需要），若下次 force 不加区分地再次冲销，就会重复冲销导致余额被
    反向冲成负数。判据是是否存在 OPENING_BALANCE_REVERSED 事件。
    """
    reversed_ids = set(
        session.scalars(
            select(Event.aggregate_id).where(
                Event.ledger_set_id == ledger_set_id,
                Event.event_type == E.OPENING_BALANCE_REVERSED,
            )
        ).all()
    )
    return [v for v in _existing_opening_vouchers(session, ledger_set_id)
            if v.id not in reversed_ids]


def _reverse_opening(
    session: Session,
    *,
    voucher: Voucher,
    code_map: dict[str, str],
    actor: dict,
    reason: str,
) -> Voucher:
    """红字冲销一张期初凭证（补偿事务，append-only）——生成**真实冲销凭证**。

    修复前只调 _accumulate_balances 改投影、不落任何凭证明细，后果是致命的：
      - 违反 ADR-002「余额投影可由凭证流完全重建」→ 系统对账直接报
        PROJECTION_MISMATCH（实测 force 重导后必现）
      - 凭证明细里期初仍是 20 万 + 新期初 15 万 = 35 万，而投影是 15 万，
        「账上两个真相」，会计按凭证明细对账必然对不上
      - 冲销动作在凭证列表里完全不可见，审计人员看不到这笔调整

    现在：生成一张借贷互换、直接 POSTED 的「冲销-期初-NNNN」凭证，
    与期初凭证同口径（不经审批链），原凭证不删不改，审计链完整。
    """
    from kernel.posting import PostingLine

    swapped = [(ln.account_id, ln.credit, ln.debit, ln.aux_dims) for ln in voucher.lines]
    rev = Voucher(
        ledger_set_id=voucher.ledger_set_id,
        period_id=voucher.period_id,
        voucher_no=f"{REVERSAL_PREFIX}{voucher.voucher_no}",
        voucher_date=voucher.voucher_date,
        status="POSTED",
        summary=f"红字冲销 {voucher.voucher_no}（{reason}）",
        created_by=str(actor.get("id") or ""),
        posted_at=utcnow(),
        lines=[
            VoucherLine(
                line_no=i + 1,
                account_id=aid,
                debit=d,
                credit=c,
                aux_dims=dims,
            )
            for i, (aid, d, c, dims) in enumerate(swapped)
        ],
    )
    session.add(rev)
    session.flush()
    _accumulate_balances(
        session,
        voucher=rev,
        lines=[
            PostingLine(account_id=aid, debit=d, credit=c, aux_dims=dims)
            for aid, d, c, dims in swapped
        ],
    )
    append_event(
        session,
        ledger_set_id=voucher.ledger_set_id,
        event_type=E.OPENING_BALANCE_REVERSED,
        aggregate_id=voucher.id,
        payload={
            "voucher_no": voucher.voucher_no,
            "reversal_voucher_no": rev.voucher_no,
            "reason": reason,
            "lines": [
                {
                    "account_code": code_map.get(ln.account_id, "?"),
                    "debit": str(ln.credit),
                    "credit": str(ln.debit),
                    "aux_dims": ln.aux_dims,
                }
                for ln in voucher.lines
            ],
        },
        actor=actor,
    )
    return rev


def import_opening_balances(
    session: Session,
    *,
    ledger_set_id: str,
    actor: dict,
    lines: list[dict],
    period_year: int | None = None,
    period_month: int | None = None,
    force: bool = False,
) -> Voucher:
    """导入期初余额：生成直接过账的「期初-NNNN」凭证并累计余额投影。

    试算不平衡（TRIAL_BALANCE_UNBALANCED）时整体拒绝，不落任何数据。

        幂等保护（P0-2 修复）：
        期初是账套根基，**同一账套只能有一份生效期初**。重复导入会直接翻倍
        （实测：20 万 → 40 万），属毁账级缺陷。因此：

        - 默认（force=False）：已有【生效中】的期初 → 抛 OPENING_ALREADY_IMPORTED
          拒绝，details 带回已有凭证号，便于用户核对后再决定
        - force=True：先对全部【生效中】的期初做红字冲销（余额归零 + 事件留痕，
          原凭证保留以维持审计链），再导入新期初。凭证号继续递增不复用。
          已冲销过的旧凭证不会二次冲销（否则余额会被反向冲成负数）

        校验顺序：期间可用性 → 数据合法性（科目/金额）→ 试算平衡 → 幂等状态。
        即**输入错误优先于状态冲突**——数据本身不平就没必要谈重复导入，
        否则用户改完数据再提交仍然被拒，白跑一趟。
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

    # ── 幂等闸门（试算已通过，此时才轮到状态冲突检查）──
    existing = _active_opening_vouchers(session, ledger_set_id)
    if existing and not force:
        raise PostingError(
            "OPENING_ALREADY_IMPORTED",
            "本账套已导入期初余额，重复导入会导致期初翻倍。"
            "如确需重新导入，请显式传入 force=true（将红字冲销旧期初后重新导入，全程留审计痕）",
            {
                "existing_vouchers": [
                    {"voucher_no": v.voucher_no, "voucher_date": str(v.voucher_date)}
                    for v in existing
                ],
                "hint": "force=true 会红字冲销上述凭证，原凭证保留在审计链中",
            },
        )

    # force 重导：先把全部【生效中】旧期初红字冲销（余额归零），再记入新期初。
    # 必须发生在新凭证入账之前，否则冲销会把新期初也反向掉。
    if existing and force:
        code_map = {a.id: a.code for a in accounts.values()}
        for old in existing:
            _reverse_opening(
                session,
                voucher=old,
                code_map=code_map,
                actor=actor,
                reason="force_reimport",
            )
        session.flush()

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
        # 期初凭证日期固定为期间首日（确定性）：不得取当天，否则跨月边界
        # 时日期漂移会把期初挤进当期发生额（2026-09-01 实测踩坑）。
        voucher_date=date(period.year, period.month, 1),
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
        event_type=E.OPENING_BALANCE_IMPORTED,
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
