from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.models import Peer, Server

# --- Callback prefixes --------------------------------------------------------
# Формат: "<ns>:<action>[:<arg>]"
CB_MENU = "menu"
CB_INSTALL = "install"
CB_SERVERS = "srv"
CB_PEERS = "peer"
CB_INVITES = "inv"
CB_ADMIN = "adm"          # admin-панель: управление пирами любого юзера
CB_PANEL = "pnl"   # admin-панель
CB_WDTT = "wdtt"   # обход белых списков (wdtt / proxy-turn-vk)
CB_DEVICE = "dev"  # устройства (Блок 9)
CB_SUB = "sub"     # подписка (Блок 9)
CB_NOP = "nop"
CB_CANCEL = "cancel"


# --- Главное меню -------------------------------------------------------------

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="🛡 Обход БС", callback_data=f"{CB_WDTT}:my")
    kb.button(text="🎫 Моя подписка", callback_data=f"{CB_SUB}:my")
    kb.button(text="🌍 Локации", callback_data=f"{CB_MENU}:locations")
    # У админа то же меню, что у юзера, плюс ОДНА кнопка — вход в админ-панель.
    # Всё управление сервисом (установка VPN, серверы, выдача конфигов/инвайтов)
    # живёт внутри панели, а не на главном экране.
    if is_admin:
        kb.button(text="👮 Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.button(text="🔔 Оповещения", callback_data=f"{CB_MENU}:notify")
    kb.button(text="🆘 Поддержка", callback_data=f"{CB_MENU}:help")
    kb.adjust(1)
    return kb.as_markup()


def notify_settings_kb(enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if enabled:
        kb.button(text="🔕 Выключить предупреждения", callback_data=f"{CB_MENU}:notify_toggle")
    else:
        kb.button(text="🔔 Включить предупреждения", callback_data=f"{CB_MENU}:notify_toggle")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


# --- Установка ----------------------------------------------------------------

def install_auth_method() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗝 Пароль", callback_data=f"{CB_INSTALL}:auth:password")
    kb.button(text="🔑 SSH-ключ", callback_data=f"{CB_INSTALL}:auth:key")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(2, 1)
    return kb.as_markup()


def install_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=f"{CB_INSTALL}:run")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(2)
    return kb.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    return kb.as_markup()


# --- Серверы ------------------------------------------------------------------

def servers_list(servers: list[Server]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in servers:
        kb.button(text=f"🖥 {s.name} ({s.status})", callback_data=f"{CB_SERVERS}:open:{s.id}")
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def server_card(server_id: int, wdtt_enabled: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать peer",  callback_data=f"{CB_PEERS}:new:{server_id}")
    kb.button(text="🎟 Инвайт",        callback_data=f"{CB_INVITES}:new:{server_id}")
    kb.button(text="👥 Peers сервера", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.button(text="🛡 Обходы сервера", callback_data=f"{CB_SERVERS}:wdtt:{server_id}")
    kb.button(text="📋 Инвайты",       callback_data=f"{CB_INVITES}:list:{server_id}")
    kb.button(text="📊 Трафик",        callback_data=f"{CB_SERVERS}:traffic:{server_id}")
    kb.button(text="🖥 Состояние",     callback_data=f"{CB_SERVERS}:stats:{server_id}")
    kb.button(text="🌍 Локация",       callback_data=f"{CB_SERVERS}:loc:{server_id}")
    kb.button(text="🌐 DNS",           callback_data=f"{CB_SERVERS}:dns:{server_id}")
    # Тумблер доступности обхода БС на сервере (выдачу юзеры делают сами).
    kb.button(
        text="🛡 Обход БС: ВКЛ" if wdtt_enabled else "🛡 Обход БС: выкл",
        callback_data=f"{CB_WDTT}:toggle:{server_id}",
    )
    kb.button(text="🗑 Удалить", callback_data=f"{CB_SERVERS}:del:{server_id}")
    kb.button(text="« К списку", callback_data=f"{CB_SERVERS}:list")
    kb.adjust(2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def server_wdtt_list_kb(
    rows: list[tuple[int, str]], server_id: int  # (access_id, label)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for access_id, label in rows:
        kb.button(text=f"🛡 {label}", callback_data=f"{CB_SERVERS}:wopen:{access_id}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def server_wdtt_card_kb(access_id: int, server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Отозвать", callback_data=f"{CB_SERVERS}:wdel:{access_id}:{server_id}")
    kb.button(text="« К обходам", callback_data=f"{CB_SERVERS}:wdtt:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def traffic_nav(server_id: int, has_orphans: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=f"{CB_SERVERS}:traffic:{server_id}")
    if has_orphans:
        kb.button(text="🧹 Убрать лишние", callback_data=f"{CB_SERVERS}:cleanup:{server_id}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def stats_nav(server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить",  callback_data=f"{CB_SERVERS}:stats:{server_id}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(2)
    return kb.as_markup()
    

def confirm_delete_server(server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❗️ Да, удалить", callback_data=f"{CB_SERVERS}:del_ok:{server_id}")
    kb.button(text="« Назад", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def pick_server(servers: list[Server], action_prefix: str) -> InlineKeyboardMarkup:
    """action_prefix — что произойдёт при клике, например 'peer:pick' или 'inv:pick'."""
    kb = InlineKeyboardBuilder()
    for s in servers:
        kb.button(text=f"🖥 {s.name}", callback_data=f"{action_prefix}:{s.id}")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(1)
    return kb.as_markup()


def invites_list_kb(
    rows: list[tuple[int, str, str]],  # (invite_id, icon, label) — уже срез страницы
    server_id: int,
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for invite_id, icon, label in rows:
        kb.button(
            text=f"{icon} {label}",
            callback_data=f"{CB_INVITES}:open:{invite_id}",
        )
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_INVITES}:list:{server_id}:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_INVITES}:list:{server_id}:{page + 1}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def invite_card_kb(
    invite_id: int, server_id: int, can_revoke: bool, used: bool = False
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_revoke:
        kb.button(text="🗑 Отозвать", callback_data=f"{CB_INVITES}:del:{invite_id}")
    elif used:
        # Использованный инвайт: пир выдан отдельно, а запись висит в истории —
        # даём убрать её.
        kb.button(text="🗑 Удалить из истории", callback_data=f"{CB_INVITES}:del:{invite_id}")
    kb.button(text="« К инвайтам", callback_data=f"{CB_INVITES}:list:{server_id}")
    kb.adjust(1)
    return kb.as_markup()

# --- Peers (пользовательский вид) --------------------------------------------

def peers_list(
    peers: list[tuple[int, str, str, str]],
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """peers: list of (peer_id, label, server_name, status) — уже срез страницы."""
    kb = InlineKeyboardBuilder()
    for pid, label, server_name, status in peers:
        mark = "✅" if status == "active" else "🚫"
        kb.button(
            text=f"{mark} {label} @ {server_name}",
            callback_data=f"{CB_PEERS}:open:{pid}",
        )
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_PEERS}:list:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_PEERS}:list:{page + 1}")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def peer_card(peer_id: int, can_revoke: bool, can_send: bool) -> InlineKeyboardMarkup:
    # Удаление пира из БД — только у админа (adm:delete). У пользователя его нет:
    # отозванный пир «ждёт» возможного возобновления и чистится планировщиком.
    kb = InlineKeyboardBuilder()
    if can_send:
        kb.button(text="📥 Получить конфиг", callback_data=f"{CB_PEERS}:send:{peer_id}")
    if can_revoke:
        kb.button(text="🗑 Отозвать", callback_data=f"{CB_PEERS}:revoke:{peer_id}")
    kb.button(text="« К списку", callback_data=f"{CB_PEERS}:list")
    kb.adjust(1)
    return kb.as_markup()


# --- Admin: управление пирами любого юзера -----------------------------------

def server_peers_admin(
    peers: list[Peer],
    server_id: int,
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """Список пиров сервера для админа (уже срез страницы) — каждый пир кликабелен."""
    kb = InlineKeyboardBuilder()
    for p in peers:
        mark = "✅" if p.status == "active" else "🚫"
        kb.button(
            text=f"{mark} {p.label} ({p.ip})",
            callback_data=f"{CB_ADMIN}:peer:{p.id}",
        )
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_SERVERS}:peers:{server_id}:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_SERVERS}:peers:{server_id}:{page + 1}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_peer_card(peer_id: int, server_id: int, can_revoke: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_revoke:
        kb.button(text="📥 Получить конфиг", callback_data=f"{CB_ADMIN}:conf:{peer_id}")
        kb.button(text="🗑 Отозвать",         callback_data=f"{CB_ADMIN}:revoke:{peer_id}")
    else:
        kb.button(text="♻️ Возобновить",   callback_data=f"{CB_ADMIN}:revive:{peer_id}")
        kb.button(text="❌ Удалить из БД", callback_data=f"{CB_ADMIN}:delete:{peer_id}")
    # Переименование доступно всегда — это просто метка в БД, не трогает конфиг.
    kb.button(text="✏️ Переименовать", callback_data=f"{CB_ADMIN}:rename:{peer_id}")
    kb.button(text="« К пирам", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


# --- Admin panel -------------------------------------------------------------

def admin_panel_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛠 Установить VPN на VPS", callback_data=f"{CB_INSTALL}:start")
    kb.button(text="🖥 Серверы",               callback_data=f"{CB_SERVERS}:list")
    kb.button(text="📊 Статистика",   callback_data=f"{CB_PANEL}:stats")
    kb.button(text="👤 Пользователи", callback_data=f"{CB_PANEL}:users:0")
    kb.button(text="📢 Рассылка",     callback_data=f"{CB_PANEL}:broadcast")
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


def admin_user_bypass_card_kb(access_id: int, user_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
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


def user_card_kb(user_id: int, is_blocked: bool, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Устройства", callback_data=f"{CB_PANEL}:udev:{user_id}:{page}")
    kb.button(text="🛡 Обходы БС",  callback_data=f"{CB_PANEL}:ubp:{user_id}:{page}")
    kb.button(text="🎫 Подписка",   callback_data=f"{CB_PANEL}:sub:{user_id}:{page}")
    if is_blocked:
        kb.button(text="✅ Разблокировать", callback_data=f"{CB_PANEL}:unblock:{user_id}:{page}")
    else:
        kb.button(text="🚫 Заблокировать",  callback_data=f"{CB_PANEL}:block:{user_id}:{page}")
    kb.button(text="« К списку", callback_data=f"{CB_PANEL}:users:{page}")
    kb.adjust(2, 1, 1, 1)
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


# --- Устройства + подписка (Блок 9) ------------------------------------------

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
    kb.button(text="🎫 Подписка", callback_data=f"{CB_SUB}:my")
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
    # Удаление доступно всегда: активное устройство удаляется (с отзывом), а
    # неактивное (истекшее) — убирается из списка, чтобы не висело мусором.
    kb.button(text="🗑 Удалить устройство", callback_data=f"{CB_DEVICE}:revoke:{device_id}")
    kb.button(text="« К устройствам", callback_data=f"{CB_DEVICE}:list")
    kb.adjust(1)
    return kb.as_markup()


def subscription_kb(has_devices_slot: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def admin_sub_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Лимит устройств", callback_data=f"{CB_PANEL}:sub_lim:{user_id}:{page}")
    kb.button(text="🛡 Лимит обхода БС",  callback_data=f"{CB_PANEL}:sub_bp:{user_id}:{page}")
    kb.button(text="📅 Задать срок",     callback_data=f"{CB_PANEL}:sub_ext:{user_id}:{page}")
    kb.button(text="📊 Лимит трафика",   callback_data=f"{CB_PANEL}:sub_trf:{user_id}:{page}")
    kb.button(text="🚫 Отключить (срок в 0)", callback_data=f"{CB_PANEL}:sub_off:{user_id}:{page}")
    kb.button(text="« К пользователю",   callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


# --- Обход белых списков (wdtt) ----------------------------------------------

def wdtt_days_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text_, days in [("30 дней", 30), ("90 дней", 90), ("180 дней", 180),
                        ("Год", 365), ("Бессрочно", 0)]:
        kb.button(text=text_, callback_data=f"{CB_WDTT}:days:{days}")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def wdtt_vk_choice_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Ссылка сервиса", callback_data=f"{CB_WDTT}:vk:svc")
    kb.button(text="🔗 Своя VK-ссылка", callback_data=f"{CB_WDTT}:vk:own")
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


def wdtt_list_kb(
    rows: list[tuple[int, str, str]],  # (access_id, mark, label) — срез страницы
    server_id: int,
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for access_id, mark, label in rows:
        kb.button(text=f"{mark} {label}", callback_data=f"{CB_WDTT}:open:{access_id}")
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_WDTT}:list:{server_id}:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_WDTT}:list:{server_id}:{page + 1}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def wdtt_card_kb(access_id: int, server_id: int, can_revoke: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Показать ссылку", callback_data=f"{CB_WDTT}:link:{access_id}")
    if can_revoke:
        kb.button(text="🗑 Отозвать", callback_data=f"{CB_WDTT}:revoke:{access_id}")
    kb.button(text="« К доступам", callback_data=f"{CB_WDTT}:list:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def wdtt_user_list_kb(
    rows: list[tuple[int, str, str, str]],  # (access_id, mark, label, server_name)
    can_create: bool = True,
    page: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for access_id, mark, label, server_name in rows:
        kb.button(
            text=f"{mark} {label} @ {server_name}",
            callback_data=f"{CB_WDTT}:myopen:{access_id}",
        )
    if can_create:
        kb.button(text="➕ Создать доступ обхода", callback_data=f"{CB_WDTT}:new")
    if has_prev:
        kb.button(text="← Назад",  callback_data=f"{CB_WDTT}:my:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →", callback_data=f"{CB_WDTT}:my:{page + 1}")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def wdtt_user_card_kb(access_id: int, can_get: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_get:
        kb.button(text="🔗 Получить ссылку", callback_data=f"{CB_WDTT}:mylink:{access_id}")
        kb.button(text="🗑 Удалить", callback_data=f"{CB_WDTT}:myrevoke:{access_id}")
    kb.button(text="« К списку", callback_data=f"{CB_WDTT}:my")
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
