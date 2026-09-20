"""0008: L3 自治授权令牌表 autonomy_grants（O11 红线：每会话显式授权，人是 Boss 硬门禁）。

autonomous_post 现在**必须**持有一张有效、未过期、预算充足的授权令牌，
否则一律拒绝（AUTH_TOKEN_REQUIRED）；令牌预算用尽自动跳闸冻结 Agent
（见 kernel.autonomy._auto_pause_on_budget）。令牌由人类 admin 显式签发。

新建表 autonomy_grants，字段与 kernel/db/models.py::AutonomyGrant 一一对应：
- id / ledger_set_id / agent_subject_id / admin_subject_id
- budget / remaining（NUMERIC(14,2)，CNY）
- expires_at（带时区）、is_revoked（Boolean）、note、created_at / updated_at

幂等建表（先 inspect 再 CREATE），SQLite/PG 通用；老库可直接 upgrade head。
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_autonomy_grants"
down_revision = "0007_voucher_foreign_quantity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if "autonomy_grants" in existing:
        return  # 已存在（测试库 create_all 或重复升级）→ 幂等跳过

    op.create_table(
        "autonomy_grants",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ledger_set_id", sa.String(32), nullable=False),
        sa.Column("agent_subject_id", sa.String(32), nullable=False),
        sa.Column("admin_subject_id", sa.String(32), nullable=False),
        sa.Column("budget", sa.Numeric(14, 2), nullable=False),
        sa.Column("remaining", sa.Numeric(14, 2), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_autonomy_grants_ledger_set_id", "autonomy_grants", ["ledger_set_id"]
    )
    op.create_index(
        "ix_autonomy_grants_agent_subject_id", "autonomy_grants", ["agent_subject_id"]
    )


def downgrade() -> None:
    # SQLite 不支持 DROP TABLE 带外键依赖的稳妥回滚；此处仅清理本表。
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "autonomy_grants" in set(insp.get_table_names()):
        op.drop_table("autonomy_grants")
