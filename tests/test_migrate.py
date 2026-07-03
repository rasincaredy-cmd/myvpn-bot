"""Тест авто-миграций: недостающие колонки добавляются в существующую таблицу."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db.base import Base
from bot.db.migrate import run_migrations


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
