"""Вход в админ-панель (/admin) и ручной бэкап."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from bot.keyboards.inline import CB_PANEL, admin_panel_menu
from bot.loader import bot as tg_bot
from bot.texts import ui

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


@router.message(Command("admin_web", "web", "panel_web"))
async def cmd_admin_web(message: Message) -> None:
    """Генерация одноразовой ссылки для входа в веб-панель управления администратора."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.config import settings
    from bot.webadmin.auth import create_magic_login_token

    admin_id = message.from_user.id
    if admin_id not in settings.admin_ids:
        return

    token = create_magic_login_token(admin_id)
    base = settings.miniapp_url.rstrip("/") if settings.miniapp_url else "http://localhost:8080"
    if "/app" in base:
        base = base.replace("/app", "")
    web_url = f"{base}/admin?token={token}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Открыть панель управления", url=web_url)]
        ]
    )
    await message.answer(
        "🌐 <b>Браузерная панель управления Moschata VPN</b>\n\n"
        "Одноразовая ссылка для безопасного входа (действует 30 минут):\n"
        f"<code>{web_url}</code>\n\n"
        "<i>В веб-панели доступны: интерактивная таблица пользователей, управление пирами и серверами, логи аудита и статистика.</i>",
        reply_markup=kb,
    )



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
            call.message.chat.id, f"❌ Бэкап не получился: {ui.safe(exc)}"
        )
