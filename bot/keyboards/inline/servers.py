"""Серверы: список, карточка, локации, инвайты, пиры сервера глазами админа."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.models import Peer, Server
from bot.keyboards.inline.prefixes import (
    CB_ADMIN,
    CB_CANCEL,
    CB_INVITES,
    CB_PANEL,
    CB_SERVERS,
    CB_WDTT,
)


def servers_list(servers: list[Server]) -> InlineKeyboardMarkup:
    """Список серверов, сгруппированный по локациям (Блок «Мелочи»): порядок
    задаёт repo.list_all_servers, здесь локация выносится в начало кнопки, чтобы
    серверы одной страны читались одним блоком. Без локации — «❔»."""
    kb = InlineKeyboardBuilder()
    for s in servers:
        loc = s.location or "❔ без локации"
        kb.button(
            text=f"{loc} · {s.name} ({s.status})",
            callback_data=f"{CB_SERVERS}:open:{s.id}",
        )
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()


def server_card(
    server_id: int, wdtt_enabled: bool = False, is_private: bool = False
) -> InlineKeyboardMarkup:
    # «➕ Создать peer» убран (Блок «Ревизия»): выдача идёт через подписку юзера
    # («📱 Мои устройства» — по всем локациям), одиночные пиры — легаси.
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Peers сервера", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.button(text="🛡 Обходы сервера", callback_data=f"{CB_SERVERS}:wdtt:{server_id}")
    kb.button(text="🎟 Инвайт",        callback_data=f"{CB_INVITES}:new:{server_id}")
    kb.button(text="📋 Инвайты",       callback_data=f"{CB_INVITES}:list:{server_id}")
    kb.button(text="📊 Трафик",        callback_data=f"{CB_SERVERS}:traffic:{server_id}")
    kb.button(text="🖥 Состояние",     callback_data=f"{CB_SERVERS}:stats:{server_id}")
    # Скорость и объём канала: упираемся мы в трафик, а не в процессор, и
    # видеть темп относительно потолка хостера нужно до того, как упрёмся.
    kb.button(text="📈 Канал",         callback_data=f"{CB_SERVERS}:chan:{server_id}")
    kb.button(text="🌍 Локация",       callback_data=f"{CB_SERVERS}:loc:{server_id}")
    kb.button(text="✏️ Имя",           callback_data=f"{CB_SERVERS}:rename:{server_id}")
    kb.button(text="🌐 DNS",           callback_data=f"{CB_SERVERS}:dns:{server_id}")
    # Тумблер доступности обхода БС на сервере (выдачу юзеры делают сами).
    kb.button(
        text="🛡 Обход БС: ВКЛ" if wdtt_enabled else "🛡 Обход БС: выкл",
        callback_data=f"{CB_WDTT}:toggle:{server_id}",
    )
    # Приватность: сервер только для админов и «друзей» (User.is_vip).
    kb.button(
        text="🔒 Приватный: ВКЛ" if is_private else "🔓 Приватный: выкл",
        callback_data=f"{CB_SERVERS}:priv:{server_id}",
    )
    kb.button(text="🛡 Защита", callback_data=f"{CB_SERVERS}:harden:{server_id}")
    kb.button(text="🗑 Удалить", callback_data=f"{CB_SERVERS}:del:{server_id}")
    kb.button(text="« К списку", callback_data=f"{CB_SERVERS}:list")
    kb.adjust(2, 2, 2, 2, 1, 1, 1, 1)
    return kb.as_markup()


def server_wdtt_list_kb(
    rows: list[tuple[int, str]], server_id: int  # (access_id, label)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for access_id, label in rows:
        kb.button(text=f"🛡 {label}", callback_data=f"{CB_SERVERS}:wopen:{access_id}")
    kb.button(text="✏️ Лимит обходов", callback_data=f"{CB_SERVERS}:wlim:{server_id}")
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


def channel_nav(server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить",  callback_data=f"{CB_SERVERS}:chan:{server_id}")
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


def pick_location_kb(names: list[str], action_prefix: str) -> InlineKeyboardMarkup:
    """Выбор локации из списка. Кнопки по ИНДЕКСУ (юникод-название с флагом может
    не влезть в 64 байта callback_data) — список имён кладётся в FSM state."""
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(names):
        kb.button(text=name, callback_data=f"{action_prefix}:{i}")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(1)
    return kb.as_markup()


def location_choice_kb(
    names: list[str], action_prefix: str, cancel_cb: str = CB_CANCEL
) -> InlineKeyboardMarkup:
    """Локация для сервера (админ): существующие — кнопками (защита от опечаток,
    «🇩🇪 Германия» и «🇩🇪  Германия» стали бы двумя локациями), новая — текстом.
    Кнопки по индексу, список имён — в FSM state."""
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(names):
        kb.button(text=name, callback_data=f"{action_prefix}:{i}")
    kb.button(text="✏️ Новая локация", callback_data=f"{action_prefix}:new")
    kb.button(text="🚫 Без локации", callback_data=f"{action_prefix}:none")
    kb.button(text="✖️ Отмена", callback_data=cancel_cb)
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
    kb.button(text="✏️ Лимит конфигов", callback_data=f"{CB_SERVERS}:plim:{server_id}")
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_peer_card(
    peer_id: int, server_id: int, can_revoke: bool, can_move: bool = False
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_revoke:
        kb.button(text="📥 Получить конфиг", callback_data=f"{CB_ADMIN}:conf:{peer_id}")
        # Переезд (Этап C): бот сам возьмёт свободный сервер этой же локации.
        # Кнопки нет, когда переселять некуда, — живая кнопка, отвечающая
        # «некуда», это обещание, которого админу не выполнят.
        if can_move:
            kb.button(text="🔀 Переселить", callback_data=f"{CB_ADMIN}:move:{peer_id}")
        kb.button(text="🗑 Отозвать",         callback_data=f"{CB_ADMIN}:revoke:{peer_id}")
    else:
        kb.button(text="♻️ Возобновить",   callback_data=f"{CB_ADMIN}:revive:{peer_id}")
        kb.button(text="❌ Удалить из БД", callback_data=f"{CB_ADMIN}:delete:{peer_id}")
    # Переименование доступно всегда — это просто метка в БД, не трогает конфиг.
    kb.button(text="✏️ Переименовать", callback_data=f"{CB_ADMIN}:rename:{peer_id}")
    kb.button(text="« К пирам", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.adjust(1)
    return kb.as_markup()


def back_to_servers_kb() -> InlineKeyboardMarkup:
    """Возврат после удаления сервера (Блок «Мелочи 2»): раньше кидало в главное
    меню, хотя админ пришёл из списка серверов и обычно продолжает там же."""
    kb = InlineKeyboardBuilder()
    kb.button(text="« К серверам", callback_data=f"{CB_SERVERS}:list")
    kb.button(text="👮 Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(1)
    return kb.as_markup()
