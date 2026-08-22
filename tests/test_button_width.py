"""Кнопки в одном ряду обязаны помещаться (22.08.2026).

Повод — живая жалоба Влада: «в кнопках со сроком не видны последние цифры».
Подписи были в пределах прежнего правила (до 30 символов), но стояли ПО ДВЕ В
РЯД, а в паре кнопка получает половину ширины экрана. Обрезается при этом
хвост — то есть ровно то, ради чего кнопку и читают: цена месяца у сроков,
«ВКЛ/выкл» у автопродления.

Правило: бюджет строки — 30 символов на всю ширину, значит на кнопку в ряду
приходится 30 // (сколько их в ряду). Проверяем СОБРАННЫЕ клавиатуры, а не
исходник: раскладку задаёт `adjust`, и по тексту программы её не видно.
"""
from __future__ import annotations

import pytest

from bot.handlers.balance import (
    _deposit_amounts,
    _shop_rows,
    _star_amounts,
    _term_price_rows,
)
from bot.keyboards import inline as k
from bot.keyboards.inline.menu import MenuState

ROW_BUDGET = 30


def _keyboards() -> dict:
    """Клавиатуры, которые видит платящий человек, со всеми их состояниями.

    Собираются с реальными подписями (цены из прайсинга, а не «X»): проверять
    ширину на заглушках бессмысленно — режется как раз настоящая цена.
    """
    return {
        "меню: есть устройства": k.main_menu(False, MenuState(True, True, False)),
        "меню: новичок": k.main_menu(False, MenuState(True, False, True)),
        "меню: подписка истекла": k.main_menu(False, MenuState(False, True, False)),
        "меню: админ": k.main_menu(True, MenuState(True, True, False)),
        "ещё": k.more_menu(),
        "баланс": k.balance_kb(True),
        "способы пополнения": k.deposit_methods_kb(4),
        "суммы CryptoBot": k.deposit_amounts_kb(_deposit_amounts()),
        "суммы звёзд": k.star_amounts_kb(_star_amounts()),
        "суммы карты": k.platega_amounts_kb(_deposit_amounts()),
        "витрина тарифов": k.tariff_shop_kb(_shop_rows(None)[0], (1, 1)),
        "тариф: конструктор": k.tariff_kb(
            2, 1, _term_price_rows(2, 1), 10, 10, switch_days=90, builder=True
        ),
        "тариф: после витрины": k.tariff_kb(
            4, 2, _term_price_rows(4, 2), 10, 10, switch_days=None, builder=False
        ),
        "не хватает денег": k.not_enough_kb(380, 2, 1),
        "способы на сумму": k.deposit_methods_for_kb(380, 4),
        "подписка": k.subscription_kb(can_pay=True, autopay=True, can_switch=True),
        "подписка: автопродление выкл": k.subscription_kb(
            can_pay=True, autopay=False, can_switch=False
        ),
        "устройства": k.devices_list_kb(
            [(1, "✅", "Телефон")], 1, 3, True, 0, has_prev=True, has_next=True
        ),
        "карточка устройства": k.device_card_kb(
            1, can_get=True, can_revoke=True,
            locations=[(1, "🇳🇱 Нидерланды")], can_move=True,
        ),
        "резервные подключения": k.wdtt_user_list_kb(
            [(1, "✅", "Телефон", "🇳🇱 Нидерланды")], can_create=True
        ),
        "формат конфига": k.config_format_kb(1),
        "устройство создано": k.device_created_kb(),
        "тарифы (витрина сервиса)": k.tariffs_kb(),
    }


@pytest.mark.parametrize("name", sorted(_keyboards()))
def test_buttons_sharing_a_row_fit(name: str) -> None:
    kb = _keyboards()[name]
    bad = []
    for row in kb.inline_keyboard:
        if len(row) < 2:
            continue                       # во всю ширину помещается всё
        limit = ROW_BUDGET // len(row)
        bad += [
            f"[{b.text}] {len(b.text)} > {limit} ({len(row)} в ряду)"
            for b in row if len(b.text) > limit
        ]
    assert not bad, f"{name}: обрежется на телефоне — " + "; ".join(bad)


class TestGuardItself:
    """Молчащий сторож выглядит как сторож, которому нечего сказать."""

    def test_it_notices_a_cramped_row(self) -> None:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()
        kb.button(text="12 мес — 1440 ₽ · 120 ₽/мес", callback_data="a")
        kb.button(text="6 мес — 810 ₽ · 135 ₽/мес", callback_data="b")
        kb.adjust(2)
        markup = kb.as_markup()
        row = markup.inline_keyboard[0]
        assert len(row) == 2
        assert any(len(b.text) > ROW_BUDGET // 2 for b in row)

    def test_it_sees_the_real_terms(self) -> None:
        """Подписи сроков и правда длиннее половины ряда — именно поэтому они
        теперь стоят по одной."""
        longest = max(len(label) for _m, label in _term_price_rows(4, 2))
        assert longest > ROW_BUDGET // 2
