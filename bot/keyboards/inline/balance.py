"""Баланс, пополнение, инвойсы и конструктор тарифа (Блок «Баланс»)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.menu import ORIGIN_MORE
from bot.keyboards.inline.prefixes import CB_BAL, CB_MENU, CB_NOP, CB_SUB


def balance_kb(can_deposit: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_deposit:
        kb.button(text="➕ Пополнить", callback_data=f"{CB_BAL}:dep", style="success")
    kb.button(text="📜 История", callback_data=f"{CB_BAL}:hist")
    kb.button(text="👥 Пригласить друга", callback_data=f"{CB_BAL}:ref")
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def topup_kb() -> InlineKeyboardMarkup:
    """Одна кнопка пополнения — для уведомлений, где деньги закончились не
    вовремя (например, автопродление срезало срок подписки): юзеру не нужно
    искать раздел «Баланс» в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Пополнить баланс", callback_data=f"{CB_BAL}:dep",
              style="success")
    return kb.as_markup()


def deposit_methods_kb(
    bonus_percent: int, cryptobot: bool = True, platega: bool = True
) -> InlineKeyboardMarkup:
    """Выбор способа пополнения (этап D). Бонус за способ — прямо на кнопке:
    выбирают из строк, а не из абзаца текста над ними.

    cryptobot=False / platega=False — ключи способа не настроены; звёздам
    настройка не нужна, поэтому пополнение живо даже без обоих."""
    kb = InlineKeyboardBuilder()
    if platega:
        kb.button(text="💳 Карта или СБП", callback_data=f"{CB_BAL}:dep:pg",
                  style="success")
    if cryptobot:
        kb.button(
            text=f"💎 CryptoBot  +{bonus_percent}%",
            callback_data=f"{CB_BAL}:dep:cb", style="success",
        )
    kb.button(text="⭐ Звёзды Telegram", callback_data=f"{CB_BAL}:dep:stars")
    kb.button(text="‹ Баланс", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()


def deposit_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """amounts: (рубли, подпись кнопки) — подписи считаются из прайсинга
    («90 ₽ — месяц»), чтобы суммы не выглядели случайными числами."""
    kb = InlineKeyboardBuilder()
    for rub, label in amounts:
        kb.button(text=label, callback_data=f"{CB_BAL}:dep:{rub}")
    kb.button(text="✏️ Своя сумма", callback_data=f"{CB_BAL}:dep:custom")
    kb.button(text="‹ Назад", callback_data=f"{CB_BAL}:dep")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def star_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """То же, что и суммы CryptoBot, но платят звёздами: на кнопке обе цифры —
    сколько звёзд спишется и сколько рублей придёт на баланс. Одна цифра без
    другой означала бы, что курс юзер узнаёт только на экране оплаты."""
    kb = InlineKeyboardBuilder()
    for rub, label in amounts:
        kb.button(text=label, callback_data=f"{CB_BAL}:star:{rub}")
    kb.button(text="✏️ Своя сумма", callback_data=f"{CB_BAL}:star:custom")
    kb.button(text="‹ Назад", callback_data=f"{CB_BAL}:dep")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def platega_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Суммы для оплаты картой/СБП — те же, что у остальных способов: юзер не
    должен видеть разный набор сумм в зависимости от кошелька."""
    kb = InlineKeyboardBuilder()
    for rub, label in amounts:
        kb.button(text=label, callback_data=f"{CB_BAL}:pg:{rub}")
    kb.button(text="✏️ Своя сумма", callback_data=f"{CB_BAL}:pg:custom")
    kb.button(text="‹ Назад", callback_data=f"{CB_BAL}:dep")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def platega_invoice_kb(pay_url: str, row_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", url=pay_url, style="success")
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"{CB_BAL}:pgchk:{row_id}")
    kb.button(text="‹ Баланс", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()


def star_invoice_kb(stars: int) -> InlineKeyboardMarkup:
    """Клавиатура счёта в звёздах: кнопка оплаты и выход.

    Без своей клавиатуры Telegram рисует у счёта ОДНУ кнопку «Оплатить», и
    передумавший юзер остаётся в тупике — у счёта @CryptoBot выход есть, а
    здесь не было.

    Кнопка оплаты обязана быть ПЕРВОЙ (Telegram не примет клавиатуру счёта,
    где pay-кнопка не в начале), её подпись клиент подставляет сам.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐ Оплатить {stars}", pay=True)
    kb.button(text="✖️ Отмена", callback_data=f"{CB_BAL}:starx")
    kb.adjust(1)
    return kb.as_markup()


def invoice_kb(pay_url: str, row_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить в @CryptoBot", url=pay_url, style="success")
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"{CB_BAL}:check:{row_id}")
    kb.button(text="‹ Баланс", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()


def tariff_shop_kb(
    rows: list[tuple[str, str]], builder_to: tuple[int, int]
) -> InlineKeyboardMarkup:
    """Витрина готовых тарифов (22.08.2026).

    До неё покупка открывалась конструктором «−/+»: шесть кнопок и вопрос
    «сколько тебе устройств?», на который человек, пришедший купить VPN, ещё не
    знает ответа. Теперь сначала три готовые строки с ценой, а конструктор —
    отдельной кнопкой для тех, кто знает точно.

    `rows` — (ключ пресета, подпись) уже с ценой: считать её в клавиатуре
    нельзя, иначе цифра на кнопке разойдётся с цифрой в тексте экрана.
    """
    kb = InlineKeyboardBuilder()
    for key, label in rows:
        kb.button(text=label, callback_data=f"{CB_BAL}:pre:{key}", style="success")
    kb.button(
        text="🧮 Собрать свой тариф",
        callback_data=f"{CB_BAL}:ext:{builder_to[0]}:{builder_to[1]}",
    )
    kb.button(text="➕ Пополнить баланс", callback_data=f"{CB_BAL}:dep")
    kb.button(text="‹ Подписка", callback_data=f"{CB_SUB}:my")
    kb.adjust(1)
    return kb.as_markup()


def not_enough_kb(
    need_rub: int, devices: int, bypass: int
) -> InlineKeyboardMarkup:
    """Экран «не хватает денег»: единственный осмысленный выход — пополнить.

    До 22.08.2026 здесь была всплывашка с текстом «жми Пополнить под
    сообщением»: человек упирался в отказ ровно в тот момент, когда собрался
    платить, и должен был сам найти нужную кнопку. Теперь сумма уже
    подставлена — остаётся выбрать, чем платить.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"➕ Пополнить на {need_rub} ₽",
              callback_data=f"{CB_BAL}:need:{need_rub}", style="success")
    kb.button(text="✏️ Другая сумма", callback_data=f"{CB_BAL}:dep")
    kb.button(text="‹ К тарифу", callback_data=f"{CB_BAL}:pick:{devices}:{bypass}")
    kb.adjust(1)
    return kb.as_markup()


def deposit_methods_for_kb(
    rub: int, bonus_percent: int, cryptobot: bool = True, platega: bool = True
) -> InlineKeyboardMarkup:
    """Выбор способа, когда сумма УЖЕ известна (пришли из «не хватает N ₽»).

    Отдельная клавиатура, а не общая: там сумму ещё выбирают, здесь она
    посчитана за человека, и заново спрашивать её — терять того, кто уже
    достал карту.
    """
    kb = InlineKeyboardBuilder()
    if platega:
        kb.button(text=f"💳 Карта или СБП — {rub} ₽",
                  callback_data=f"{CB_BAL}:pg:{rub}", style="success")
    if cryptobot:
        kb.button(text=f"💎 CryptoBot  +{bonus_percent}%",
                  callback_data=f"{CB_BAL}:dep:{rub}", style="success")
    kb.button(text="⭐ Звёзды Telegram", callback_data=f"{CB_BAL}:star:{rub}")
    kb.button(text="✏️ Другая сумма", callback_data=f"{CB_BAL}:dep")
    kb.adjust(1)
    return kb.as_markup()


def tariff_kb(
    devices: int, bypass: int, term_prices: list[tuple[int, str]],
    max_devices: int, max_bypass: int, *, switch_days: int | None,
    builder: bool = True,
) -> InlineKeyboardMarkup:
    """Экран «⚙️ Тариф»: тариф крутится ±, ниже — смена без оплаты и сроки.

    Всё состояние живёт в callback_data, без FSM: `ext:<dev>:<byp>` —
    перерисовка, `chg:<dev>:<byp>` — смена без оплаты, `buy:<dev>:<byp>:<мес>` —
    покупка. Иначе экран ломался бы от старого сообщения в истории чата.

    `switch_days` — сколько дней останется после смены без оплаты, или None,
    если смена сейчас недоступна (тариф не тронут, триал, истёкшая, бессрочная).
    Число стоит прямо на кнопке: человек меняет тариф и обязан видеть, во что
    превратится срок, ДО нажатия, а не после.

    Подписи средних кнопок — только эмодзи+число («📱 2»): в ряду из трёх кнопок
    длинный текст обрезается на телефоне и числа не видно; расшифровка типов —
    в тексте сообщения. На границах (0, максимум, «последняя позиция») «−»/«+»
    рисуем заглушкой CB_NOP — не гоняем пустые перерисовки.
    """
    kb = InlineKeyboardBuilder()
    sizes: list[int] = []

    def _step(cur_d: int, cur_b: int, ok: bool) -> str:
        return f"{CB_BAL}:ext:{cur_d}:{cur_b}" if ok else CB_NOP

    if builder:
        # «−» недоступен на нуле и когда это последняя позиция тарифа (0+0 нельзя).
        kb.button(text="−", callback_data=_step(devices - 1, bypass, devices > 0 and devices + bypass > 1))
        kb.button(text=f"📱 {devices}", callback_data=CB_NOP)
        kb.button(text="+", callback_data=_step(devices + 1, bypass, devices < max_devices))
        kb.button(text="−", callback_data=_step(devices, bypass - 1, bypass > 0 and devices + bypass > 1))
        kb.button(text=f"⚡ {bypass}", callback_data=CB_NOP)
        kb.button(text="+", callback_data=_step(devices, bypass + 1, bypass < max_bypass))
        sizes += [3, 3]

    if switch_days is not None:
        kb.button(
            text=f"✅ Сменить без оплаты — {switch_days} дн.",
            callback_data=f"{CB_BAL}:chg:{devices}:{bypass}",
            style="primary",
        )
        sizes.append(1)

    # Сроки — по ОДНОМУ в ряд. Кнопка в паре получает половину ширины, и
    # подпись «12 мес — 1440 ₽ · 120 ₽/мес» на телефоне обрезалась ровно там,
    # где стоит самое важное — цена месяца (Влад, 22.08.2026). Правило «до 30
    # символов» тут не спасает: оно про кнопку во всю ширину.
    for months, label in term_prices:
        kb.button(text=label, callback_data=f"{CB_BAL}:buy:{devices}:{bypass}:{months}",
                  style="success")
    sizes.extend([1] * len(term_prices))

    if not builder:
        # Состав пришёл из витрины — менять его можно, но это уже не первое,
        # чего от человека ждут: сначала срок.
        kb.button(text="🧮 Изменить состав",
                  callback_data=f"{CB_BAL}:ext:{devices}:{bypass}")
        sizes.append(1)

    # Выход на пополнение прямо отсюда: юзеру с пустым балансом не нужно
    # догадываться, что пополнение живёт в разделе «Баланс».
    kb.button(text="➕ Пополнить баланс", callback_data=f"{CB_BAL}:dep")
    kb.button(text="‹ Тарифы", callback_data=f"{CB_BAL}:shop")
    sizes.extend([1, 1])
    kb.adjust(*sizes)
    return kb.as_markup()


def tariff_confirm_kb(devices: int, bypass: int) -> InlineKeyboardMarkup:
    """Подтверждение смены тарифа без оплаты.

    Отдельный шаг, потому что действие меняет дату окончания подписки и назад
    его не отмотать: пересчёт округляется вниз, и «передумал, верни как было»
    вернёт на день-другой меньше.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, сменить", callback_data=f"{CB_BAL}:chgok:{devices}:{bypass}",
              style="primary")
    kb.button(text="‹ Назад", callback_data=f"{CB_BAL}:ext:{devices}:{bypass}")
    kb.adjust(1)
    return kb.as_markup()


def referral_kb(origin: str | None = None) -> InlineKeyboardMarkup:
    """Реферальный экран: сменить имя в ссылке и выход.

    Смена имени — отдельная кнопка, а не «настройка где-то там»: ссылку несут
    на форумы, и человек хочет, чтобы она называлась им, а не номером.

    Возврат зависит от того, откуда пришли: из «⚙️ Ещё» — обратно в «Ещё».
    Раньше он всегда вёл в «Баланс», где человек мог вообще не быть.
    """
    tail = f":{origin}" if origin else ""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить ссылку", callback_data=f"{CB_BAL}:refedit{tail}")
    if origin == ORIGIN_MORE:
        kb.button(text="‹ Ещё", callback_data=f"{CB_MENU}:more")
    else:
        kb.button(text="‹ Баланс", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()
