"""Главное меню, «Ещё», оповещения и общая навигация (Блок «Облик»)."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
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


@dataclass(frozen=True)
class MenuState:
    """Состояние юзера, от которого зависит набор кнопок главного меню.

    Отдельный тип, а не три булевых аргумента: меню собирается в трёх местах
    (/start, /menu, возврат «‹ Меню»), и позиционные флаги там рано или поздно
    разъехались бы местами.
    """
    sub_active: bool
    has_devices: bool
    is_trial: bool


# Откуда человек пришёл на вложенный экран. Метка едет хвостом callback_data,
# потому что своего состояния у инлайн-кнопки нет, а FSM ради одной кнопки
# возврата заводить дороже, чем дописать шесть символов.
ORIGIN_MORE = "more"


def origin_of(data: str) -> str | None:
    """Метка происхождения из callback_data, если она там есть."""
    return ORIGIN_MORE if data.endswith(f":{ORIGIN_MORE}") else None


def back_target(origin: str | None) -> tuple[str, str]:
    """Подпись и адрес кнопки возврата — ТУДА, ОТКУДА ПРИШЛИ.

    До 21.08.2026 всё вложенное в «⚙️ Ещё» возвращало в главное меню (а
    рефералка — в «Баланс», где человек вообще не был). Один шаг назад
    сбрасывал на первый этаж, и до второй кнопки раздела человек добирался
    заново через «Ещё».
    """
    if origin == ORIGIN_MORE:
        return "‹ Ещё", f"{CB_MENU}:more"
    return "‹ Меню", f"{CB_MENU}:open"


def main_menu(is_admin: bool, state: MenuState) -> InlineKeyboardMarkup:
    """Главное меню, собранное под состояние юзера.

    До 20.08.2026 меню было одно на всех: одиннадцать кнопок, из которых две
    полноширинные занимали юридические ссылки (их открывают один раз в жизни),
    а витрина «Тарифы»/«Локации» висела и у того, кто платит третий месяц.
    Теперь набор один, но неуместное выпадает, а редкое уезжает в «⚙️ Ещё».

    Правило иерархии: ровно ОДНО главное действие во всю ширину и цветом,
    остальное парами. Два «синих» — это уже не иерархия, а мигание.

    Цвет кнопок (style) поддерживается с Bot API 9.4; старые клиенты просто
    покажут обычные кнопки.
    """
    kb = InlineKeyboardBuilder()
    sizes: list[int] = []

    # Первая кнопка — то единственное, что человеку в этом состоянии нужно.
    if not state.sub_active:
        kb.button(text="🔁 Продлить подписку", callback_data=f"{CB_BAL}:extend",
                  style="primary")
        sizes.append(1)
        kb.button(text="📱 Устройства", callback_data=f"{CB_DEVICE}:list")
        sizes.append(1)
    elif not state.has_devices:
        # У новичка «Мои устройства» ведут в пустой список — предлагаем сразу
        # добавление, а рядом витрину: ему ещё интересно, что за сервис.
        kb.button(text="🚀 Подключить устройство", callback_data=f"{CB_DEVICE}:add",
                  style="primary")
        sizes.append(1)
    else:
        kb.button(text="📱 Устройства", callback_data=f"{CB_DEVICE}:list",
                  style="primary")
        sizes.append(1)
        kb.button(text="⚡ Резервное подключение", callback_data=f"{CB_WDTT}:my")
        sizes.append(1)

    if state.sub_active and not state.has_devices:
        kb.button(text="💳 Тарифы", callback_data=f"{CB_LEGAL}:tariffs")
        kb.button(text="🌍 Локации", callback_data=f"{CB_MENU}:locations")
    else:
        kb.button(text="🎫 Подписка", callback_data=f"{CB_SUB}:my")
        kb.button(text="💰 Баланс", callback_data=f"{CB_BAL}:my")
    sizes.append(2)

    kb.button(text="🆘 Поддержка", callback_data=f"{CB_MENU}:help")
    kb.button(text="⚙️ Ещё", callback_data=f"{CB_MENU}:more")
    sizes.append(2)

    if is_admin:
        kb.button(text="👮 Админ-панель", callback_data=f"{CB_PANEL}:main")
        sizes.append(1)

    kb.adjust(*sizes)
    return kb.as_markup()


def more_menu() -> InlineKeyboardMarkup:
    """Всё, что ушло с главного экрана: настройки, витрина, документы.

    Юридические ссылки живут именно здесь. На главном они занимали две
    полноширинные строки на самом часто открываемом экране бота — а нужны
    ровно один раз.
    """
    from bot.config import settings

    kb = InlineKeyboardBuilder()
    sizes = []
    if settings.miniapp_url:
        # Мини-приложение — первая кнопка раздела: это единственный вход, у
        # которого нет своего экрана в боте, и найти его иначе неоткуда.
        # Главное меню оно не занимает: там потолок в шесть кнопок, а у
        # приложения есть второй вход — кнопка «☰» рядом с полем ввода.
        kb.button(
            text="🚀 Приложение",
            web_app=WebAppInfo(url=settings.miniapp_url),
        )
        sizes.append(1)
    # Метка ORIGIN_MORE в конце callback_data: экран, открытый отсюда, обязан
    # вернуть человека СЮДА, а не в главное меню (см. back_target).
    # Порядок: сначала то, что приносит деньги, потом настройки. Длинные
    # подписи стоят во всю ширину — в паре кнопка получает половину экрана и
    # обрезается многоточием (Влад, 22.08.2026).
    kb.button(text="💳 Тарифы", callback_data=f"{CB_LEGAL}:tariffs:{ORIGIN_MORE}")
    kb.button(text="👥 Пригласить друга", callback_data=f"{CB_BAL}:ref:{ORIGIN_MORE}")
    kb.button(text="🔔 Оповещения", callback_data=f"{CB_MENU}:notify:{ORIGIN_MORE}")
    kb.button(text="🌍 Локации", callback_data=f"{CB_MENU}:locations:{ORIGIN_MORE}")
    sizes += [1, 1, 2]

    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
        sizes.append(1)
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
        sizes.append(1)

    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def onboarding_hint_kb() -> InlineKeyboardMarkup:
    """Одна кнопка под подсказкой новому юзеру: сразу к добавлению устройства
    (cb_dev_add сам проверит подписку/лимит/наличие локаций)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить устройство", callback_data=f"{CB_DEVICE}:add")
    return kb.as_markup()


def notify_settings_kb(enabled: bool, origin: str | None = None) -> InlineKeyboardMarkup:
    # Метку происхождения тащим и на переключателе: после нажатия экран
    # перерисовывается, и возврат не должен внезапно поменять адрес.
    tail = f":{origin}" if origin else ""
    kb = InlineKeyboardBuilder()
    if enabled:
        kb.button(text="🔕 Выключить оповещения",
                  callback_data=f"{CB_MENU}:notify_toggle{tail}")
    else:
        kb.button(text="🔔 Включить оповещения",
                  callback_data=f"{CB_MENU}:notify_toggle{tail}")
    back_text, back_data = back_target(origin)
    kb.button(text=back_text, callback_data=back_data)
    kb.adjust(1)
    return kb.as_markup()


# --- Навигация ----------------------------------------------------------------

def back_to_menu(origin: str | None = None) -> InlineKeyboardMarkup:
    """Кнопка возврата. Без аргумента — в главное меню, как раньше."""
    text, data = back_target(origin)
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=data)
    return kb.as_markup()


def to_server(server_id: int) -> InlineKeyboardMarkup:
    """Кнопка возврата на карточку сервера (после создания peer/инвайта)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="‹ Сервер", callback_data=f"{CB_SERVERS}:open:{server_id}")
    return kb.as_markup()
