"""0005: Account.attrs 科目级本体属性列（阶段1本体底座）。

accounts 增加一列：
- attrs: JSON，科目级声明属性（如 cash_flow/bad_debt/depreciate/amortize/deduction），
  真源是 coa 模板 attrs 列，导入时经 kernel.coa.parse_attrs 解析落库。
幂等加列（SQLite 无严格类型，JSON 直接可用；PG 由 create_all/migration 对齐）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_account_attrs"
down_revision = "0004_agent_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("accounts")}
    if "attrs" not in cols:
        bind.execute(sa.text("ALTER TABLE accounts ADD COLUMN attrs JSON"))


def downgrade() -> None:
    pass  # SQLite 不支持 DROP COLUMN；保留列无害
