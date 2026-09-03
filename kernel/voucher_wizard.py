"""凭证创建原语（G1 制单闭环）——MCP 与 Web 共用的唯一入口。

为什么必须抽到内核层：
    修复前创建凭证的逻辑整段写在 mcp-server 的 create_voucher 工具里。
    Web 端要做制单，若照抄一遍，就会出现「两条制单路径两套校验」——
    这是最危险的重复：今天在 MCP 侧补一条校验，明天 Web 侧忘了补，
    同一笔业务走对话进得来、走界面进不来（或反过来），
    账上出现只有某条路径才拦得住的脏数据。

    因此这里提供唯一实现，两边都调它。

不在本模块职责内：
    权限（kernel.authz.enforce）、Agent 配额（check_agent_quota）、
    断路器（check_breaker）—— 这三道治理门禁由调用方按需叠加，
    因为 Web 端与 MCP 端的主体类型不同（人 / Agent），策略本就有差异。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Period, Voucher, VoucherLine
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import (
    PostingError,
    PostingLine,
    validate_voucher,
)

ZERO = Decimal("0")


def _fmt(d: Decimal | None) -> str:
    return f"{(d or ZERO):.2f}"


def _amount(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value if value not in (None, "") else "0")).quantize(
            Decimal("0.01")
        )
    except Exception:
        raise PostingError("AMOUNT_INVALID", f"{field} 不是合法金额: {value!r}") from None
    return d


def voucher_snapshot(session: Session, v: Voucher) -> dict:
    """凭证快照（写进 VOUCHER_CREATED 事件 payload）。

    事件是 append-only 的事实，payload 结构一旦有数据写入就不可再改，
    因此此函数输出必须与历史数据保持一致。
    """
    ids = [ln.account_id for ln in v.lines]
    cmap = {
        a.id: a.code
        for a in session.scalars(select(Account).where(Account.id.in_(ids)))
    } if ids else {}
    total_debit = sum((ln.debit for ln in v.lines), ZERO)
    return {
        "voucher_no": v.voucher_no,
        "voucher_date": v.voucher_date.isoformat(),
        "summary": v.summary or "",
        "total_debit": _fmt(total_debit),
        "lines": [
            {
                "account_code": cmap.get(ln.account_id, "?"),
                "debit": _fmt(ln.debit),
                "credit": _fmt(ln.credit),
                "aux_dims": ln.aux_dims or {},
            }
            for ln in v.lines
        ],
    }


def _parse_date(value: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise PostingError(
            "DATE_INVALID", f"日期格式应为 YYYY-MM-DD: {value!r}"
        ) from None


def next_voucher_no(session: Session, ledger_set_id: str, prefix: str = "记-") -> str:
    """下一张凭证号。按账套内凭证总数递增，不复用已用过的号。"""
    seq = (
        len(
            session.scalars(
                select(Voucher.id).where(Voucher.ledger_set_id == ledger_set_id)
            ).all()
        )
        + 1
    )
    return f"{prefix}{seq:04d}"


def create_draft_voucher(
    session: Session,
    *,
    ledger_set_id: str,
    actor: dict,
    voucher_date,
    summary: str = "",
    lines: list[dict] | None = None,
    idempotency_key: str | None = None,
    prefix: str = "记-",
) -> tuple[Voucher, bool]:
    """创建草稿凭证并即时硬校验。

    lines 形如 [{"account_code": "6602", "debit": "800", "credit": ""}]，
    金额为字符串或数字，空表示 0。

    返回 (voucher, replayed)：replayed=True 表示命中 idempotency_key
    直接返回既有凭证，未新建。

    抛 PostingError：DATE_INVALID / ACCOUNT_NOT_FOUND / PERIOD_NOT_FOUND /
    NO_LINES / PERIOD_NOT_OPEN / PERIOD_MISMATCH / VOUCHER_UNBALANCED 等，
    message_zh 可直接展示给最终用户。
    """
    d = _parse_date(voucher_date)
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == d.year,
            Period.month == d.month,
        )
    ).first()
    accounts = {
        a.code: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }

    posting_lines: list[PostingLine] = []
    orm_lines: list[VoucherLine] = []
    for i, ln in enumerate(lines or [], start=1):
        code = (ln.get("account_code") or "").strip()
        acc = accounts.get(code)
        if acc is None:
            raise PostingError("ACCOUNT_NOT_FOUND", f"第 {i} 行科目不存在: {code!r}")
        dr = _amount(ln.get("debit"), f"第{i}行借方")
        cr = _amount(ln.get("credit"), f"第{i}行贷方")
        dims = ln.get("aux_dims") or {}
        posting_lines.append(PostingLine(acc.id, dr, cr, dims))
        orm_lines.append(
            VoucherLine(
                line_no=i,
                account_id=acc.id,
                debit=dr,
                credit=cr,
                aux_dims=dims or None,
            )
        )

    validate_voucher(
        lines=posting_lines,
        accounts_by_id={a.id: a for a in accounts.values()},
        period_status=period.status if period else "MISSING",
        period_year=d.year,
        period_month=d.month,
        voucher_date=d,
    )
    if period is None:
        raise PostingError(
            "PERIOD_NOT_FOUND", f"{d.year}-{d.month:02d} 期间不存在，请先初始化"
        )

    v = Voucher(
        ledger_set_id=ledger_set_id,
        period_id=period.id,
        voucher_no=next_voucher_no(session, ledger_set_id, prefix),
        voucher_date=d,
        status="DRAFT",
        summary=summary,
        created_by=str(actor.get("id") or ""),
        idempotency_key=idempotency_key,
        lines=orm_lines,
    )
    session.add(v)
    try:
        session.flush()
    except Exception as exc:  # IntegrityError：idempotency_key 唯一约束冲突
        from sqlalchemy.exc import IntegrityError

        if not isinstance(exc, IntegrityError) or not idempotency_key:
            raise
        session.rollback()
        prior = session.scalars(
            select(Voucher).where(Voucher.idempotency_key == idempotency_key)
        ).first()
        if prior is None:
            raise
        return prior, True
    return v, False


def record_voucher_created(session: Session, voucher: Voucher, actor: dict):
    """为新建凭证补 VOUCHER_CREATED 事件（append-only 事实）。

    与 create_draft_voucher 分开，是为了让「建对象 → flush 抢唯一约束 →
    成功后再记事件」这个顺序显式可见：失败重放的那次不该再记一次事件。
    """
    append_event(
        session,
        ledger_set_id=voucher.ledger_set_id,
        event_type=E.VOUCHER_CREATED,
        aggregate_id=voucher.id,
        payload=voucher_snapshot(session, voucher),
        actor=actor,
    )
    session.flush()
