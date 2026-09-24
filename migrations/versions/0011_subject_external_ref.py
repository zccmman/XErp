"""0011: Subject.external_ref 外部身份映射列（P0-4 / P0-3 WB 原生审批闭环）。

subjects 增加一列：
- external_ref: 外部通道身份键（WB user id / 飞书 open_id / 企微 userid），
  NULL=未绑定。仅作「外部通道 → 内核人主体」的解析键，不参与授权判定
  （授权仍由内核 Subject.type 派生，见 kernel.state._subject_type）。

幂等：已存在该列则跳过 ALTER（老库升级路径钉死于 tests/test_migrations.py）。
PG 与 SQLite 共用同一 ALTER，VARCHAR(64) 无 NOT NULL 约束，兼容旧行。
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_subject_external_ref"
down_revision = "0010_party_credit_limit"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "subjects")
    if "external_ref" not in cols:
        bind.execute(sa.text(
            "ALTER TABLE subjects ADD COLUMN external_ref VARCHAR(64)"
        ))


def downgrade() -> None:
    pass  # SQLite 不支持 DROP COLUMN；保留列无害
