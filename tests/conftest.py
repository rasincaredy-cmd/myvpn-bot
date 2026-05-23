"""
Общая настройка тестов.

ВАЖНО: env-переменные выставляем ДО любого `from bot.*` импорта,
иначе pydantic-settings упадёт из-за отсутствия BOT_TOKEN/ENCRYPTION_KEY.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "BOT_TOKEN", "1234567890:dummy_token_for_tests_xxxxxxxxxxxxxxxxxxxxxxxx"
)
os.environ.setdefault("ADMIN_IDS", "111,222")
# Валидный 32-байтовый Fernet-ключ (base64), сгенерированный одноразово для тестов.
os.environ.setdefault(
    "ENCRYPTION_KEY", "YlNtN1JqU3pzQk1VYUVRYVNVMUx0M2NubTBkTllxOEU="
)
# In-memory БД, чтобы не трогать ./data/
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.db.base import Base


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Свежий in-memory engine на каждый тест — никакого общего state."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        # Импорт здесь, чтобы модели зарегистрировались в metadata.
        from bot.db import models  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s

    await engine.dispose()
