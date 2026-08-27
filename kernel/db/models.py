"""Ontology Schema v0 — 依 ADR-001/002/004/005。

表：events / subjects / ledger_sets / accounts / periods / parties / vouchers / voucher_lines
契约：金额 Numeric(18,2)（SQLite 测试回退为近似值，PG 为精确 Decimal）；
     维度类字段 JSONVariant（PG=JSONB）；业务表无 updated_at——历史由事件账本承载（ADR-002）。
"""

import decimal
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kernel.db.base import Base, JSONVariant

AMOUNT = Numeric(18, 2)


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Event(Base):
    """事件账本（ADR-002）：唯一事实源，append-only，账套内 sha256 成链。

    hash = sha256(prev_hash + canonical_json(ledger_set_id, event_type,
                                          aggregate_id, payload, actor, occurred_at))
    首条事件 prev_hash = GENESIS（64 个 0）。本表禁止 UPDATE/DELETE（触发器强制）。
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        Integer().with_variant(postgresql.BIGINT(), "postgresql"),
        primary_key=True,
        autoincrement=True,
    )
    ledger_set_id: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONVariant)
    actor: Mapped[dict] = mapped_column(JSONVariant)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class Subject(Base):
    """身份：人与 Agent 同表一等公民（ADR-005）。"""

    __tablename__ = "subjects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(8))  # user | agent
    display_name: Mapped[str] = mapped_column(String(100))
    autonomy_level: Mapped[int] = mapped_column(Integer, default=1)  # L0-L3（ADR-004）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerSet(Base):
    """账套。"""

    __tablename__ = "ledger_sets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    accounting_standard: Mapped[str] = mapped_column(String(30), default="small_business")
    functional_currency: Mapped[str] = mapped_column(String(8), default="CNY")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Account(Base):
    """科目：语义骨架（ADR-004 前置；方向 debit|credit）。"""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("ledger_set_id", "code", name="uq_account_code"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ledger_set_id: Mapped[str] = mapped_column(ForeignKey("ledger_sets.id"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    direction: Mapped[str] = mapped_column(String(8))  # debit | credit
    category: Mapped[str] = mapped_column(String(16))  # asset|liability|equity|cost|pnl
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    is_leaf: Mapped[bool] = mapped_column(default=True)
    aux_dim_defs: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    parent: Mapped["Account | None"] = relationship(
        remote_side="Account.id", back_populates="children"
    )
    children: Mapped[list["Account"]] = relationship(back_populates="parent")


class Period(Base):
    """会计期间：OPEN | CLOSING | CLOSED（ADR-004 cancel_post 窗口依据）。"""

    __tablename__ = "periods"
    __table_args__ = (UniqueConstraint("ledger_set_id", "year", "month", name="uq_period"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ledger_set_id: Mapped[str] = mapped_column(ForeignKey("ledger_sets.id"))
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default="OPEN")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Party(Base):
    """往来/辅助核算对象：customer|supplier|department|project|other。"""

    __tablename__ = "parties"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ledger_set_id: Mapped[str] = mapped_column(ForeignKey("ledger_sets.id"))
    party_type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    aux_attrs: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)


class Voucher(Base):
    """凭证聚合根：状态机见 ADR-004。"""

    __tablename__ = "vouchers"
    __table_args__ = (UniqueConstraint("ledger_set_id", "voucher_no", name="uq_voucher_no"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ledger_set_id: Mapped[str] = mapped_column(ForeignKey("ledger_sets.id"))
    period_id: Mapped[str] = mapped_column(ForeignKey("periods.id"))
    voucher_no: Mapped[str] = mapped_column(String(32))
    voucher_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(8), default="DRAFT")
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("subjects.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list["VoucherLine"]] = relationship(
        back_populates="voucher", order_by="VoucherLine.line_no", cascade="all, delete-orphan"
    )


class VoucherLine(Base):
    """凭证明细行：借贷金额 + 辅助核算维度。"""

    __tablename__ = "voucher_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    voucher_id: Mapped[str] = mapped_column(ForeignKey("vouchers.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    debit: Mapped[decimal.Decimal] = mapped_column(AMOUNT, default=0)
    credit: Mapped[decimal.Decimal] = mapped_column(AMOUNT, default=0)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    aux_dims: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)

    voucher: Mapped[Voucher] = relationship(back_populates="lines")


class Balance(Base):
    """余额投影（ADR-002）：可随时由事件流全量重建，不是事实源。

    维度：账套 + 期间 + 科目 + 辅助维度规范键（canonical_json，无辅助维度为空串）。
    """

    __tablename__ = "balances"
    __table_args__ = (
        UniqueConstraint(
            "ledger_set_id", "period_id", "account_id", "dims_key", name="uq_balance_dim"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ledger_set_id: Mapped[str] = mapped_column(String(32), index=True)
    period_id: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    dims_key: Mapped[str] = mapped_column(String(500), default="")
    debit_total: Mapped[decimal.Decimal] = mapped_column(AMOUNT, default=0)
    credit_total: Mapped[decimal.Decimal] = mapped_column(AMOUNT, default=0)
