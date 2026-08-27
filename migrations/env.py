"""Alembic 环境：URL 优先取 LEDGEROS_DB 环境变量。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from kernel.db import models  # noqa: F401  确保模型注册
from kernel.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    import os

    return os.environ.get("LEDGEROS_DB", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
