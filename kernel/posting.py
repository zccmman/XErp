"""记账内核：凭证硬校验与过账（P0-05）。

ADR-002：余额是投影不是事实——post_voucher 同时维护 balances 投影；
ADR-003：错误码大写蛇形 + message_zh 可直接展示；金额一律 Decimal，禁止 float；
ADR-004：APPROVED → POSTED 是唯一合法记账跃迁，非法跃迁一律 INVALID_TRANSITION。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Balance, Event, Period, Voucher, utcnow
from kernel.events import E
from kernel.ledger import append_event
from kernel.ledger.canonical import canonical_json

ZERO = Decimal("0")


class PostingError(Exception):
    """记账失败（ADR-003 错误信封的内核形态）。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


@dataclass(frozen=True)
class PostingLine:
    """凭证明细行的纯数据形态（金额必须是 Decimal）。"""

    account_id: str
    debit: Decimal
    credit: Decimal
    aux_dims: dict | None = None


def validate_voucher(
    *,
    lines: list[PostingLine],
    accounts_by_id: dict[str, object],
    period_status: str,
    period_year: int,
    period_month: int,
    voucher_date,
) -> None:
    """凭证硬校验：通过则静默返回，否则抛 PostingError。"""
    if len(lines) < 2:
        raise PostingError(
            "NO_LINES",
            "凭证至少需要两行分录",
            {"line_count": len(lines)},
        )
    if period_status != "OPEN":
        raise PostingError(
            "PERIOD_NOT_OPEN",
            f"会计期间状态为 {period_status}，只有未结账（OPEN）期间才能记账",
            {"period_status": period_status},
        )
    if (voucher_date.year, voucher_date.month) != (period_year, period_month):
        raise PostingError(
            "PERIOD_MISMATCH",
            f"凭证日期 {voucher_date.isoformat()} 不属于期间 "
            f"{period_year}-{period_month:02d}",
            {
                "voucher_date": voucher_date.isoformat(),
                "period_year": period_year,
                "period_month": period_month,
            },
        )

    total_debit = ZERO
    total_credit = ZERO
    for no, line in enumerate(lines, start=1):
        debit, credit = line.debit, line.credit
        if debit < ZERO or credit < ZERO:
            raise PostingError(
                "AMOUNT_INVALID",
                f"第 {no} 行存在负数金额，借贷金额必须为零或正数",
                {"line_no": no, "debit": str(debit), "credit": str(credit)},
            )
        if debit > ZERO and credit > ZERO:
            raise PostingError(
                "LINE_BOTH_SIDES",
                f"第 {no} 行借贷双方同时有金额，一行只能记借方或贷方之一",
                {"line_no": no},
            )
        if debit == ZERO and credit == ZERO:
            raise PostingError(
                "AMOUNT_INVALID",
                f"第 {no} 行借贷金额均为零",
                {"line_no": no},
            )
        if line.account_id not in accounts_by_id:
            raise PostingError(
                "ACCOUNT_NOT_FOUND",
                f"第 {no} 行科目不存在：{line.account_id}",
                {"line_no": no, "account_id": line.account_id},
            )
        total_debit += debit
        total_credit += credit

    if total_debit != total_credit:
        raise PostingError(
            "VOUCHER_UNBALANCED",
            f"借贷不平衡：借 {total_debit} ≠ 贷 {total_credit}",
            {"total_debit": str(total_debit), "total_credit": str(total_credit)},
        )


def post_voucher(session: Session, *, voucher_id: str, actor: dict) -> Event:
    """过账：APPROVED → POSTED，写 voucher.posted 事件并累计 balances 投影。

    校验失败时本事务不落任何变更；commit 由调用方负责。
    """
    voucher = session.get(Voucher, voucher_id)
    if voucher.status != "APPROVED":
        raise PostingError(
            "INVALID_TRANSITION",
            f"凭证当前状态为 {voucher.status}，仅已审批（APPROVED）状态可以记账",
            {"voucher_no": voucher.voucher_no, "status": voucher.status},
        )

    period = session.get(Period, voucher.period_id)
    accounts_by_id = {
        a.id: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == voucher.ledger_set_id)
        ).all()
    }
    lines = [
        PostingLine(
            account_id=line.account_id,
            debit=line.debit,
            credit=line.credit,
            aux_dims=line.aux_dims,
        )
        for line in voucher.lines
    ]
    validate_voucher(
        lines=lines,
        accounts_by_id=accounts_by_id,
        period_status=period.status,
        period_year=period.year,
        period_month=period.month,
        voucher_date=voucher.voucher_date,
    )

    voucher.status = "POSTED"
    voucher.posted_at = utcnow()

    payload = {
        "voucher_no": voucher.voucher_no,
        "voucher_date": voucher.voucher_date.isoformat(),
        "summary": voucher.summary,
        "lines": [
            {
                "line_no": line.line_no,
                "account_id": line.account_id,
                "debit": str(line.debit),
                "credit": str(line.credit),
                "aux_dims": line.aux_dims,
            }
            for line in voucher.lines
        ],
    }
    evt = append_event(
        session,
        ledger_set_id=voucher.ledger_set_id,
        event_type=E.VOUCHER_POSTED,
        aggregate_id=voucher.id,
        payload=payload,
        actor=actor,
    )

    _accumulate_balances(session, voucher=voucher, lines=lines)
    session.flush()
    return evt


def _dims_key(aux_dims: dict | None) -> str:
    """辅助维度 JSON 的 canonical 排序串；无辅助维度为空串（ADR-002 规范）。"""
    if not aux_dims:
        return ""
    return canonical_json(aux_dims)


def _accumulate_balances(session: Session, *, voucher: Voucher, lines: list[PostingLine]) -> None:
    """按（账套, 期间, 科目, dims_key）累计借/贷发生额——余额是投影不是事实。"""
    for line in lines:
        key = _dims_key(line.aux_dims)
        bal = session.scalars(
            select(Balance).where(
                Balance.ledger_set_id == voucher.ledger_set_id,
                Balance.period_id == voucher.period_id,
                Balance.account_id == line.account_id,
                Balance.dims_key == key,
            )
        ).first()
        if bal is None:
            bal = Balance(
                ledger_set_id=voucher.ledger_set_id,
                period_id=voucher.period_id,
                account_id=line.account_id,
                dims_key=key,
                debit_total=ZERO,
                credit_total=ZERO,
            )
            session.add(bal)
        bal.debit_total += line.debit
        bal.credit_total += line.credit
