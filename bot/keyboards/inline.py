from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.models import Server

# --- Callback prefixes --------------------------------------------------------
# Формат: "<ns>:<action>[:<arg>]"
CB_MENU = "menu"
CB_INSTALL = "install"
CB_SERVERS = "srv"
CB_PEERS = "peer"
CB_INVITES = "inv"
CB_NOP = "nop"
CB_CANCEL = "cancel"


# --- Главное меню -------------------------------------------------------------

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📁 Мои конфиги", callback_data=f"{CB_PEERS}:list")
    if is_admin:
        kb.button(text="🛠 Установить VPN на VPS", callback_data=f"{CB_INSTALL}:start")
        kb.button(text="🖥 Мои серверы", callback_data=f"{CB_SERVERS}:list")
        kb.button(text="➕ Выдать конфиг peer", callback_data=f"{CB_PEERS}:new")
        kb.button(text="🎟 Создать инвайт", callback_data=f"{CB_INVITES}:new")
    kb.button(text="🆘 Помощь", callback_data=f"{CB_MENU}:help")
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
    kb.button(text="➕ Создать peer", callback_data=f"{CB_PEERS}:new:{server_id}")
    kb.button(text="🎟 Инвайт", callback_data=f"{CB_INVITES}:new:{server_id}")
    kb.button(text="👥 Peers сервера", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.button(text="🗑 Удалить", callback_data=f"{CB_SERVERS}:del:{server_id}")
    kb.button(text="« К списку", callback_data=f"{CB_SERVERS}:list")
    kb.adjust(2, 1, 1, 1)
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


# --- Peers --------------------------------------------------------------------

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


def peer_card(peer_id: int, can_revoke: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Получить конфиг", callback_data=f"{CB_PEERS}:send:{peer_id}")
    if can_revoke:
        kb.button(text="🗑 Отозвать", callback_data=f"{CB_PEERS}:revoke:{peer_id}")
    kb.button(text="« К списку", callback_data=f"{CB_PEERS}:list")
    kb.adjust(1)
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    return kb.as_markup()
