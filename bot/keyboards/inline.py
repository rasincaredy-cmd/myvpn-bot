from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.models import Peer, Server

# --- Callback prefixes --------------------------------------------------------
# Формат: "<ns>:<action>[:<arg>]"
CB_MENU = "menu"
CB_INSTALL = "install"
CB_SERVERS = "srv"
CB_INVITES = "inv"
CB_ADMIN = "adm"          # admin-панель: управление пирами любого юзера
CB_PANEL = "pnl"   # admin-панель
CB_WDTT = "wdtt"   # обход белых списков (wdtt / proxy-turn-vk)
CB_DEVICE = "dev"  # устройства (Блок 9)
CB_SUB = "sub"     # подписка (Блок 9)
CB_NOP = "nop"
CB_CANCEL = "cancel"
CB_BAL = "bal"     # баланс/оплата/рефералка (Блок «Баланс»)
CB_SUPPORT = "sup" # сапорт-чат (Блок «Сапорт-чат»)


# --- Главное меню -------------------------------------------------------------

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="🛡 Обход БС", callback_data=f"{CB_WDTT}:my")
    kb.button(text="🎫 Моя подписка", callback_data=f"{CB_SUB}:my")
    kb.button(text="💰 Баланс", callback_data=f"{CB_BAL}:my")
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


def user_wipe_confirm_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    """Двухшаговое подтверждение уничтожения юзера (Блок «Ревизия»)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="❗️ Да, стереть безвозвратно", callback_data=f"{CB_PANEL}:udelc:{user_id}:{page}")
    kb.button(text="✖️ Отмена", callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


# --- Сапорт-чат (Блок «Сапорт-чат») -------------------------------------------

def support_intro_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Написать в поддержку", callback_data=f"{CB_SUPPORT}:start")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def support_dialog_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Завершить диалог", callback_data=f"{CB_MENU}:open")
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


# --- Баланс (Блок «Баланс») ---------------------------------------------------

def balance_kb(can_deposit: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_deposit:
        kb.button(text="➕ Пополнить", callback_data=f"{CB_BAL}:dep")
    kb.button(text="📜 История", callback_data=f"{CB_BAL}:hist")
    kb.button(text="👥 Реферальная программа", callback_data=f"{CB_BAL}:ref")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def deposit_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """amounts: (рубли, подпись кнопки) — подписи считаются из прайсинга
    («90 ₽ — месяц»), чтобы суммы не выглядели случайными числами."""
    kb = InlineKeyboardBuilder()
    for rub, label in amounts:
        kb.button(text=label, callback_data=f"{CB_BAL}:dep:{rub}")
    kb.button(text="✏️ Своя сумма", callback_data=f"{CB_BAL}:dep:custom")
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def invoice_kb(pay_url: str, row_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить в @CryptoBot", url=pay_url)
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"{CB_BAL}:check:{row_id}")
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()


def extend_kb(
    devices: int, bypass: int, term_prices: list[tuple[int, str]],
    max_devices: int, max_bypass: int,
) -> InlineKeyboardMarkup:
    """Экран продления: тариф крутится ±, сроки с ценами. Всё состояние — в
    callback data (без FSM): ext:<dev>:<byp> перерисовка, buy:<dev>:<byp>:<mes>.

    Подписи средних кнопок — только эмодзи+число («📱 2»): в ряду из трёх кнопок
    длинный текст обрезается на телефоне и числа не видно; расшифровка типов —
    в тексте сообщения. На границах (0, максимум, «последняя позиция») «−»/«+»
    рисуем заглушкой CB_NOP — не гоняем пустые перерисовки."""
    kb = InlineKeyboardBuilder()

    def _step(cur_d: int, cur_b: int, ok: bool) -> str:
        return f"{CB_BAL}:ext:{cur_d}:{cur_b}" if ok else CB_NOP

    # «−» недоступен на нуле и когда это последняя позиция тарифа (0+0 нельзя).
    kb.button(text="−", callback_data=_step(devices - 1, bypass, devices > 0 and devices + bypass > 1))
    kb.button(text=f"📱 {devices}", callback_data=CB_NOP)
    kb.button(text="+", callback_data=_step(devices + 1, bypass, devices < max_devices))
    kb.button(text="−", callback_data=_step(devices, bypass - 1, bypass > 0 and devices + bypass > 1))
    kb.button(text=f"🛡 {bypass}", callback_data=CB_NOP)
    kb.button(text="+", callback_data=_step(devices, bypass + 1, bypass < max_bypass))
    for months, label in term_prices:
        kb.button(text=label, callback_data=f"{CB_BAL}:buy:{devices}:{bypass}:{months}")
    # Выход на пополнение прямо отсюда: юзеру с пустым балансом не нужно
    # догадываться, что пополнение живёт в разделе «Баланс».
    kb.button(text="➕ Пополнить баланс", callback_data=f"{CB_BAL}:dep")
    kb.button(text="« К подписке", callback_data=f"{CB_SUB}:my")
    kb.adjust(3, 3, 2, 2, 1, 1)
    return kb.as_markup()


def admin_sub_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Лимит устройств", callback_data=f"{CB_PANEL}:sub_lim:{user_id}:{page}")
    kb.button(text="🛡 Лимит обхода БС",  callback_data=f"{CB_PANEL}:sub_bp:{user_id}:{page}")
    kb.button(text="📅 Задать срок",     callback_data=f"{CB_PANEL}:sub_ext:{user_id}:{page}")
    kb.button(text="📊 Лимит трафика",   callback_data=f"{CB_PANEL}:sub_trf:{user_id}:{page}")
    kb.button(text="💰 Баланс ±",        callback_data=f"{CB_PANEL}:sub_bal:{user_id}:{page}")
    kb.button(text="🚫 Отключить (срок в 0)", callback_data=f"{CB_PANEL}:sub_off:{user_id}:{page}")
    kb.button(text="« К пользователю",   callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(1)
    return kb.as_markup()


# --- Обход белых списков (wdtt) ----------------------------------------------

def wdtt_vk_choice_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Рекомендуемый вариант", callback_data=f"{CB_WDTT}:vk:svc")
    kb.button(text="🔗 Своя ссылка на звонок VK", callback_data=f"{CB_WDTT}:vk:own")
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
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for access_id, mark, label, server_name in rows:
        kb.button(
            text=f"{mark} {label} @ {server_name}",
            callback_data=f"{CB_WDTT}:myopen:{access_id}",
        )
    if can_create:
        kb.button(text="➕ Добавить обход", callback_data=f"{CB_WDTT}:new")
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
