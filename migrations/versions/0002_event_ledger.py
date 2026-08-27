"""event ledger v0（含 append-only 强制）

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONV = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ledger_set_id", sa.String(32), nullable=False, index=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("payload", JSONV, nullable=False),
        sa.Column("actor", JSONV, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION prevent_events_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'events is append-only (%)', TG_OP;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER events_no_mutation
            BEFORE UPDATE OR DELETE ON events
            FOR EACH ROW EXECUTE FUNCTION prevent_events_mutation();
            """
        )
    else:  # sqlite（测试环境）
        for op_name in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER events_no_{op_name}
                BEFORE {op_name} ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events is append-only');
                END;
                """
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS events_no_mutation ON events")
        op.execute("DROP FUNCTION IF EXISTS prevent_events_mutation()")
    else:
        op.execute("DROP TRIGGER IF EXISTS events_no_UPDATE")
        op.execute("DROP TRIGGER IF EXISTS events_no_DELETE")
    op.drop_table("events")
