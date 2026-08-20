"""«Ссылками» на несколько локаций не должно упираться в лимит Telegram.

Найдено аудитом 20.08.2026 с реальными цифрами: одна vpn://-ссылка для
AmneziaWG — 962 символа, а потолок сообщения 4096. Все ссылки складывались в
ОДНО сообщение, поэтому при четырёх локациях отправка падала и человек не
получал ничего. У сервиса на тот момент было две локации и куплена третья —
то есть до поломки оставалась одна страна.
"""
from __future__ import annotations

import pytest

from bot.handlers.config_delivery import TG_TEXT_LIMIT, pack_link_messages


def _block(n: int, size: int = 962) -> str:
    return f"<b>Локация {n}</b>\n<code>" + "x" * size + "</code>"


class TestPacking:
    def test_single_block_stays_one_message(self) -> None:
        out = pack_link_messages([_block(1)], "ЗАГОЛОВОК", "ПОДВАЛ")
        assert len(out) == 1
        assert "ЗАГОЛОВОК" in out[0] and "ПОДВАЛ" in out[0]

    def test_splits_when_over_the_limit(self) -> None:
        out = pack_link_messages([_block(i) for i in range(6)], "ЗАГОЛОВОК", "ПОДВАЛ")
        assert len(out) > 1

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 6, 10, 25])
    def test_every_message_fits(self, count: int) -> None:
        out = pack_link_messages([_block(i) for i in range(count)], "ЗАГОЛОВОК", "ПОДВАЛ")
        for msg in out:
            assert len(msg) <= TG_TEXT_LIMIT, f"сообщение на {len(msg)} символов"

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 6, 10, 25])
    def test_nothing_is_lost(self, count: int) -> None:
        """Ни одна локация не должна потеряться при разбиении — иначе человек
        молча не получит часть конфигов."""
        blocks = [_block(i) for i in range(count)]
        joined = "\n".join(pack_link_messages(blocks, "ЗАГОЛОВОК", "ПОДВАЛ"))
        for i in range(count):
            assert f"<b>Локация {i}</b>" in joined

    def test_header_only_on_first_footer_only_on_last(self) -> None:
        """Заголовок и подсказка повторяться не должны: три одинаковых шапки
        подряд читаются как сбой бота."""
        out = pack_link_messages([_block(i) for i in range(6)], "ЗАГОЛОВОК", "ПОДВАЛ")
        assert sum("ЗАГОЛОВОК" in m for m in out) == 1
        assert sum("ПОДВАЛ" in m for m in out) == 1
        assert "ЗАГОЛОВОК" in out[0]
        assert "ПОДВАЛ" in out[-1]

    def test_oversized_single_block_is_not_dropped(self) -> None:
        """Блок, который сам по себе длиннее лимита, отдаём как есть: пусть
        Telegram ругнётся, но молча потерять конфиг нельзя."""
        out = pack_link_messages([_block(1, size=5000)], "ЗАГОЛОВОК", "ПОДВАЛ")
        assert any("x" * 5000 in m for m in out)
