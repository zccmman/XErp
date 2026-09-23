"""0009: 应收应付未清项核销表 arap_clearing（Phase A / G1）。

核销是新增的不可变记录（关联回款行与发票行 + 金额），不改写任何凭证或
余额投影；未清项可由「凭证明细行 + arap_clearing」完全重建（ADR-002）。

字段与 kernel/db/models.py::ArapClearing 一一对应：
- id / ledger_set_id / dim_key(customer|supplier) / partner
- invoice_line_id / payment_line_id（凭证明细行 id，字符串，不跨表加 FK）
- amount（NUMERIC(18,2)，功能币种）
- cleared_at（业务日期）/ source(manual|ai_proposed|fifo_auto)
- created_by / created_at

幂等建表（先 inspect 再 CREATE），SQLite/PG 通用；老库可直接 upgrade head。
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_arap_clearing"
down_revision = "0008_autonomy_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if "arap_clearing" in existing:
        return  # 已存在（测试库 create_all 或重复升级）→ 幂等跳过

    op.create_table(
        "arap_clearing",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32), nullable=False),
        sa.Column("dim_key", sa.String(16), nullable=False),
        sa.Column("partner", sa.String(128), nullable=False),
        sa.Column("invoice_line_id", sa.String(32), nullable=False),
        sa.Column("payment_line_id", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("cleared_at", sa.Date, nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_arap_clearing_ledger_set_id", "arap_clearing", ["ledger_set_id"]
    )
    op.create_index(
        "ix_arap_clearing_invoice_line_id", "arap_clearing", ["invoice_line_id"]
    )
    op.create_index(
        "ix_arap_clearing_payment_line_id", "arap_clearing", ["payment_line_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "arap_clearing" in set(insp.get_table_names()):
        op.drop_table("arap_clearing")
