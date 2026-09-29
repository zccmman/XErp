"""嵌入式迁移器 TDD：打开任意账套 DB 时零感知补齐表/列（抗漂移）。

背景事故类（见 test_migrations.py）：XErp 演进只 ADD COLUMN / CREATE TABLE，
但 ``create_all`` 仅建缺失表、不补旧表缺失列；老账套（旧模型建表）复制到新机器
直接 ``no such column`` 500。本文件钉死 ``kernel.migrate.ensure_schema_current``：

1. 全新库经 ensure_schema_current 后所有模型表/列齐全；
2. 旧库（手工建的老结构、无 alembic_version、且已填充数据行）经 ensure_schema_current
   补齐缺失列且不抛异常（NOT NULL 无默认列自动补 DEFAULT，已填充行安全）；
3. 重复调用幂等（再跑一遍不报错）；
4. xerp_meta.schema_version 记录到当前 SCHEMA_VERSION。
"""

import os
import sqlite3
import tempfile

import pytest

from kernel.migrate import SCHEMA_VERSION, ensure_schema_current, read_schema_version


def _columns(db_path: str, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _tables(db_path: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        con.close()


def _legacy_db(path: str, *, with_rows: bool) -> None:
    """模拟真实 dev 库：create_all 之前的老结构 + 已填充数据行（可选）。"""
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE ledger_sets (
        id VARCHAR(32) PRIMARY KEY, name VARCHAR(64), accounting_standard VARCHAR(32))"""
    )
    con.execute(
        """CREATE TABLE vouchers (
        id VARCHAR(32) PRIMARY KEY, ledger_set_id VARCHAR(32),
        period_id VARCHAR(32), voucher_no VARCHAR(32), voucher_date VARCHAR(10),
        status VARCHAR(16), summary VARCHAR(200), created_by VARCHAR(32),
        idempotency_key VARCHAR(64), created_at TIMESTAMP, posted_at TIMESTAMP)"""
    )
    con.execute(
        """CREATE TABLE subjects (
        id VARCHAR(32) PRIMARY KEY, type VARCHAR(16), display_name VARCHAR(64),
        created_at TIMESTAMP)"""
    )
    con.execute(
        """CREATE TABLE accounts (
        id VARCHAR(32) PRIMARY KEY, ledger_set_id VARCHAR(32), code VARCHAR(32),
        name VARCHAR(64), direction VARCHAR(8), category VARCHAR(16),
        parent_code VARCHAR(32), aux_dim_defs JSON, is_leaf BOOLEAN)"""
    )
    con.execute(
        """CREATE TABLE voucher_lines (
        id VARCHAR(32) PRIMARY KEY, voucher_id VARCHAR(32), line_no INTEGER,
        account_id VARCHAR(32), debit NUMERIC(18,2), credit NUMERIC(18,2),
        summary VARCHAR(500), aux_dims JSON)"""
    )
    con.execute(
        """CREATE TABLE parties (
        id VARCHAR(32) PRIMARY KEY, ledger_set_id VARCHAR(32), party_type VARCHAR(16),
        name VARCHAR(200), aux_attrs JSON)"""
    )
    if with_rows:
        con.execute("INSERT INTO ledger_sets VALUES ('ls1','测试','small_business')")
        con.execute("INSERT INTO vouchers VALUES ('v1','ls1','p1','001','2026-08-01','DRAFT','摘要','u1',NULL,NULL,NULL)")
        con.execute("INSERT INTO subjects VALUES ('u1','user','老板',NULL)")
        con.execute("INSERT INTO accounts VALUES ('a1','ls1','1001','现金','debit','asset',NULL,NULL,1)")
        # 已填充的 voucher_lines 行：验证 NOT NULL 无默认列（foreign_debit 等）补齐不崩
        con.execute("INSERT INTO voucher_lines VALUES ('l1','v1',1,'a1',100,0,'摘要',NULL)")
        con.execute("INSERT INTO parties VALUES ('pt1','ls1','customer','客户甲',NULL)")
    con.commit()
    con.close()


def test_fresh_db_has_all_model_tables_and_columns():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "fresh.db")
        res = ensure_schema_current(f"sqlite:///{db}")
        assert res["schema_version"] == SCHEMA_VERSION
        assert "added_columns" in res
        # 关键模型表均由迁移器提供
        assert _tables(db) >= {
            "ledger_sets", "vouchers", "subjects", "accounts",
            "voucher_lines", "parties", "arap_clearing",
            "inventory_items", "asset_cards", "xerp_meta",
        }
        # 关键列齐全（与 test_migrations 同口径）
        assert {"required_signers", "signatures"} <= _columns(db, "vouchers")
        assert "attrs" in _columns(db, "accounts")
        assert {"daily_voucher_limit", "quota_currency", "external_ref"} <= _columns(db, "subjects")
        _vl = _columns(db, "voucher_lines")
        assert {"currency", "fx_rate", "foreign_debit", "foreign_credit",
                "quantity", "unit"} <= _vl
        assert _columns(db, "arap_clearing")
        assert "credit_limit" in _columns(db, "parties")
        assert _columns(db, "inventory_items")
        assert _columns(db, "asset_cards")
        # version 已记录
        assert read_schema_version(f"sqlite:///{db}") == str(SCHEMA_VERSION)


def test_legacy_empty_db_upgrade_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        _legacy_db(db, with_rows=False)
        pre = read_schema_version(f"sqlite:///{db}")
        assert pre is None  # 老库无版本记录
        ensure_schema_current(f"sqlite:///{db}")
        # 升级后列齐全
        assert {"required_signers", "signatures"} <= _columns(db, "vouchers")
        assert {"daily_voucher_limit", "quota_currency", "external_ref"} <= _columns(db, "subjects")
        assert "attrs" in _columns(db, "accounts")
        _vl = _columns(db, "voucher_lines")
        assert {"currency", "fx_rate", "foreign_debit", "foreign_credit",
                "quantity", "unit"} <= _vl
        assert _columns(db, "arap_clearing")
        assert "credit_limit" in _columns(db, "parties")
        assert _columns(db, "inventory_items")
        assert _columns(db, "asset_cards")
        # 幂等：再跑一遍不抛异常
        ensure_schema_current(f"sqlite:///{db}")
        assert read_schema_version(f"sqlite:///{db}") == str(SCHEMA_VERSION)


def test_legacy_populated_db_upgrade_not_null_columns_safe():
    """已填充旧表的 NOT NULL 无默认列（foreign_debit/foreign_credit）必须能补齐。"""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy_pop.db")
        _legacy_db(db, with_rows=True)
        # 升级前确认老表缺这些列
        assert "foreign_debit" not in _columns(db, "voucher_lines")
        # 升级不应抛错（NOT NULL 无默认列自动补 DEFAULT 0）
        ensure_schema_current(f"sqlite:///{db}")
        assert "foreign_debit" in _columns(db, "voucher_lines")
        assert "foreign_credit" in _columns(db, "voucher_lines")
        # 已填充行被安全回填（默认值 0）
        con = sqlite3.connect(db)
        try:
            fd = con.execute("SELECT foreign_debit FROM voucher_lines WHERE id='l1'").fetchone()[0]
        finally:
            con.close()
        assert fd == 0
        assert read_schema_version(f"sqlite:///{db}") == str(SCHEMA_VERSION)
