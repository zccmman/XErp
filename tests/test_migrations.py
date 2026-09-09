"""迁移链 TDD：旧库升级路径必须可用。

背景事故（2026-09-09 真机）：G1 多级签字给 Voucher 加了
required_signers/signatures 两列但漏写迁移——测试库每次 create_all
全新建表，掩盖了老库升级路径；dev 库进账套直接 500
（no such column: vouchers.required_signers）。

本文件钉死两件事：
1. 全新库 alembic upgrade head 到最新 revision，关键模型列齐全；
2. 旧库（只有 0001-0003 结构、alembic_version 为空）经
   stamp + upgrade 补齐 0004-0006 的列，升级是幂等的。
"""

import os
import sqlite3
import tempfile

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic_upgrade(db_url: str, target: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(_REPO, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO, "migrations"))
    os.environ["XERP_DB"] = db_url
    command.upgrade(cfg, target)


def _alembic_stamp(db_url: str, target: str) -> None:
    """只写版本号不执行迁移——用于「表结构已手工建好」的老库。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(_REPO, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO, "migrations"))
    os.environ["XERP_DB"] = db_url
    command.stamp(cfg, target)


def _columns(db_path: str, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_fresh_upgrade_head_has_all_model_columns():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "fresh.db")
        _alembic_upgrade(f"sqlite:///{db}")
        v = _columns(db, "vouchers")
        assert {"required_signers", "signatures"} <= v, "G1 签字列必须由迁移提供"
        assert "attrs" in _columns(db, "accounts"), "阶段1本体属性列"
        assert {"daily_voucher_limit", "quota_currency"} <= _columns(db, "subjects")
        con = sqlite3.connect(db)
        try:
            ver = con.execute("select version_num from alembic_version").fetchone()[0]
        finally:
            con.close()
        assert ver == "0006_voucher_signers"


def test_legacy_db_upgrade_is_idempotent():
    """模拟真实 dev 库：create_all 建的旧结构 + 空 alembic_version。"""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        con = sqlite3.connect(db)
        # 旧版模型（G1 签字列 / 0004 额度列 / 0005 attrs 列加入之前）
        con.execute(
            """CREATE TABLE vouchers (
            id VARCHAR(32) PRIMARY KEY, ledger_set_id VARCHAR(32),
            period_id VARCHAR(32), voucher_no VARCHAR(32),
            voucher_date VARCHAR(10), status VARCHAR(16), summary VARCHAR(200),
            created_by VARCHAR(32), idempotency_key VARCHAR(64),
            created_at TIMESTAMP, posted_at TIMESTAMP)"""
        )
        con.execute(
            """CREATE TABLE subjects (
            id VARCHAR(32) PRIMARY KEY, type VARCHAR(16),
            display_name VARCHAR(64), created_at TIMESTAMP)"""
        )
        con.execute(
            """CREATE TABLE accounts (
            id VARCHAR(32) PRIMARY KEY, ledger_set_id VARCHAR(32),
            code VARCHAR(32), name VARCHAR(64), direction VARCHAR(8),
            category VARCHAR(16), parent_code VARCHAR(32),
            aux_dim_defs JSON, is_leaf BOOLEAN)"""
        )
        con.commit()
        con.close()
        # 表结构等价于 0001-0003 已生效：stamp 0003 跳过建表迁移
        _alembic_stamp(f"sqlite:///{db}", "0003")
        _alembic_upgrade(f"sqlite:///{db}")  # 0004-0006
        assert {"required_signers", "signatures"} <= _columns(db, "vouchers")
        assert {"daily_voucher_limit", "quota_currency"} <= _columns(db, "subjects")
        assert "attrs" in _columns(db, "accounts")
        # 再跑一遍 head：幂等，不抛异常
        _alembic_upgrade(f"sqlite:///{db}")
