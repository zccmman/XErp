"""0007: VoucherLine 外币/数量核算列补迁移（② 缺口修复）。

voucher_lines 增加 6 列（② 外币/数量核算加入 models.py 时漏写了迁移——
与 G1 漏写 vouchers 签字列是同一类事故，测试库每次 create_all 全新建表
掩盖了老库升级路径；dev 库经新代码查询直接 500：

    no such column: voucher_lines.currency

- currency:        VARCHAR(8)，币种；空=本币（账套 functional_currency）
- fx_rate:         NUMERIC(18,6)，记账汇率；本币行为为 NULL
- foreign_debit:   NUMERIC(18,2)，原币借方，默认 0（非本币必填）
- foreign_credit:  NUMERIC(18,2)，原币贷方，默认 0（非本币必填）
- quantity:        NUMERIC(18,3)，数量；quantity=yes 科目必填，否则 NULL
- unit:            VARCHAR(16)，计量单位

foreign_debit/foreign_credit 在模型里是 NOT NULL default=0，必须带 DEFAULT 0，
否则既有纯本币凭证行读取到 NULL 会破坏 Decimal 运算。其余四列可空。

幂等加列（先 inspect 再 ALTER），SQLite/PG 通用；老库可直接 upgrade head。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_voucher_foreign_quantity"
down_revision = "0006_voucher_signers"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "voucher_lines")
    adds = [
        ("currency", "VARCHAR(8)"),
        ("fx_rate", "NUMERIC(18,6)"),
        ("foreign_debit", "NUMERIC(18,2) DEFAULT 0"),
        ("foreign_credit", "NUMERIC(18,2) DEFAULT 0"),
        ("quantity", "NUMERIC(18,3)"),
        ("unit", "VARCHAR(16)"),
    ]
    for name, typ in adds:
        if name not in cols:
            bind.execute(
                sa.text(f"ALTER TABLE voucher_lines ADD COLUMN {name} {typ}")
            )


def downgrade() -> None:
    pass  # SQLite 不支持 DROP COLUMN；保留列无害
