"""Цена, названная словами, обязана совпадать с ценой, которая спишется.

С 8.08 доплаты за устройство и за резервное подключение РАЗНЫЕ (40 и 30 ₽), а
прежние тексты называли одну цифру на обе позиции — с новым прайсом это прямая
ложь про деньги.
"""
from __future__ import annotations

from bot.config import settings
from bot.services.pricing import monthly_price_kopeks


def test_constructor_starts_at_the_typical_tariff() -> None:
    """Старт конструктора — 1 устройство + 1 подключение (120 ₽), а не 2+1 (160 ₽).

    2+1 выше рыночной медианы ~150 ₽, и первым числом, которое видит юзер, ему
    быть не стоит.
    """
    from bot.handlers.balance import _START_BYPASS, _START_DEVICES

    assert (_START_DEVICES, _START_BYPASS) == (1, 1)
    assert monthly_price_kopeks(_START_DEVICES, _START_BYPASS) == 120_00


def test_extend_text_names_both_extra_prices() -> None:
    from bot.handlers.balance import _extend_intro

    text = _extend_intro()
    assert f"{settings.price_first_rub} ₽" in text
    assert f"{settings.price_extra_device_rub} ₽" in text
    assert f"{settings.price_extra_bypass_rub} ₽" in text


def test_tariffs_screen_names_both_extra_prices() -> None:
    """Экран тарифов — витрина для платёжного провайдера: одна доплата вместо
    двух означала бы, что второе устройство стоит 30 ₽, а списывается 40 ₽."""
    from bot.handlers.legal import build_tariffs_text

    text = build_tariffs_text()
    assert f"{settings.price_extra_device_rub} ₽" in text
    assert f"{settings.price_extra_bypass_rub} ₽" in text


def test_tariffs_screen_shows_the_real_typical_price() -> None:
    from bot.handlers.legal import build_tariffs_text

    assert "120 ₽" in build_tariffs_text()
