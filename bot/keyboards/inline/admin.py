"""Админ-панель: меню, рассылка, списки и карточки юзеров, подписка."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_INSTALL, CB_MENU, CB_PANEL, CB_SERVERS


def admin_panel_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛠 Установить VPN на VPS", callback_data=f"{CB_INSTALL}:start")
    kb.button(text="🖥 Серверы",               callback_data=f"{CB_SERVERS}:list")
    kb.button(text="📊 Статистика",   callback_data=f"{CB_PANEL}:stats")
    kb.button(text="👤 Пользователи", callback_data=f"{CB_PANEL}:users:0")
    kb.button(text="📢 Рассылка",     callback_data=f"{CB_PANEL}:broadcast")
    kb.button(text="📦 Бэкап сейчас", callback_data=f"{CB_PANEL}:backup_now")
    kb.button(text="« В меню",        callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def broadcast_target_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Все", callback_data=f"{CB_PANEL}:bc_to:all")
    kb.button(text="✅ С активной подпиской", callback_data=f"{CB_PANEL}:bc_to:active")
    kb.button(text="⌛ Без активной подписки", callback_data=f"{CB_PANEL}:bc_to:inactive")
    kb.button(text="✍️ Выбрать вручную", callback_data=f"{CB_PANEL}:bc_to:manual")
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def broadcast_select_kb(
    rows: list[tuple[int, bool, str]],  # (user_id, checked, name)
    selected_count: int,
    page: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for uid, checked, name in rows:
        mark = "☑️" if checked else "⬜"
        kb.button(text=f"{mark} {name}", callback_data=f"{CB_PANEL}:bc_sel:{uid}:{page}")
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_PANEL}:bc_selpg:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_PANEL}:bc_selpg:{page + 1}")
    kb.button(text=f"✅ Готово ({selected_count})", callback_data=f"{CB_PANEL}:bc_seldone")
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Разослать", callback_data=f"{CB_PANEL}:bc_send")
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:main")
    kb.adjust(2)
    return kb.as_markup()


def back_to_panel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    return kb.as_markup()


def admin_user_items_kb(
    rows: list[tuple[int, str, str]],  # (item_id, mark, label)
    kind: str,                          # "udev" | "ubp"
    user_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    """Список устройств/обходов юзера в админке. kind → open-callback."""
    kb = InlineKeyboardBuilder()
    for item_id, mark, label in rows:
        kb.button(text=f"{mark} {label}", callback_data=f"{CB_PANEL}:{kind}o:{item_id}:{user_id}:{page}")
    kb.button(text="« К пользователю", callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


def admin_user_device_card_kb(
    device_id: int,
    user_id: int,
    page: int,
    configs: list[tuple[int, str]] | None = None,  # (peer_id, loc_label)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for peer_id, loc in (configs or []):
        kb.button(text=f"📥 {loc}", callback_data=f"{CB_PANEL}:ucfg:{peer_id}:{user_id}:{page}:{device_id}")
    kb.button(text="🗑 Удалить устройство", callback_data=f"{CB_PANEL}:udevx:{device_id}:{user_id}:{page}")
    kb.button(text="« К устройствам", callback_data=f"{CB_PANEL}:udev:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


def admin_user_bypass_card_kb(
    access_id: int, user_id: int, page: int, is_active: bool = True
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # Ссылка обхода админу (Блок «Мелочи»): симметрично «📄 Конфиг» у устройств —
    # поддержке нужно видеть ровно то, что у юзера на руках.
    if is_active:
        kb.button(text="🔗 Ссылка обхода", callback_data=f"{CB_PANEL}:ubpl:{access_id}:{user_id}:{page}")
        kb.button(text="🗑 Отозвать доступ", callback_data=f"{CB_PANEL}:ubpx:{access_id}:{user_id}:{page}")
    kb.button(text="« К обходам", callback_data=f"{CB_PANEL}:ubp:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


def users_list_kb(
    users: list, page: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    from datetime import datetime, timezone

    def _icon(u) -> str:
        if u.is_blocked:
            return "🔴"
        if u.is_admin:
            return "👑"
        exp = u.sub_expires_at
        exp_aware = exp if (exp is None or exp.tzinfo) else exp.replace(tzinfo=timezone.utc)
        active = exp is None or exp_aware > datetime.now(timezone.utc)
        if not active:
            return "💤"  # без активной подписки
        if u.is_trial and exp is not None:
            return "🎁"  # триал
        return "💎"  # платная

    kb = InlineKeyboardBuilder()
    for u in users:
        name = (f"@{u.username}" if u.username else None) or u.full_name or f"id{u.tg_id}"
        kb.button(text=f"{_icon(u)} {name}", callback_data=f"{CB_PANEL}:user:{u.id}:{page}")
    if has_prev:
        kb.button(text="← Назад",   callback_data=f"{CB_PANEL}:users:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →",  callback_data=f"{CB_PANEL}:users:{page + 1}")
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def user_card_kb(
    user_id: int, is_blocked: bool, page: int, is_vip: bool = False
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Устройства", callback_data=f"{CB_PANEL}:udev:{user_id}:{page}")
    kb.button(text="🛡 Обходы БС",  callback_data=f"{CB_PANEL}:ubp:{user_id}:{page}")
    kb.button(text="🎫 Подписка",   callback_data=f"{CB_PANEL}:sub:{user_id}:{page}")
    # «Друг» видит приватные серверы (Server.is_private).
    kb.button(
        text="⭐ Друг: ВКЛ" if is_vip else "⭐ Друг: выкл",
        callback_data=f"{CB_PANEL}:vip:{user_id}:{page}",
    )
    if is_blocked:
        kb.button(text="✅ Разблокировать", callback_data=f"{CB_PANEL}:unblock:{user_id}:{page}")
    else:
        kb.button(text="🚫 Заблокировать",  callback_data=f"{CB_PANEL}:block:{user_id}:{page}")
    kb.button(text="🗑 Стереть из БД", callback_data=f"{CB_PANEL}:udel:{user_id}:{page}")
    kb.button(text="« К списку", callback_data=f"{CB_PANEL}:users:{page}")
    kb.adjust(2, 1, 2, 1, 1)
    return kb.as_markup()


def admin_sub_give_kb(
    user_id: int, page: int, terms: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """Выбор срока выдаваемой подписки. terms: (месяцы, подпись со сроком и
    ценой) — цену показываем, чтобы админ видел, на какую сумму дарит."""
    kb = InlineKeyboardBuilder()
    for months, label in terms:
        kb.button(text=label, callback_data=f"{CB_PANEL}:sub_gdo:{user_id}:{page}:{months}")
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:sub:{user_id}:{page}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def back_to_users_kb(page: int) -> InlineKeyboardMarkup:
    """Возврат после удаления юзера (Блок «Мелочи 2»): карточки уже нет, а
    выкидывать в корень админ-панели неудобно — админ чаще всего чистит список
    дальше. Поэтому первым делом «К пользователям», панель — вторым."""
    kb = InlineKeyboardBuilder()
    kb.button(text="« К пользователям", callback_data=f"{CB_PANEL}:users:{page}")
    kb.button(text="👮 Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def user_wipe_confirm_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    """Двухшаговое подтверждение уничтожения юзера (Блок «Ревизия»)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="❗️ Да, стереть безвозвратно", callback_data=f"{CB_PANEL}:udelc:{user_id}:{page}")
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


def cancel_to_sub_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    """«Отмена» из любого ввода в настройках подписки юзера (Блок «Мелочи»).
    Ведёт обратно в карточку подписки; хендлер `panel:sub:` чистит FSM, иначе
    следующее сообщение админа улетело бы в брошенный step-хендлер."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:sub:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


def admin_sub_kb(user_id: int, page: int, is_trial: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Лимит устройств", callback_data=f"{CB_PANEL}:sub_lim:{user_id}:{page}")
    kb.button(text="🛡 Лимит обхода БС",  callback_data=f"{CB_PANEL}:sub_bp:{user_id}:{page}")
    kb.button(text="📅 Задать срок",     callback_data=f"{CB_PANEL}:sub_ext:{user_id}:{page}")
    # Выдача готового тарифного срока (1/3/6/12 мес) — в отличие от «Задать
    # срок» с произвольной датой, юзер получает ровно то же, что купил бы сам,
    # и автопродление дальше берёт этот же срок.
    kb.button(text="🎫 Выдать подписку",  callback_data=f"{CB_PANEL}:sub_give:{user_id}:{page}")
    kb.button(text="📊 Лимит трафика",   callback_data=f"{CB_PANEL}:sub_trf:{user_id}:{page}")
    kb.button(text="💰 Баланс ±",        callback_data=f"{CB_PANEL}:sub_bal:{user_id}:{page}")
    # Триал раздаётся автоматически при регистрации и повторно не выдаётся —
    # кнопка (Блок «Мелочи 2») нужна для тестов и «выдай ещё раз» вручную.
    kb.button(
        text="🎁 Триал заново" if is_trial else "🎁 Выдать триал",
        callback_data=f"{CB_PANEL}:sub_trl:{user_id}:{page}",
    )
    kb.button(text="🚫 Отключить (срок в 0)", callback_data=f"{CB_PANEL}:sub_off:{user_id}:{page}")
    kb.button(text="« К пользователю",   callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()
