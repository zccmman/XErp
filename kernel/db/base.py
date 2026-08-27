"""SQLAlchemy 声明基类与通用类型。"""

from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase

# 双方言 JSON：SQLite 测试用 JSON，PostgreSQL 生产用 JSONB（ADR-001 测试回退条款）
JSONVariant = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass
