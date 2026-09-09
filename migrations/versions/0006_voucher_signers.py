"""0006: Voucher 多级签字列补迁移（G1 缺口修复）。

vouchers 增加两列（G1-49 多级签字加入 models.py 时漏写了迁移——
测试库每次 create_all 全新建表，掩盖了旧库升级路径的缺失）：
- required_signers: JSON，要求签署的角色列表，如 ["cashier","manager"]
- signatures: JSON，已收签署记录 [{"role":...,"subject_id":...,"signed_at":...}]

幂等加列（先 inspect 再 ALTER），SQLite/PG 通用；老库可直接 upgrade head。
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_voucher_signers"
down_revision = "0005_account_attrs"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "vouchers")
    if "required_signers" not in cols:
        bind.execute(sa.text("ALTER TABLE vouchers ADD COLUMN required_signers JSON"))
    if "signatures" not in cols:
        bind.execute(sa.text("ALTER TABLE vouchers ADD COLUMN signatures JSON"))


def downgrade() -> None:
    pass  # SQLite 不支持 DROP COLUMN；保留列无害
