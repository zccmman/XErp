"""XErp 嵌入式迁移器（零依赖、抗漂移）。

设计铁律（寄生 WB · 极简部署 · 数据自持）
------------------------------------------------
- 不依赖 alembic CLI / 不依赖任何宿主；纯 ``sqlalchemy`` + sqlite ``PRAGMA``，
  随内核分发，复制目录即复制账套、打开即升级。
- 打开任意账套 DB 时调用 :func:`ensure_schema_current`，按 ORM 元数据补齐：
    1. 缺失表  → ``Base.metadata.create_all``（幂等，仅建不存在的表）；
    2. 缺失列  → ``ALTER TABLE ... ADD COLUMN``（幂等，按元数据逐列补齐）。
- 抗漂移：老库经 ``create_all`` 预建了部分表/列、``alembic_version`` 落后于真值，
  也能安全补齐、不抛错——根除「老库进账套 500 / no such column」事故类
  （见 ``tests/test_migrations.py``、``tests/test_embedded_migrate.py``）。
- SQLite 只增不改：XErp 演进历史仅 ``ADD COLUMN`` / ``CREATE TABLE``，
  绝不做 ``DROP`` / ``ALTER TYPE``，故补齐策略天然安全。
- 记录 ``schema_version`` 到 ``xerp_meta`` 表，供 ``doctor`` 诊断「升级前→后」。

调用点（全在内核，零宿主依赖）
------------------------------
- ``integration/workbuddy/xerp_project.py``：``init`` / ``open_session`` / ``doctor``
- ``kernel/webapp.py``：``build_app``（Web 逃生舱首启）
"""

from __future__ import annotations

import re

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.sqlite import dialect as _SQLiteDialect
from sqlalchemy.schema import CreateColumn

from kernel.db.base import Base
from kernel.db import models  # noqa: F401  触发 ORM 元数据注册（R1 红线：仅 import 内核）

__all__ = ["SCHEMA_VERSION", "ensure_schema_current", "read_schema_version"]

# 与 alembic head（0012_inventory_asset）对齐：模型发生结构变更时 +1。
SCHEMA_VERSION = 12


def _strip_add_column_constraints(ddl: str) -> tuple[str, bool]:
    """把 CREATE COLUMN 的 DDL 削成 ADD COLUMN 可用形态。

    去掉 SQLite ``ALTER TABLE ADD COLUMN`` 不支持/不需要的约束：
    ``REFERENCES``（FK，SQLite 默认关闭外键，去掉等价）、``PRIMARY KEY``、
    ``UNIQUE``、``NOT NULL``。保留 ``DEFAULT``。返回 (ddl, 原是否 NOT NULL)。
    """
    ddl = re.sub(r"\s+REFERENCES\s+\w+(?:\s*\(\s*\w+\s*\))?", "", ddl, flags=re.I)
    ddl = re.sub(r"\s+PRIMARY KEY", "", ddl, flags=re.I)
    ddl = re.sub(r"\s+UNIQUE", "", ddl, flags=re.I)
    had_not_null = bool(re.search(r"\s+NOT NULL", ddl, flags=re.I))
    ddl = re.sub(r"\s+NOT NULL", "", ddl, flags=re.I)
    return ddl.strip(), had_not_null


def _type_default(col) -> str:
    """NOT NULL 且无 server_default 的列，在已填充旧表上 ADD 必须给默认。"""
    t = str(col.type).upper()
    if any(k in t for k in ("INT", "NUM", "FLOAT", "DECIMAL", "REAL", "BOOLEAN", "BOOL")):
        return "0"
    return "''"


def ensure_schema_current(db_url: str) -> dict:
    """打开账套 DB 时调用：补齐缺失表/列，写 schema_version。幂等、抗漂移。

    返回 {schema_version, added_columns, db_url}。
    """
    engine = create_engine(db_url)
    added: list[str] = []
    # 全程复用同一个连接：避免连接池在 Windows 上残留文件句柄导致锁
    with engine.connect() as conn:
        # 1) 补齐缺失表（create_all 仅建不存在的表，绝不触碰已有表结构）
        Base.metadata.create_all(conn)

        # 2) 确保自述元数据表存在（不在 ORM 元数据内，手工维护）
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS xerp_meta (k VARCHAR(64) PRIMARY KEY, v TEXT)")
        )

        inspector = inspect(conn)
        for table in Base.metadata.sorted_tables:
            tname = table.name
            if not inspector.has_table(tname):
                continue  # create_all 已建
            existing = {c["name"] for c in inspector.get_columns(tname)}
            for col in table.columns:
                if col.name in existing:
                    continue
                full = str(CreateColumn(col).compile(dialect=_SQLiteDialect()))
                ddl, had_not_null = _strip_add_column_constraints(full)
                if had_not_null and col.server_default is None:
                    # 已填充旧表上 NOT NULL 必须有默认，否则 ALTER 失败
                    ddl += " DEFAULT " + _type_default(col)
                conn.execute(text(f"ALTER TABLE {tname} ADD COLUMN {ddl}"))
                added.append(f"{tname}.{col.name}")

        # 3) 写当前 schema 版本（供 doctor 诊断升级前后）
        conn.execute(
            text(
                "INSERT INTO xerp_meta(k, v) VALUES('schema_version', :v) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v"
            ),
            {"v": str(SCHEMA_VERSION)},
        )
        conn.commit()

    # 释放连接池文件句柄（Windows 文件锁 / 临时目录清理需要）
    engine.dispose()
    return {"schema_version": SCHEMA_VERSION, "added_columns": added, "db_url": db_url}


def read_schema_version(db_url: str) -> str | None:
    """读取账套 DB 当前已记录的 schema_version（未升级过 / 表不存在返回 None）。"""
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            try:
                row = conn.execute(
                    text("SELECT v FROM xerp_meta WHERE k='schema_version'")
                ).fetchone()
            except Exception:
                # xerp_meta 尚未建（老版 init 建的库 / 从未迁移过）→ 视为未记录
                return None
            return row[0] if row else None
    finally:
        engine.dispose()
