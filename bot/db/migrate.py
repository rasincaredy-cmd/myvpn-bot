"""Лёгкие авто-миграции схемы: добавляет недостающие колонки в существующие таблицы.

`Base.metadata.create_all` создаёт только отсутствующие *таблицы*, но не умеет
добавлять новые *колонки* в уже существующие. Поэтому при обновлении бота
(например, добавили поле в Peer) в боевой базе колонки не появятся и бот упадёт.

Здесь мы сравниваем модели с фактической схемой и делаем `ALTER TABLE ADD COLUMN`
для недостающих колонок. Идемпотентно и безопасно для боевой базы; работает на
SQLite и Postgres. Добавляем только nullable-колонки или колонки с DEFAULT —
`ADD COLUMN NOT NULL` без значения по умолчанию невозможен для непустой таблицы.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from bot.db.base import Base


def _default_sql(column) -> str | None:
    """Достаёт текст server_default колонки ('0', 'now()' и т.п.), если он задан."""
    sd = column.server_default
    if sd is None:
        return None
    arg = getattr(sd, "arg", None)
    if arg is None:
        return None
    text_attr = getattr(arg, "text", None)  # server_default="0" → TextClause
    if text_attr is not None:
        return str(text_attr)
    if isinstance(arg, str):
        return arg
    return None


def _column_ddl(dialect, column) -> str:
    coltype = column.type.compile(dialect=dialect)
    ddl = f"{column.name} {coltype}"
    default_sql = _default_sql(column)
    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
        if not column.nullable:
            ddl += " NOT NULL"
    return ddl


async def run_migrations(conn: AsyncConnection) -> None:
    """Добавляет недостающие колонки во все таблицы из Base.metadata."""

    def _existing_columns(sync_conn) -> dict[str, set[str]]:
        insp = inspect(sync_conn)
        found: dict[str, set[str]] = {}
        for table_name in Base.metadata.tables:
            if insp.has_table(table_name):
                found[table_name] = {c["name"] for c in insp.get_columns(table_name)}
        return found

    existing = await conn.run_sync(_existing_columns)
    dialect = conn.dialect

    for table_name, table in Base.metadata.tables.items():
        have = existing.get(table_name)
        if have is None:
            continue  # таблицы нет вовсе — create_all создаст её целиком

        for column in table.columns:
            if column.name in have or column.primary_key:
                continue
            # ADD COLUMN безопасен только для nullable или с DEFAULT.
            if not column.nullable and _default_sql(column) is None:
                logger.warning(
                    "Пропускаю ADD COLUMN {}.{}: NOT NULL без DEFAULT — добавь вручную",
                    table_name, column.name,
                )
                continue
            ddl = _column_ddl(dialect, column)
            logger.info("Миграция: ALTER TABLE {} ADD COLUMN {}", table_name, ddl)
            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))
