"""Глобальный перехват необработанных ошибок в хендлерах.

Без него любое неожиданное исключение просто улетает в лог aiogram, а юзер
остаётся с «вечно крутящейся» кнопкой и без единого слова — хуже всего это
в денежных сценариях, где непонятно, прошла операция или нет.

Здесь мы ничего не «чиним»: транзакцию уже откатил session_scope. Задача —
внятно ответить в интерфейсе и дать админам сигнал с деталями, чтобы баг
не жил незамеченным.
"""
from __future__ import annotations

import html

from aiogram import Bot, Router
from aiogram.types import ErrorEvent
from loguru import logger

from bot.config import settings

router = Router(name="errors")

_USER_TEXT = (
    "⚠️ Что-то пошло не так на нашей стороне. Операция не выполнена — "
    "попробуй ещё раз через минуту.\n"
    "Если повторяется, напиши в поддержку: меню → «🆘 Поддержка»."
)


async def _alert_admins(bot: Bot, event: ErrorEvent, who: int | None) -> None:
    """Короткий сигнал админам: что упало и у кого. Тихо гаснет при сбое."""
    exc = event.exception
    text = (
        "❌ <b>Необработанная ошибка</b>\n"
        f"Тип: <code>{html.escape(type(exc).__name__)}</code>\n"
        f"Текст: <code>{html.escape(str(exc)[:300])}</code>\n"
        f"Юзер: <code>{who}</code>"
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


@router.errors()
async def on_unhandled_error(event: ErrorEvent, bot: Bot) -> bool:
    """Ловит всё, что не поймали хендлеры. True = апдейт считаем обработанным
    (иначе aiogram будет ретраить его и спамить теми же ошибками)."""
    update = event.update
    callback = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)
    who = None

    logger.exception("Unhandled error while processing update {}", getattr(update, "update_id", "?"))

    try:
        if callback is not None:
            who = callback.from_user.id if callback.from_user else None
            # Сначала гасим «часики» на кнопке, затем поясняем текстом.
            await callback.answer("Ошибка, попробуй ещё раз", show_alert=True)
        elif message is not None:
            who = message.from_user.id if message.from_user else None
            await message.answer(_USER_TEXT)
    except Exception:
        logger.warning("Failed to notify user about unhandled error")

    await _alert_admins(bot, event, who)
    return True
