from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from bot.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.db_url,
    echo=False,
    future=True,
)

SessionMaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db() -> None:
    # Импорт здесь, чтобы модели зарегистрировались в Base.metadata.
    from bot.db import models  # noqa: F401
    from bot.db.migrate import run_migrations

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all создаёт недостающие таблицы, но не колонки — добираем их.
        await run_migrations(conn)

    # Грандфазер (Блок 9): существующие активные пиры → устройства. Идемпотентно.
    from loguru import logger
    from bot.db.repo import backfill_devices
    async with session_scope() as session:
        n = await backfill_devices(session)
    if n:
        logger.info("Backfill: обёрнуто пиров в устройства: {}", n)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionMaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
