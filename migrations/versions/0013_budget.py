"""0013: 预算主数据表 budgets / budget_lines（ERP 模块纵深 · 计划与控制）。

两张表为**业务主数据**（非投影、非凭证），与 models.py 的 Budget / BudgetLine 一一对应：
- budgets: 一套账套一个会计年度可有多个版本；同一 (ledger_set_id, fiscal_year)
  仅一个 ACTIVE 版本生效（activate_budget 时同组其余版本回退 SUPERSEDED）。
- budget_lines: 按 科目 code × 期间 量化预算净额（period=0 表示年度总额，1..12 表示月份）。

预算与账本严格隔离：内核**不**改写任何凭证或余额，仅在 v.s. 实际对比时
经 amounts_by_code（单一真源）读取实际发生额做差异分析（见 kernel/budget.py）。

幂等建表（先 inspect 再 CREATE），SQLite/PG 通用；老库可直接 upgrade head。
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_budget"
down_revision = "0012_inventory_asset"
branch_labels = None
depends_on = None


def _has(bind, table: str) -> bool:
    return table in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if not _has(bind, "budgets"):
        op.create_table(
            "budgets",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "ledger_set_id",
                sa.String(32),
                sa.ForeignKey("ledger_sets.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
            sa.Column("note", sa.String(500), nullable=True),
            sa.Column("created_by", sa.String(32), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "ledger_set_id", "fiscal_year", "version", name="uq_budget_rev"
            ),
        )
        op.create_index("ix_budgets_ledger_set_id", "budgets", ["ledger_set_id"])

    if not _has(bind, "budget_lines"):
        op.create_table(
            "budget_lines",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "budget_id",
                sa.String(32),
                sa.ForeignKey("budgets.id"),
                nullable=False,
            ),
            sa.Column("account_code", sa.String(32), nullable=False),
            sa.Column("period", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
            sa.Column("note", sa.String(200), nullable=True),
            sa.UniqueConstraint(
                "budget_id", "account_code", "period", name="uq_budget_line"
            ),
        )
        op.create_index("ix_budget_lines_budget_id", "budget_lines", ["budget_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has(bind, "budget_lines"):
        op.drop_table("budget_lines")
    if _has(bind, "budgets"):
        op.drop_table("budgets")
