"""append-only 数据库层强制。

SQLite（测试环境）：BEFORE UPDATE/DELETE 触发器 RAISE(ABORT)。
PostgreSQL（生产）：等价触发器由 migrations/versions/0002 迁移创建（plpgsql）。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

_TMPL = """
CREATE TRIGGER IF NOT EXISTS {table}_no_{op}
BEFORE {op} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only ({op} blocked)');
END;
"""


def install_append_only_sqlite(engine: Engine, table: str = "events") -> None:
    """在 SQLite 上为指定表安装 append-only 触发器（幂等）。"""
    with engine.begin() as conn:
        for op in ("UPDATE", "DELETE"):
            conn.execute(text(_TMPL.format(table=table, op=op)))
