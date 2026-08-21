"""Списочные экраны не должны молча исчезать, упершись в потолок Telegram.

Сообщение длиннее 4096 символов Telegram не обрезает — он его НЕ ПРИНИМАЕТ.
Экран трафика сервера собирался из всех пиров одним куском и переставал
отправляться примерно на 34-м пире (аудит 20.08.2026; на тот момент их было 14
и 10 — то есть до поломки оставалось меньше чем вдвое).
"""
from __future__ import annotations

import pytest

from bot.utils.tgtext import TG_TEXT_LIMIT, fit_to_message


def _lines(n: int, size: int = 110) -> list[str]:
    return ["📊 <b>Заголовок</b>"] + [f"строка {i} " + "x" * size for i in range(n)]


class TestFit:
    def test_short_list_untouched(self) -> None:
        out = fit_to_message(_lines(5))
        assert "показаны первые" not in out
        assert out.count("строка ") == 5

    @pytest.mark.parametrize("n", [1, 10, 34, 50, 200])
    def test_always_fits(self, n: int) -> None:
        assert len(fit_to_message(_lines(n))) <= TG_TEXT_LIMIT

    def test_truncation_is_never_silent(self) -> None:
        """Молча укороченный список читается как «больше ничего нет» — и по нему
        принимают решения."""
        out = fit_to_message(_lines(200))
        assert "показаны первые" in out
        assert "из 200" in out

    def test_header_always_survives(self) -> None:
        """Без заголовка непонятно, что вообще на экране."""
        assert "📊 <b>Заголовок</b>" in fit_to_message(_lines(200))

    def test_empty_input(self) -> None:
        assert fit_to_message([]) == ""

    def test_header_only(self) -> None:
        assert fit_to_message(["только заголовок"]) == "только заголовок"
