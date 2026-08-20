"""Тест авто-миграций: недостающие колонки добавляются в существующую таблицу."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db import models  # noqa: F401 — регистрирует таблицы в Base.metadata
from bot.db.base import Base
from bot.db.migrate import run_migrations

# Импорт `models` выше обязателен и не является лишним: без него
# `Base.metadata.tables` пуст, create_all не создаёт ничего, миграции нечего
# добавлять — и тест проходил только когда модели успевал импортировать
# какой-нибудь другой тест-модуль. В одиночку `pytest tests/test_migrate.py`
# падал (найдено 20.08.2026).


@pytest.mark.asyncio
async def test_adds_missing_columns_to_existing_table() -> None:
    """Симулируем старую БД: таблица peers без новых колонок трафика."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # 1. Создаём урезанную таблицу peers — как в старой версии, без
    #    traffic_used_bytes / traffic_last_raw_bytes.
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE peers ("
            "id INTEGER PRIMARY KEY, server_id INTEGER, user_id INTEGER, "
            "label VARCHAR, ip VARCHAR, public_key VARCHAR, "
            "private_key_enc BLOB, status VARCHAR, created_at DATETIME"
            ")"
        ))
        # Запись со старой схемой — миграция не должна её потерять.
        await conn.execute(text(
            "INSERT INTO peers (id, server_id, user_id, label, ip, public_key, status) "
            "VALUES (1, 1, 1, 'old', '10.8.0.2', 'PUBKEY', 'active')"
        ))

    # 2. Прогоняем create_all (создаст прочие таблицы) + миграции.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await run_migrations(conn)

    # 3. Новые колонки на месте, старая запись цела, дефолт = 0.
    async with engine.connect() as conn:
        cols = await conn.run_sync(
            lambda sc: {c["name"] for c in inspect(sc).get_columns("peers")}
        )
        assert "traffic_used_bytes" in cols
        assert "traffic_last_raw_bytes" in cols
        assert "expires_at" in cols
        assert "traffic_limit_bytes" in cols

        row = (await conn.execute(text(
            "SELECT label, traffic_used_bytes, traffic_last_raw_bytes "
            "FROM peers WHERE id = 1"
        ))).one()
        assert row.label == "old"
        assert row.traffic_used_bytes == 0
        assert row.traffic_last_raw_bytes == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_second_run() -> None:
    """Повторный прогон миграций на актуальной схеме — без ошибок и изменений."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await run_migrations(conn)
        await run_migrations(conn)  # второй раз — no-op
    await engine.dispose()


class TestIndexes:
    """`ALTER TABLE ADD COLUMN` не создаёт индексы — их надо добирать отдельно.

    Поймано на `users.ref_code` (Блок «Рефка», 20.08.2026): уникальность была
    объявлена в модели, а в боевой базе её бы просто не существовало, и два
    человека заняли бы одно имя реферальной ссылки.
    """

    @pytest.mark.asyncio
    async def test_missing_unique_index_is_created(self) -> None:
        from sqlalchemy import inspect, text
        from sqlalchemy.ext.asyncio import create_async_engine

        from bot.db.base import Base
        from bot.db.migrate import run_migrations

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Имитируем боевую базу: индекс уронили, колонка осталась.
            await conn.execute(text("DROP INDEX IF EXISTS ix_users_ref_code"))
            names = await conn.run_sync(
                lambda c: {i["name"] for i in inspect(c).get_indexes("users")}
            )
            assert "ix_users_ref_code" not in names, "индекс не удалился, тест бессмыслен"

            await run_migrations(conn)

            names = await conn.run_sync(
                lambda c: {i["name"] for i in inspect(c).get_indexes("users")}
            )
            assert "ix_users_ref_code" in names
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_duplicate_rows_do_not_break_startup(self) -> None:
        """Уникальный индекс поверх дублей не создастся — но старт бота из-за
        этого падать не должен: без индекса он работает, а дубли разбирает
        человек."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from bot.db.base import Base
        from bot.db.migrate import run_migrations

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("DROP INDEX IF EXISTS ix_users_ref_code"))
            # NOT NULL-поля заполняем явно: в боевой базе их проставляет
            # ORM, а здесь мы пишем в обход неё.
            await conn.execute(text(
                "INSERT INTO users (tg_id, ref_code, is_admin, is_vip, is_blocked) "
                "VALUES (9001, 'dup', 0, 0, 0), (9002, 'dup', 0, 0, 0)"
            ))
            await run_migrations(conn)  # не должно бросить
        await engine.dispose()
