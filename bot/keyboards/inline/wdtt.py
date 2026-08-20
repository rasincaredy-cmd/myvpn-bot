"""Резервное подключение (wdtt): выбор VK-ссылки, платформы, список и карточка."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_BAL, CB_CANCEL, CB_MENU, CB_WDTT


def wdtt_vk_choice_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Рекомендуемый вариант", callback_data=f"{CB_WDTT}:vk:svc")
    kb.button(text="🔗 Свой адрес", callback_data=f"{CB_WDTT}:vk:own")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(1)
    return kb.as_markup()


def wdtt_platform_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Android", callback_data=f"{CB_WDTT}:plat:android")
    kb.button(text="🍏 iOS",     callback_data=f"{CB_WDTT}:plat:ios")
    kb.button(text="💻 ПК",      callback_data=f"{CB_WDTT}:plat:pc")
    kb.button(text="✖️ Отмена",  callback_data=CB_CANCEL)
    kb.adjust(3, 1)
    return kb.as_markup()


def wdtt_user_list_kb(
    rows: list[tuple[int, str, str, str]],  # (access_id, mark, label, server_name)
    can_create: bool = True,
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
    offer_tariff: bool = False,  # в тарифе ноль подключений — предложить добавить
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for access_id, mark, label, server_name in rows:
        kb.button(
            text=f"{mark} {label} @ {server_name}",
            callback_data=f"{CB_WDTT}:myopen:{access_id}",
        )
    if can_create:
        kb.button(text="➕ Добавить", callback_data=f"{CB_WDTT}:new")
    elif offer_tariff:
        # Текст говорил «добавь в подписке», а кнопки не было: человека
        # отправляли искать раздел руками (Блок «Тариф»).
        kb.button(text="⚙️ Сменить тариф", callback_data=f"{CB_BAL}:extend",
                  style="primary")
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_WDTT}:my:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_WDTT}:my:{page + 1}")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def wdtt_user_card_kb(access_id: int, can_get: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_get:
        kb.button(text="🔗 Получить ссылку", callback_data=f"{CB_WDTT}:mylink:{access_id}")
        # Отвязка от устройства — рядом со ссылкой, а не в поддержке: человек с
        # новым телефоном видит от приложения «неверный пароль» и первым делом
        # идёт именно сюда, за ссылкой.
        # Подпись короткая: длинная обрезается на телефоне многоточием, и
        # человек не видит, что кнопка вообще про смену устройства.
        kb.button(text="📱 Сменить устройство",
                  callback_data=f"{CB_WDTT}:myunbind:{access_id}")
        kb.button(text="🗑 Удалить", callback_data=f"{CB_WDTT}:myrevoke:{access_id}",
                  style="danger")
    kb.button(text="‹ Подключения", callback_data=f"{CB_WDTT}:my")
    kb.adjust(1)
    return kb.as_markup()


def wdtt_pick_device_kb(devices: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """devices: (device_id, label). Выбор устройства, под которое создаётся обход."""
    kb = InlineKeyboardBuilder()
    for device_id, label in devices:
        kb.button(text=f"📱 {label}", callback_data=f"{CB_WDTT}:dev:{device_id}")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(1)
    return kb.as_markup()


def back_to_bypasses_kb() -> InlineKeyboardMarkup:
    """Возврат после удаления своего резервного подключения — в список доступов,
    а не в меню (Блок «Мелочи 2»). То же, что back_to_devices_kb у устройств."""
    kb = InlineKeyboardBuilder()
    kb.button(text="‹ Подключения", callback_data=f"{CB_WDTT}:my")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()
