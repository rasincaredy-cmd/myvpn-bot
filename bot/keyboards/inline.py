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
CB_NOP = "nop"
CB_CANCEL = "cancel"


# --- Главное меню -------------------------------------------------------------

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📁 Мои конфиги", callback_data=f"{CB_PEERS}:list")
    if is_admin:
        kb.button(text="🛠 Установить VPN на VPS", callback_data=f"{CB_INSTALL}:start")
        kb.button(text="🖥 Мои серверы",           callback_data=f"{CB_SERVERS}:list")
        kb.button(text="➕ Выдать конфиг peer",    callback_data=f"{CB_PEERS}:new")
        kb.button(text="🎟 Создать инвайт",        callback_data=f"{CB_INVITES}:new")
        kb.button(text="👮 Админ-панель",          callback_data=f"{CB_PANEL}:main")
    kb.button(text="🔔 Оповещения", callback_data=f"{CB_MENU}:notify")
    kb.button(text="🆘 Помощь", callback_data=f"{CB_MENU}:help")
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
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def server_card(server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать peer",  callback_data=f"{CB_PEERS}:new:{server_id}")
    kb.button(text="🎟 Инвайт",        callback_data=f"{CB_INVITES}:new:{server_id}")
    kb.button(text="👥 Peers сервера", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.button(text="📋 Инвайты",       callback_data=f"{CB_INVITES}:list:{server_id}")
    kb.button(text="📊 Трафик",        callback_data=f"{CB_SERVERS}:traffic:{server_id}")
    kb.button(text="🖥 Состояние",     callback_data=f"{CB_SERVERS}:stats:{server_id}")
    kb.button(text="🗑 Удалить",       callback_data=f"{CB_SERVERS}:del:{server_id}")
    kb.button(text="« К списку",       callback_data=f"{CB_SERVERS}:list")
    kb.adjust(2, 2, 2, 1, 1)
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
    rows: list[tuple[int, str, str]],  # (invite_id, icon, label)
    server_id: int,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for invite_id, icon, label in rows:
        kb.button(
            text=f"{icon} {label}",
            callback_data=f"{CB_INVITES}:open:{invite_id}",
        )
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

def peers_list(peers: list[tuple[int, str, str, str]]) -> InlineKeyboardMarkup:
    """peers: list of (peer_id, label, server_name, status)."""
    kb = InlineKeyboardBuilder()
    for pid, label, server_name, status in peers:
        mark = "✅" if status == "active" else "🚫"
        kb.button(
            text=f"{mark} {label} @ {server_name}",
            callback_data=f"{CB_PEERS}:open:{pid}",
        )
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

def server_peers_admin(peers: list[Peer], server_id: int) -> InlineKeyboardMarkup:
    """Список пиров сервера для админа — каждый пир кликабелен."""
    kb = InlineKeyboardBuilder()
    for p in peers:
        mark = "✅" if p.status == "active" else "🚫"
        kb.button(
            text=f"{mark} {p.label} ({p.ip})",
            callback_data=f"{CB_ADMIN}:peer:{p.id}",
        )
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_peer_card(peer_id: int, server_id: int, can_revoke: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_revoke:
        kb.button(text="📥 Получить конфиг", callback_data=f"{CB_ADMIN}:conf:{peer_id}")
        kb.button(text="⚙️ Лимиты",          callback_data=f"{CB_ADMIN}:limits:{peer_id}")
        kb.button(text="🗑 Отозвать",         callback_data=f"{CB_ADMIN}:revoke:{peer_id}")
    else:
        kb.button(text="♻️ Возобновить",   callback_data=f"{CB_ADMIN}:revive:{peer_id}")
        kb.button(text="❌ Удалить из БД", callback_data=f"{CB_ADMIN}:delete:{peer_id}")
    kb.button(text="« К пирам", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.adjust(1)
    return kb.as_markup()

def peer_limits_kb(peer_id: int, has_limits: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Срок действия",  callback_data=f"{CB_ADMIN}:set_exp:{peer_id}")
    kb.button(text="📊 Лимит трафика",  callback_data=f"{CB_ADMIN}:set_trf:{peer_id}")
    if has_limits:
        kb.button(text="🗑 Сбросить лимиты", callback_data=f"{CB_ADMIN}:clr_lim:{peer_id}")
    kb.button(text="« К пиру", callback_data=f"{CB_ADMIN}:peer:{peer_id}")
    kb.adjust(1)
    return kb.as_markup()


# --- Admin panel -------------------------------------------------------------

def admin_panel_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика",   callback_data=f"{CB_PANEL}:stats")
    kb.button(text="👤 Пользователи", callback_data=f"{CB_PANEL}:users:0")
    kb.button(text="📢 Рассылка",     callback_data=f"{CB_PANEL}:broadcast")
    kb.button(text="« В меню",        callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def back_to_panel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    return kb.as_markup()


def users_list_kb(
    users: list, page: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in users:
        icon = "🔴" if u.is_blocked else ("👑" if u.is_admin else "👤")
        name = (f"@{u.username}" if u.username else None) or u.full_name or f"id{u.tg_id}"
        kb.button(text=f"{icon} {name}", callback_data=f"{CB_PANEL}:user:{u.id}:{page}")
    if has_prev:
        kb.button(text="← Назад",   callback_data=f"{CB_PANEL}:users:{page - 1}")
    if has_next:
        kb.button(text="Вперёд →",  callback_data=f"{CB_PANEL}:users:{page + 1}")
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def user_card_kb(user_id: int, is_blocked: bool, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_blocked:
        kb.button(text="✅ Разблокировать", callback_data=f"{CB_PANEL}:unblock:{user_id}:{page}")
    else:
        kb.button(text="🚫 Заблокировать",  callback_data=f"{CB_PANEL}:block:{user_id}:{page}")
    kb.button(text="« К списку", callback_data=f"{CB_PANEL}:users:{page}")
    kb.adjust(1)
    return kb.as_markup()


# --- Навигация ----------------------------------------------------------------

def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    return kb.as_markup()
