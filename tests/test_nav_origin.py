"""Кнопка «назад» обязана вести туда, откуда пришли (21.08.2026).

Влад: «кнопки, вложенные в „Ещё“, ведут не назад, а в баланс или в главное
меню». Так и было: у раздела «⚙️ Ещё» четыре вложенных экрана, и все четыре
возвращали мимо — три в главное меню, рефералка вообще в «Баланс», где человек
мог ни разу не быть. Один шаг назад сбрасывал на первый этаж, и до второй
кнопки раздела приходилось идти заново.

Метка происхождения едет хвостом callback_data (`…:more`) — своего состояния у
инлайн-кнопки нет. Отсюда два места, где всё ломается тихо:
  • кнопку в «Ещё» добавили без хвоста — экран снова уводит в меню;
  • хендлер ловит только «голый» callback_data — кнопка с хвостом мертва.
Тест сторожит оба.
"""
from __future__ import annotations

import pytest

from bot.keyboards.inline import (
    ORIGIN_MORE,
    back_target,
    back_to_menu,
    more_menu,
    notify_settings_kb,
    origin_of,
    referral_kb,
)


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _nested(kb):
    """Кнопки «Ещё», которые ведут на вложенный экран бота.

    Ссылки на документы и сама кнопка возврата — не вложенные экраны.
    """
    return [
        b for b in _buttons(kb)
        if b.callback_data and not b.text.startswith("‹")
    ]


class TestMoreMenuMarksItsChildren:
    def test_every_nested_button_carries_the_origin(self) -> None:
        bad = [b.text for b in _nested(more_menu()) if origin_of(b.callback_data) is None]
        assert not bad, f"кнопки «Ещё» без метки возврата: {bad}"

    def test_more_menu_itself_returns_to_main(self) -> None:
        back = [b for b in _buttons(more_menu()) if b.text.startswith("‹")]
        assert [b.callback_data for b in back] == ["menu:open"]


class TestBackTarget:
    def test_from_more_goes_back_to_more(self) -> None:
        assert back_target(ORIGIN_MORE) == ("‹ Ещё", "menu:more")

    def test_without_origin_goes_to_menu(self) -> None:
        assert back_target(None) == ("‹ Меню", "menu:open")

    def test_back_to_menu_keyboard_follows_the_origin(self) -> None:
        assert _buttons(back_to_menu(ORIGIN_MORE))[0].callback_data == "menu:more"
        assert _buttons(back_to_menu())[0].callback_data == "menu:open"


class TestReferral:
    def test_from_more_returns_to_more(self) -> None:
        """Рефералка возвращала в «Баланс» — экран, на котором человек, пришедший
        из «Ещё», вообще не был."""
        datas = [b.callback_data for b in _buttons(referral_kb(ORIGIN_MORE))]
        assert "menu:more" in datas
        assert "bal:my" not in datas

    def test_from_balance_returns_to_balance(self) -> None:
        datas = [b.callback_data for b in _buttons(referral_kb())]
        assert "bal:my" in datas

    def test_rename_button_keeps_the_origin(self) -> None:
        """Иначе после переименования ссылки человек уезжает в «Баланс»."""
        edit = [b for b in _buttons(referral_kb(ORIGIN_MORE)) if "refedit" in b.callback_data]
        assert edit and origin_of(edit[0].callback_data) == ORIGIN_MORE


class TestNotify:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_toggle_keeps_the_origin(self, enabled: bool) -> None:
        """Экран перерисовывается после переключения — и адрес возврата не
        должен меняться на полпути."""
        kb = notify_settings_kb(enabled, ORIGIN_MORE)
        toggle = [b for b in _buttons(kb) if "notify_toggle" in b.callback_data]
        assert toggle and origin_of(toggle[0].callback_data) == ORIGIN_MORE
        assert any(b.callback_data == "menu:more" for b in _buttons(kb))

    def test_without_origin_nothing_changes(self) -> None:
        kb = notify_settings_kb(True)
        assert any(b.callback_data == "menu:notify_toggle" for b in _buttons(kb))
        assert any(b.callback_data == "menu:open" for b in _buttons(kb))


# --- Живые ли кнопки ---------------------------------------------------------

class _FakeCall:
    """Достаточно для магических фильтров aiogram: они смотрят только `.data`."""

    def __init__(self, data: str) -> None:
        self.data = data


def _handled_by(router, data: str) -> bool:
    call = _FakeCall(data)
    for handler in router.callback_query.handlers:
        try:
            if all(bool(f.callback(call)) for f in handler.filters or []):
                return True
        except Exception:  # фильтр, которому нужен настоящий объект события
            continue
    return False


def _routers():
    from bot.handlers import balance, common, legal

    return [common.router, legal.router, balance.router]


class TestButtonsAreAlive:
    """Кнопка с меткой возврата, которую никто не ловит, крутит спиннер до
    таймаута — и выглядит как зависший бот."""

    @pytest.mark.parametrize("data", [b.callback_data for b in _nested(more_menu())])
    def test_every_more_button_has_a_handler(self, data: str) -> None:
        assert any(_handled_by(r, data) for r in _routers()), f"некому обработать {data}"

    @pytest.mark.parametrize(
        "data",
        ["menu:notify_toggle:more", "bal:refedit:more", "menu:more", "menu:howto"],
    )
    def test_inner_buttons_have_a_handler(self, data: str) -> None:
        assert any(_handled_by(r, data) for r in _routers()), f"некому обработать {data}"

    def test_the_scanner_can_fail(self) -> None:
        """Проверка проверки: молчащий сторож выглядит как сторож, которому
        нечего сказать (урок 20.08.2026)."""
        assert not any(_handled_by(r, "menu:nosuchscreen:more") for r in _routers())
