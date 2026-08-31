"""0004: Agent 自治额度字段（P1-03）。

subjects 增加两列：
- daily_voucher_limit: Agent 单日新增凭证金额上限（NUMERIC(14,2)，NULL=不限）
- quota_currency: 额度币种（默认 CNY）
权限策略表 casbin_rule 由 casbin-sqlalchemy-adapter 运行时自建，无需迁移。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_agent_quota"
down_revision = "0003"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:

    insp = sa.inspect(bind)
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "subjects")
    if "daily_voucher_limit" not in cols:
        bind.execute(sa.text(
            "ALTER TABLE subjects ADD COLUMN daily_voucher_limit NUMERIC(14,2)"
        ))
    if "quota_currency" not in cols:
        bind.execute(sa.text(
            "ALTER TABLE subjects ADD COLUMN quota_currency VARCHAR(8) DEFAULT 'CNY'"
        ))


def downgrade() -> None:
    pass  # SQLite 不支持 DROP COLUMN；保留列无害
