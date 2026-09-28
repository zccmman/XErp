"""0012: P0-1 主数据表 inventory_items（存货档案）/ asset_cards（固定资产卡片）。

两张表均为**业务主数据**（非投影），必须持久化，与 models.py 一一对应：
- inventory_items: 货品编码/名称/计价方法/默认存货科目（收发存台账由凭证明细重建，不存表）
- asset_cards: 资产编号/原值/残值率/年限/开始折旧日/累计折旧(展示用，真源仍是 1602 凭证)

幂等建表（先 inspect 再 CREATE），SQLite/PG 通用；老库可直接 upgrade head。
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_inventory_asset"
down_revision = "0011_subject_external_ref"
branch_labels = None
depends_on = None


def _has(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if not _has(bind, "inventory_items"):
        op.create_table(
            "inventory_items",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("ledger_set_id", sa.String(32), nullable=False),
            sa.Column("code", sa.String(32), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("spec", sa.String(200), nullable=True),
            sa.Column("unit", sa.String(16), nullable=True),
            sa.Column("valuation_method", sa.String(16), nullable=False, server_default="weighted_avg"),
            sa.Column("default_account_code", sa.String(32), nullable=False, server_default="1405"),
            sa.Column("attrs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("ledger_set_id", "code", name="uq_inventory_item_code"),
        )
        op.create_index("ix_inventory_items_ledger_set_id", "inventory_items", ["ledger_set_id"])

    if not _has(bind, "asset_cards"):
        op.create_table(
            "asset_cards",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("ledger_set_id", sa.String(32), nullable=False),
            sa.Column("asset_no", sa.String(32), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("category_code", sa.String(32), nullable=False, server_default="160101"),
            sa.Column("original_value", sa.Numeric(18, 2), nullable=False),
            sa.Column("salvage_rate", sa.Numeric(6, 4), nullable=False, server_default="0"),
            sa.Column("method", sa.String(16), nullable=False, server_default="straight"),
            sa.Column("useful_life_months", sa.Integer(), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("accumulated_depreciation", sa.Numeric(18, 2), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("aux_dims", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("ledger_set_id", "asset_no", name="uq_asset_no"),
        )
        op.create_index("ix_asset_cards_ledger_set_id", "asset_cards", ["ledger_set_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has(bind, "asset_cards"):
        op.drop_table("asset_cards")
    if _has(bind, "inventory_items"):
        op.drop_table("inventory_items")
