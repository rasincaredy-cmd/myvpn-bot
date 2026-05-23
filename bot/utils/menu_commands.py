from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from bot.config import settings

_BASE_COMMANDS = [
    BotCommand(command="start", description="🚀 Запуск / главное меню"),
    BotCommand(command="menu", description="📋 Показать меню"),
    BotCommand(command="help", description="🆘 Помощь"),
    BotCommand(command="exit", description="✖️ Отменить текущее действие"),
]

_ADMIN_EXTRA = [
    BotCommand(command="install", description="🛠 Установить VPN на VPS"),
    BotCommand(command="servers", description="🖥 Мои серверы"),
    BotCommand(command="newpeer", description="➕ Создать peer"),
    BotCommand(command="invite", description="🎟 Создать инвайт"),
]


async def set_bot_commands(bot: Bot) -> None:
    """Регистрирует выпадающее меню команд (синий «/» в Telegram)."""
    await bot.set_my_commands(
        _BASE_COMMANDS,
        scope=BotCommandScopeAllPrivateChats(),
    )
    # Админам дополнительно показываем админ-команды.
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                _BASE_COMMANDS + _ADMIN_EXTRA,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            # Админ ещё не открывал чат с ботом — Telegram вернёт ошибку.
            # Игнорируем, скоуп проставится после первого /start.
            pass
