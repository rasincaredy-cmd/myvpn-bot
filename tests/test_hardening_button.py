"""Кнопка защиты в карточке сервера."""
from bot.keyboards.inline.servers import server_card


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_has_protection_button() -> None:
    markup = server_card(1)
    assert any("Защита" in t for t in _texts(markup))


def test_protection_button_leads_to_check_not_apply() -> None:
    # Первое нажатие обязано только ПОКАЗАТЬ состояние. Применение —
    # отдельным подтверждением: случайный тык не должен трогать сервер.
    markup = server_card(1)
    datas = [d for d in _datas(markup) if d and "harden" in d]
    assert datas, "нет кнопки защиты"
    assert all("hardenrun" not in d for d in datas), (
        "из карточки нельзя сразу применять — только проверка"
    )
