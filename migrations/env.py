from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from accalab.db import Base, database_url
import accalab.models  # noqa: F401


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
migration_url = os.getenv("DATABASE_URL")
if not migration_url:
    migration_url = database_url()
config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        if provided_connection.dialect.name == "sqlite":
            provided_connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            provided_connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        context.configure(
            connection=provided_connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        provided_connection.commit()
        return

    connectable = create_engine(migration_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
