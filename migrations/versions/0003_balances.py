"""balances projection v0

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "balances",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32), nullable=False, index=True),
        sa.Column("period_id", sa.String(32), nullable=False, index=True),
        sa.Column("account_id", sa.String(32), nullable=False, index=True),
        sa.Column("dims_key", sa.String(500), nullable=False),
        sa.Column("debit_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("credit_total", sa.Numeric(18, 2), nullable=False),
        sa.UniqueConstraint(
            "ledger_set_id", "period_id", "account_id", "dims_key", name="uq_balance_dim"
        ),
    )


def downgrade() -> None:
    op.drop_table("balances")
