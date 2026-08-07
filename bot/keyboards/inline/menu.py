"""Главное меню, оповещения и общая навигация («В меню», «К серверу»)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import (
    CB_BAL,
    CB_DEVICE,
    CB_LEGAL,
    CB_MENU,
    CB_PANEL,
    CB_SERVERS,
    CB_SUB,
    CB_WDTT,
)


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    """Главные действия — во всю ширину и цветом, справочные разделы — парами.
    Цвет кнопок (style) поддерживается с Bot API 9.4; старые клиенты просто
    покажут обычные кнопки."""
    from bot.config import settings

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list",
              style="primary")
    kb.button(text="⚡ Резервное подключение", callback_data=f"{CB_WDTT}:my")
    kb.button(text="🎫 Моя подписка", callback_data=f"{CB_SUB}:my")
    kb.button(text="💰 Баланс", callback_data=f"{CB_BAL}:my")
    kb.button(text="💳 Тарифы", callback_data=f"{CB_LEGAL}:tariffs")
    kb.button(text="🌍 Локации", callback_data=f"{CB_MENU}:locations")
    kb.button(text="🔔 Оповещения", callback_data=f"{CB_MENU}:notify")
    kb.button(text="🆘 Поддержка", callback_data=f"{CB_MENU}:help")

    sizes = [1, 1, 2, 2, 2]

    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
        sizes.append(1)
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
        sizes.append(1)
    if is_admin:
        kb.button(text="👮 Админ-панель", callback_data=f"{CB_PANEL}:main")
        sizes.append(1)

    kb.adjust(*sizes)
    return kb.as_markup()


def onboarding_hint_kb() -> InlineKeyboardMarkup:
    """Одна кнопка под подсказкой новому юзеру: сразу к добавлению устройства
    (cb_dev_add сам проверит подписку/лимит/наличие локаций)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить устройство", callback_data=f"{CB_DEVICE}:add")
    return kb.as_markup()


def notify_settings_kb(enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if enabled:
        kb.button(text="🔕 Выключить оповещения", callback_data=f"{CB_MENU}:notify_toggle")
    else:
        kb.button(text="🔔 Включить оповещения", callback_data=f"{CB_MENU}:notify_toggle")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


# --- Навигация ----------------------------------------------------------------

def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    return kb.as_markup()


def to_server(server_id: int) -> InlineKeyboardMarkup:
    """Кнопка возврата на карточку сервера (после создания peer/инвайта)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    return kb.as_markup()
