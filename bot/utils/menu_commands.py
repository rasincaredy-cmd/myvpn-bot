from __future__ import annotations

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from bot.config import settings

_BASE_COMMANDS = [
    BotCommand(command="start", description="🚀 Запуск / главное меню"),
    BotCommand(command="menu", description="📋 Показать меню"),
    BotCommand(command="help", description="🆘 Поддержка"),
]

# Блок «Ревизия»: /servers (не имел хендлера) и /newpeer (легаси-выдача вне
# подписочной модели) убраны — всё живёт в /admin.
_ADMIN_EXTRA = [
    BotCommand(command="admin", description="👮 Админ-панель"),
    BotCommand(command="exit", description="✖️ Отменить текущее действие"),
    BotCommand(command="install", description="🛠 Установить VPN на VPS"),
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


async def set_menu_button(bot: Bot) -> None:
    """Кнопка «☰» рядом с полем ввода: открыть мини-приложение.

    Ставится один раз на всех: у Telegram эта кнопка — штатное место для
    мини-приложений, и человек ищет её именно там. Адрес не задан — возвращаем
    кнопку в исходное состояние, иначе выключенное настройкой приложение
    продолжало бы открываться у всех, кто уже видел кнопку.
    """
    from loguru import logger

    try:
        if settings.miniapp_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Приложение",
                    web_app=WebAppInfo(url=settings.miniapp_url),
                )
            )
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception:
        # Кнопка — украшение: без неё приложение открывается из «⚙️ Ещё», и
        # ронять из-за неё запуск бота нельзя.
        logger.exception("Кнопка мини-приложения не установилась")
