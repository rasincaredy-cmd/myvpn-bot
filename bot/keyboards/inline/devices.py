"""Устройства юзера и его подписка (Блок 9)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_BAL, CB_DEVICE, CB_MENU, CB_SUB, CB_WDTT


def devices_list_kb(
    rows: list[tuple[int, str, str]],  # (device_id, mark, label) — срез страницы
    used: int,
    limit: int,
    can_add: bool,
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for device_id, mark, label in rows:
        kb.button(text=f"{mark} {label}", callback_data=f"{CB_DEVICE}:open:{device_id}")
    if can_add:
        kb.button(text=f"➕ Добавить устройство ({used}/{limit})", callback_data=f"{CB_DEVICE}:add")
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_DEVICE}:list:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_DEVICE}:list:{page + 1}")
    # Имя кнопки везде одно — «🎫 Моя подписка» (как в главном меню): юзер должен
    # находить раздел по тому же названию, что видит в текстах.
    kb.button(text="🎫 Моя подписка", callback_data=f"{CB_SUB}:my")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def device_card_kb(
    device_id: int,
    can_get: bool,
    can_revoke: bool,
    locations: list[tuple[int, str]] | None = None,  # (peer_id, loc_label)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_get:
        locs = locations or []
        if len(locs) > 1:
            # Несколько локаций → кнопка на каждую + «получить все» разом.
            for peer_id, loc in locs:
                kb.button(text=f"📥 {loc}", callback_data=f"{CB_DEVICE}:send1:{peer_id}")
            kb.button(text="📥 Получить все", callback_data=f"{CB_DEVICE}:send:{device_id}")
        else:
            kb.button(text="📥 Получить конфиг", callback_data=f"{CB_DEVICE}:send:{device_id}")
    # Переименование — только метка в БД, конфиги не трогает (Блок «Ревизия»).
    kb.button(text="✏️ Переименовать", callback_data=f"{CB_DEVICE}:ren:{device_id}")
    # Удаление доступно всегда: активное устройство удаляется (с отзывом), а
    # неактивное (истекшее) — убирается из списка, чтобы не висело мусором.
    kb.button(text="🗑 Удалить устройство", callback_data=f"{CB_DEVICE}:revoke:{device_id}")
    kb.button(text="« К устройствам", callback_data=f"{CB_DEVICE}:list")
    kb.adjust(1)
    return kb.as_markup()


def subscription_kb(
    *,
    can_pay: bool = False,       # показать «Продлить» (Crypto Pay включён)
    autopay: bool | None = None,  # None — тумблер не показывать (нет смысла без оплаты)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_pay:
        kb.button(text="🔁 Продлить / купить", callback_data=f"{CB_BAL}:extend")
    if autopay is not None:
        kb.button(
            text="♻️ Автопродление: ВКЛ" if autopay else "♻️ Автопродление: выкл",
            callback_data=f"{CB_BAL}:autopay",
        )
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def device_created_kb() -> InlineKeyboardMarkup:
    """После создания устройства: текст t.device_created отсылает к «🛡 Обход БС» —
    даём кнопку прямо здесь, а не заставляем идти через меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Обход БС", callback_data=f"{CB_WDTT}:my")
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def back_to_devices_kb() -> InlineKeyboardMarkup:
    """Возврат после удаления своего устройства (Блок «Мелочи 2»): в список
    устройств, а не в главное меню — юзер обычно удаляет и сразу смотрит, что
    осталось."""
    kb = InlineKeyboardBuilder()
    kb.button(text="« Мои устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()
