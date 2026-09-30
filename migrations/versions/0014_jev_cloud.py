"""0014: JEV 云端模式授权配置表 jev_cloud_setting。

每账套一条：backend(local|typesafe) + cloud_consent(是否已授权数据出境)。
云端模式默认关闭；仅当用户显式开启并授权、且配置 TYPESAFE_API_KEY 时才启用，
把决策的聚合指标发送至 TypeSafe 云（api.typesafe.ai）获取校准置信度。
幂等建表（先 inspect 再 CREATE）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_jev_cloud"
down_revision = "0013_budget"
branch_labels = None
depends_on = None


def _has(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if not _has(bind, "jev_cloud_setting"):
        op.create_table(
            "jev_cloud_setting",
            sa.Column(
                "ledger_set_id",
                sa.String(32),
                sa.ForeignKey("ledger_sets.id"),
                primary_key=True,
            ),
            sa.Column("backend", sa.String(16), nullable=False, server_default="local"),
            sa.Column("cloud_consent", sa.Boolean(), nullable=False),
            sa.Column("consent_actor", sa.String(64), nullable=True),
            sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_jev_cloud_setting_ledger_set_id", "jev_cloud_setting", ["ledger_set_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has(bind, "jev_cloud_setting"):
        op.drop_table("jev_cloud_setting")
