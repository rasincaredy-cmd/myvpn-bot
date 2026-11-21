"""Вход в админ-панель (/admin) и ручной бэкап."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from bot.keyboards.inline import CB_MENU, CB_PANEL
from bot.loader import bot as tg_bot
from bot.texts import ui

router = Router(name="admin_entry")


@router.message(Command("admin", "admin_web", "web", "panel_web", "panel"))
@router.callback_query(F.data.in_({f"{CB_PANEL}:main", f"{CB_PANEL}:web_link"}))
async def cmd_admin_web_entry(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Вход в админ-панель: выдача одноразовой защищённой magic-ссылки в веб-панель."""
    await state.clear()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.config import settings
    from bot.webadmin.auth import create_magic_login_token

    user_id = event.from_user.id
    if user_id not in settings.admin_ids:
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ ограничен", show_alert=True)
        return

    token = create_magic_login_token(user_id)
    base = settings.miniapp_url.rstrip("/") if settings.miniapp_url else "http://localhost:8080"
    if "/app" in base:
        base = base.replace("/app", "")
    web_url = f"{base}/admin?token={token}"

    from bot.utils.validators import get_lan_ip, is_valid_button_url

    lan_ip = get_lan_ip()
    port = settings.webhook_port or 8080
    lan_url = f"http://{lan_ip}:{port}/admin?token={token}"

    is_public = is_valid_button_url(web_url)

    text = (
        "🌐 <b>Панель управления Moschata VPN</b>\n\n"
        "Управление ботом переведено в браузерную веб-панель.\n"
        "В панели доступны: интерактивная таблица пользователей, управление серверами и пирами, журнал аудита и статистика.\n\n"
        "<b>Ссылка для входа на ПК:</b>\n"
        f"<code>{web_url}</code>\n\n"
        "<b>Ссылка для входа с телефона (в домашней Wi-Fi сети):</b>\n"
        f"<code>{lan_url}</code>\n\n"
        "<i>Ссылки одноразовые и действуют 30 минут. Нажми на ссылку, чтобы скопировать.</i>"
    )

    buttons = []
    if is_public:
        buttons.append([InlineKeyboardButton(text="🚀 Открыть веб-панель", url=web_url)])
    buttons.append([InlineKeyboardButton(text="‹ Меню", callback_data=f"{CB_MENU}:open")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


# Алиас для обратной совместимости импортов
cmd_admin = cmd_admin_web_entry




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
