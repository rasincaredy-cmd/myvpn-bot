"""Экран тарифов: цифры берутся из pricing, а не пишутся руками.

Провайдер требует, чтобы клиенту было понятно, сколько и за что он платит,
поэтому цены на экране обязаны совпадать с теми, по которым реально списывают.
"""
from __future__ import annotations

from bot.handlers.legal import build_tariffs_text
from bot.services.pricing import fmt_rub, monthly_price_kopeks, term_price_kopeks


def test_tariffs_text_shows_base_price() -> None:
    assert fmt_rub(monthly_price_kopeks(1, 1)) in build_tariffs_text()


def test_tariffs_text_shows_term_prices() -> None:
    """Скидочные суммы за 3/6/12 месяцев названы явно — банк требует, чтобы
    было понятно, сколько и за что платит клиент."""
    text = build_tariffs_text()
    monthly = monthly_price_kopeks(1, 1)
    for months in (3, 6, 12):
        assert fmt_rub(term_price_kopeks(monthly, months)) in text, (
            f"нет цены за {months} мес"
        )


def test_tariffs_text_has_no_forbidden_wording() -> None:
    assert "обход" not in build_tariffs_text().lower()


def test_tariffs_text_follows_price_changes(monkeypatch) -> None:
    """Цены не зашиты в текст: поменяли .env — экран пересчитался сам.

    Без этой проверки тесты выше прошли бы и на константах, случайно совпавших
    с текущим конфигом.
    """
    from bot.config import settings

    monkeypatch.setattr(settings, "price_first_rub", 150)
    text = build_tariffs_text()
    assert "150 ₽" in text, "цена первой позиции из конфига не попала на экран"
    assert fmt_rub(term_price_kopeks(monthly_price_kopeks(1, 1), 12)) in text


def test_menu_has_legal_buttons(monkeypatch) -> None:
    """Кнопки документов появляются, когда адреса заданы — банк требует
    постоянного доступа к ним из бота."""
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "https://telegra.ph/p")
    monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/t")

    urls = [
        b.url
        for row in main_menu(is_admin=False).inline_keyboard
        for b in row
        if b.url
    ]
    assert "https://telegra.ph/p" in urls
    assert "https://telegra.ph/t" in urls


def test_menu_without_legal_urls_has_no_dead_buttons(monkeypatch) -> None:
    """Адреса не заданы — кнопок нет: пустая ссылка ломает отправку меню."""
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")

    assert not [
        b for row in main_menu(is_admin=False).inline_keyboard for b in row if b.url
    ]

