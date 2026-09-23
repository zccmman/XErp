"""0010: 往来对象 Party 增加授信额度 credit_limit 列（Phase B / G2）。

信用额度是 Boss 对客户的赊销上限配置，不是账本余额投影；0 = 不设额度。
复用 Party 单表作为客户属性真源，避免新建投影表。

幂等 add_column：先 inspect 判定列是否存在，已存在则跳过（测试库 create_all
或重复升级 / 老库 stamp 后 upgrade 均不报错）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_party_credit_limit"
down_revision = "0009_arap_clearing"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "parties", "credit_limit"):
        return  # 已存在 → 幂等跳过
    op.add_column(
        "parties",
        sa.Column(
            "credit_limit", sa.Numeric(18, 2), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "parties", "credit_limit"):
        return
    # SQLite 旧版本不支持 DROP COLUMN（<3.35），用 batch 操作重建表兼容。
    with op.batch_alter_table("parties") as batch_op:
        batch_op.drop_column("credit_limit")
