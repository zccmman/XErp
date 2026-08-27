"""ontology v0

Revision ID: 0001
Revises:
Create Date: 2026-08-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONV = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "subjects",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("type", sa.String(8), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("autonomy_level", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ledger_sets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("accounting_standard", sa.String(30), nullable=False),
        sa.Column("functional_currency", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32),
                  sa.ForeignKey("ledger_sets.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("is_leaf", sa.Boolean, nullable=False),
        sa.Column("aux_dim_defs", JSONV, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ledger_set_id", "code", name="uq_account_code"),
    )
    op.create_table(
        "periods",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32),
                  sa.ForeignKey("ledger_sets.id"), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ledger_set_id", "year", "month", name="uq_period"),
    )
    op.create_table(
        "parties",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32),
                  sa.ForeignKey("ledger_sets.id"), nullable=False),
        sa.Column("party_type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("aux_attrs", JSONV, nullable=True),
    )
    op.create_table(
        "vouchers",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32),
                  sa.ForeignKey("ledger_sets.id"), nullable=False),
        sa.Column("period_id", sa.String(32), sa.ForeignKey("periods.id"), nullable=False),
        sa.Column("voucher_no", sa.String(32), nullable=False),
        sa.Column("voucher_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("summary", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("subjects.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ledger_set_id", "voucher_no", name="uq_voucher_no"),
        sa.UniqueConstraint("idempotency_key", name="uq_idempotency"),
    )
    op.create_table(
        "voucher_lines",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("voucher_id", sa.String(32),
                  sa.ForeignKey("vouchers.id"), nullable=False),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column("account_id", sa.String(32), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit", sa.Numeric(18, 2), nullable=False),
        sa.Column("credit", sa.Numeric(18, 2), nullable=False),
        sa.Column("summary", sa.String(500), nullable=True),
        sa.Column("aux_dims", JSONV, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("voucher_lines")
    op.drop_table("vouchers")
    op.drop_table("parties")
    op.drop_table("periods")
    op.drop_table("accounts")
    op.drop_table("ledger_sets")
    op.drop_table("subjects")
