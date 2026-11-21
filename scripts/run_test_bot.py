"""Скрипт запуска изолированного тестового бота с веб-панелью и подписками.

Запуск:
    .venv\\Scripts\\python scripts/run_test_bot.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import os
import sys

# 1. Загружаем test_bot.env ДО любых импортов bot.*
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

env_path = ROOT_DIR / "test_bot.env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path, override=True)
    print(f"[*] Загружен конфиг тестового бота: {env_path.name}")
else:
    print(f"[!] Файл {env_path} не найден!")

from bot.config import settings
from bot.db.base import init_db, session_scope
from bot.db.models import Server, ServerStatus
from bot.services.crypto import encrypt
from bot.services.amnezia import AmneziaParams
from sqlalchemy import select


async def seed_test_server():
    """Создает тестовый сервер в тестовой БД, если его еще нет."""
    async with session_scope() as session:
        existing = (await session.execute(select(Server))).scalars().first()
        if existing is None:
            params = AmneziaParams(
                jc=4, jmin=50, jmax=1000,
                s1=0, s2=0,
                h1=1, h2=2, h3=3, h4=4
            )
            test_server = Server(
                name="🇳🇱 Нидерланды (Тест)",
                host="48.102.173.96",
                ssh_port=22,
                ssh_user="root",
                ssh_password_enc=encrypt("dummy_password"),
                wg_port=585,
                wg_interface="awg0",
                wg_subnet="10.8.0.0/24",
                server_public_key="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb=",
                server_endpoint="48.102.173.96:585",
                awg_params_json=params.to_json(),
                status=ServerStatus.READY,
                owner_tg_id=0,
                location="🇳🇱 Нидерланды",
                dns="1.1.1.1, 1.0.0.1",
                wdtt_enabled=True,
                wdtt_ports="56000,56001,9000",
                max_peers=50,
                wdtt_max_accesses=20,
                is_private=False,
            )
            session.add(test_server)
            await session.commit()
            print("[*] Добавлен демонстрационный сервер в тестовую базу.")


async def start():
    print("=" * 60)
    print("🚀 ЗАПУСК ТЕСТОВОГО БОТА И ВЕБ-ПАНЕЛИ УПРАВЛЕНИЯ")
    print(f"Токен бота: {settings.bot_token[:15]}...")
    print(f"База данных: {settings.db_url}")
    print(f"Веб-порт: {settings.webhook_port}")
    print("=" * 60)

    from bot.__main__ import _setup_logging, _on_startup
    from bot.loader import bot, dp

    _setup_logging()
    await _on_startup()
    await seed_test_server()

    dev_auth_token = settings.encryption_key[:16]
    print("\n" + "✨" * 30)
    print(f"🌐 Браузерная админ-панель: http://127.0.0.1:{settings.webhook_port}/admin?dev_auth={dev_auth_token}")
    print(f"🔗 Ссылки подписок: http://127.0.0.1:{settings.webhook_port}/sub/<sub_token>/info")
    print("✨" * 30 + "\n")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(start())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Тестовый бот остановлен.")
