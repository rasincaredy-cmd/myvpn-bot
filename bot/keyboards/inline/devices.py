"""Устройства юзера и его подписка (Блок 9)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_BAL, CB_CFG, CB_DEVICE, CB_MENU, CB_SUB, CB_WDTT


def config_format_kb(peer_id: int) -> InlineKeyboardMarkup:
    """Чем прислать конфиг. Файл первой кнопкой — он нужен чаще всего и
    работает на любой платформе; QR и ссылка закрывают частные случаи
    (другое устройство рядом / этот же телефон)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Файлом",    callback_data=f"{CB_CFG}:file:{peer_id}")
    kb.button(text="📱 QR-кодом",  callback_data=f"{CB_CFG}:qr:{peer_id}")
    kb.button(text="🔗 Ссылкой",   callback_data=f"{CB_CFG}:link:{peer_id}")
    kb.adjust(1)
    return kb.as_markup()


def config_format_device_kb(device_id: int) -> InlineKeyboardMarkup:
    """То же самое, но на всё устройство сразу: у конфигов одного устройства
    формат нужен один и тот же, а вопрос до 10.08.2026 задавался на каждую
    локацию отдельно. `dev` в середине отличает эти кнопки от одиночных."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Файлами",   callback_data=f"{CB_CFG}:file:dev:{device_id}")
    kb.button(text="📱 QR-кодами", callback_data=f"{CB_CFG}:qr:dev:{device_id}")
    kb.button(text="🔗 Ссылками",  callback_data=f"{CB_CFG}:link:dev:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


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
    # Имя кнопки везде одно — «🎫 Подписка» (как в меню и в заголовке экрана):
    # человек ищет глазами ровно то слово, на которое нажал.
    kb.button(text="🎫 Подписка", callback_data=f"{CB_SUB}:my")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def device_card_kb(
    device_id: int,
    can_get: bool,
    can_revoke: bool,
    locations: list[tuple[int, str]] | None = None,  # (peer_id, loc_label)
    can_move: bool = False,
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
    # Смена сервера (Этап C). Кнопки нет, когда переезжать некуда: живая
    # кнопка, отвечающая «некуда», хуже отсутствующей.
    if can_move:
        kb.button(text="🔀 Сменить сервер", callback_data=f"{CB_DEVICE}:move:{device_id}")
    # Переименование — только метка в БД, конфиги не трогает (Блок «Ревизия»).
    kb.button(text="✏️ Переименовать", callback_data=f"{CB_DEVICE}:ren:{device_id}")
    # Удаление доступно всегда: активное устройство удаляется (с отзывом), а
    # неактивное (истекшее) — убирается из списка, чтобы не висело мусором.
    kb.button(text="🗑 Удалить устройство", callback_data=f"{CB_DEVICE}:revoke:{device_id}",
              style="danger")
    kb.button(text="‹ Устройства", callback_data=f"{CB_DEVICE}:list")
    kb.adjust(1)
    return kb.as_markup()


def move_pick_config_kb(
    rows: list[tuple[int, str]], device_id: int  # (peer_id, loc_label)
) -> InlineKeyboardMarkup:
    """Какой из конфигов устройства переселяем. Показывается только когда их
    больше одного — с единственным конфигом лишний экран это лишний тап."""
    kb = InlineKeyboardBuilder()
    for peer_id, loc in rows:
        kb.button(text=f"🔀 {loc}", callback_data=f"{CB_DEVICE}:mvloc:{peer_id}")
    kb.button(text="‹ Устройство", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def move_pick_location_kb(
    peer_id: int, names: list[str], device_id: int
) -> InlineKeyboardMarkup:
    """Локации кнопками ПО ИНДЕКСУ: юникод-название с флагом («🇳🇱 Нидерланды»)
    в 64 байта callback_data не всегда влезает — тот же приём, что в
    pick_location_kb. Индекс — позиция в отсортированном списке ключей, и
    хендлер пересобирает список тем же способом."""
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(names):
        kb.button(text=name, callback_data=f"{CB_DEVICE}:mvsrv:{peer_id}:{i}")
    kb.button(text="‹ Устройство", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def move_pick_server_kb(
    peer_id: int, rows: list[tuple[int, str]], device_id: int  # (server_id, label)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for server_id, label in rows:
        kb.button(text=f"🖥 {label}", callback_data=f"{CB_DEVICE}:mvok:{peer_id}:{server_id}")
    kb.button(text="‹ Устройство", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def move_confirm_kb(peer_id: int, server_id: int, device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, переехать", callback_data=f"{CB_DEVICE}:mvgo:{peer_id}:{server_id}")
    kb.button(text="‹ Устройство", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def subscription_kb(
    *,
    can_pay: bool = False,        # показать оплату (способ пополнения включён)
    autopay: bool | None = None,  # None — тумблер не показывать (нет смысла без оплаты)
    can_switch: bool = False,     # смена тарифа без оплаты доступна
) -> InlineKeyboardMarkup:
    """Кнопки экрана подписки.

    «⚙️ Сменить тариф» — главная новинка 20.08.2026: до неё сменить тариф можно
    было ТОЛЬКО купив новый срок, и человек, которому нужно второе устройство,
    упирался во всплывашку «достигнут лимит» без единого выхода.
    """
    kb = InlineKeyboardBuilder()
    sizes: list[int] = []
    if can_pay:
        kb.button(text="🔁 Продлить подписку", callback_data=f"{CB_BAL}:extend",
                  style="primary")
        sizes.append(1)
    if can_switch:
        kb.button(text="⚙️ Сменить тариф", callback_data=f"{CB_BAL}:extend")
        sizes.append(1)
    if autopay is not None:
        # Во всю ширину: в паре кнопка получает половину экрана, и обрезается у
        # неё ровно хвост — то самое «ВКЛ/выкл», ради которого её и читают
        # (та же беда, что у кнопок сроков; Влад, 22.08.2026).
        kb.button(
            text="♻️ Автопродление: ВКЛ" if autopay else "♻️ Автопродление: выкл",
            callback_data=f"{CB_BAL}:autopay",
        )
        sizes.append(1)
        kb.button(text="📱 Устройства", callback_data=f"{CB_DEVICE}:list")
        sizes.append(1)
    else:
        kb.button(text="📱 Устройства", callback_data=f"{CB_DEVICE}:list")
        sizes.append(1)
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def device_created_kb() -> InlineKeyboardMarkup:
    """После создания устройства: справка и резервное подключение — кнопками.

    Инструкция уехала за «📖 Как подключить» (21.08.2026): в самом сообщении
    остались три шага, а всё, что длиннее, открывает тот, кому оно нужно.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Как подключить", callback_data=f"{CB_MENU}:howto")
    kb.button(text="⚡ Резервное подключение", callback_data=f"{CB_WDTT}:my")
    kb.button(text="📱 Устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def howto_kb() -> InlineKeyboardMarkup:
    """Справка «как подключить»: выход в поддержку и в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🆘 Поддержка", callback_data=f"{CB_MENU}:help")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(2)
    return kb.as_markup()


def back_to_devices_kb() -> InlineKeyboardMarkup:
    """Возврат после удаления своего устройства (Блок «Мелочи 2»): в список
    устройств, а не в главное меню — юзер обычно удаляет и сразу смотрит, что
    осталось."""
    kb = InlineKeyboardBuilder()
    kb.button(text="‹ Устройства", callback_data=f"{CB_DEVICE}:list")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()

def limit_reached_kb(back_to: str, label: str = "⚙️ Сменить тариф") -> InlineKeyboardMarkup:
    """Выход из тупика: одна кнопка к экрану тарифа и возврат.

    До 20.08.2026 на месте таких экранов были всплывашки — «Достигнут лимит
    устройств (1/1)», «Подписка закончилась» — и больше ничего: человек
    упирался в стену ровно в тот момент, когда готов был заплатить. Теперь у
    стены есть дверь, а подпись на ней зависит от того, что человеку нужно:
    сменить тариф или продлить подписку.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=label, callback_data=f"{CB_BAL}:extend", style="primary")
    kb.button(text="‹ Назад", callback_data=back_to)
    kb.adjust(1)
    return kb.as_markup()
