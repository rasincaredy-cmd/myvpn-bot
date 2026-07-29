"""Вход в админ-панель (/admin) и ручной бэкап."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from bot.keyboards.inline import CB_PANEL, admin_panel_menu
from bot.loader import bot as tg_bot

router = Router(name="admin_entry")


@router.message(Command("admin"))
@router.callback_query(F.data == f"{CB_PANEL}:main")
async def cmd_admin(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = "👮 <b>Админ-панель</b>"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=admin_panel_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=admin_panel_menu())


@router.callback_query(F.data == f"{CB_PANEL}:backup_now")
async def cb_panel_backup(call: CallbackQuery) -> None:
    """Ручной бэкап: тот же архив, что ночной, — всем админам. Ночной маркер
    не трогаем: регулярный бэкап должен идти своим расписанием."""
    from bot.services import backup as backup_svc

    if not backup_svc.enabled():
        await call.answer(
            "Бэкап выключен: задай BACKUP_PASSWORD в .env и перезапусти бота. "
            "Пароль сохрани и вне сервера!",
            show_alert=True,
        )
        return
    await call.answer("Собираю бэкап…")
    try:
        filename = await backup_svc.send_backup_to_admins()
        logger.info("Manual backup sent: {}", filename)
    except Exception as exc:
        logger.exception("Manual backup failed")
        await tg_bot.send_message(
            call.message.chat.id, f"❌ Бэкап не получился: {exc}"
        )
