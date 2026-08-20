"""Тупики: у каждой стены должна быть дверь (Блок «Тариф», 20.08.2026).

Правило: если человек упёрся в ограничение, которое снимается деньгами или
действием, — это ЭКРАН с кнопкой, а не всплывашка `show_alert`. Всплывашка
годится для «уже сделано», «не найдено» и «попробуй позже».

До этих правок бот трижды говорил «нельзя» и не предлагал ничего: лимит
устройств, лимит резервных подключений и закончившаяся подписка.
"""
from __future__ import annotations

import pytest

from bot.keyboards.inline import limit_reached_kb, wdtt_user_list_kb


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _datas(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


class TestLimitReached:
    def test_offers_a_way_to_pay_and_a_way_back(self) -> None:
        kb = limit_reached_kb("dev:list")
        assert "bal:extend" in _datas(kb), "нет выхода к тарифу"
        assert "dev:list" in _datas(kb), "нет возврата"

    def test_label_matches_what_the_person_needs(self) -> None:
        """Упёрся в лимит — «сменить тариф»; подписка кончилась — «продлить».
        Одна подпись на оба случая врала бы в одном из них."""
        assert "Сменить тариф" in " ".join(_texts(limit_reached_kb("dev:list")))
        assert "Продлить" in " ".join(
            _texts(limit_reached_kb("dev:list", "🔁 Продлить подписку"))
        )

    def test_exit_is_the_primary_action(self) -> None:
        kb = limit_reached_kb("dev:list")
        primary = [b.text for row in kb.inline_keyboard for b in row if b.style == "primary"]
        assert len(primary) == 1


class TestBypassListOffersTariff:
    """«В твоём тарифе нет резервных подключений» — раньше текст отправлял
    человека искать раздел руками, кнопки не было."""

    def test_tariff_button_appears_when_limit_is_zero(self) -> None:
        kb = wdtt_user_list_kb([], can_create=False, offer_tariff=True)
        assert "bal:extend" in _datas(kb)

    def test_no_tariff_button_when_user_can_just_add(self) -> None:
        """Место в тарифе есть — предлагать смену тарифа незачем."""
        kb = wdtt_user_list_kb([], can_create=True, offer_tariff=True)
        assert "bal:extend" not in _datas(kb)
        assert any("Добавить" in t for t in _texts(kb))

    def test_expired_subscription_gets_no_tariff_button(self) -> None:
        """У истёкшей подписки пересчитывать нечего — её сначала продлевают,
        и зовёт к этому экран подписки, а не список подключений."""
        kb = wdtt_user_list_kb([], can_create=False, offer_tariff=False)
        assert "bal:extend" not in _datas(kb)

    @pytest.mark.parametrize("can_create,offer", [(True, False), (False, True), (False, False)])
    def test_always_has_a_way_out(self, can_create: bool, offer: bool) -> None:
        kb = wdtt_user_list_kb([], can_create=can_create, offer_tariff=offer)
        assert "menu:open" in _datas(kb)
