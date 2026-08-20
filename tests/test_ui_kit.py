"""Дизайн-система экранов (Блок «Облик»).

Правила интерфейса живут кодом, а не договорённостью: заголовок, строки-факты,
свёрнутая справка. Тесты стерегут ровно те свойства, которые ломаются молча —
экранирование чужого текста и разрастание экрана.
"""
from __future__ import annotations

import pytest

from bot.texts import ui


class TestPieces:
    def test_title_is_one_emoji_and_bold_noun(self) -> None:
        assert ui.title("🎫", "Подписка") == "🎫 <b>Подписка</b>"

    def test_fact_puts_value_in_bold(self) -> None:
        assert ui.fact("📱", "Устройства", "2 из 3") == "📱 Устройства — <b>2 из 3</b>"

    def test_help_is_a_collapsed_blockquote(self) -> None:
        """Справка обязана быть СВЁРНУТОЙ: смысл приёма в том, что длинное
        объяснение не занимает экран, пока его не попросят."""
        out = ui.help_block("Как это работает", "Долгое объяснение.")
        assert out.startswith("<blockquote expandable>")
        assert "Как это работает" in out


class TestEscaping:
    """Чужой текст в HTML-разметке.

    Найдено 20.08.2026: имя из профиля Telegram подставлялось в приветствие
    как есть. Имя вида «<Вася>» делает сообщение непарсимым, Telegram его не
    принимает — и человек не может запустить бота ВООБЩЕ, ни разу.
    """

    def test_fact_escapes_value(self) -> None:
        out = ui.fact("📱", "Устройство", "<b>Вася</b>")
        assert "<b>Вася</b>" not in out
        assert "&lt;b&gt;" in out

    def test_title_escapes_name(self) -> None:
        assert "&lt;" in ui.title("👋", "<script>")

    def test_safe_passes_plain_text_through(self) -> None:
        assert ui.safe("Обычное имя") == "Обычное имя"

    def test_safe_handles_none(self) -> None:
        assert ui.safe(None) == ""


class TestScreen:
    def test_screen_stacks_head_lead_facts(self) -> None:
        out = ui.screen(
            head=ui.title("🎫", "Подписка"),
            lead="Активна ещё 25 дней.",
            facts=[ui.fact("📱", "Устройства", "2 из 3")],
        )
        assert out.index("🎫") < out.index("Активна") < out.index("📱")

    def test_screen_never_leaves_triple_newline(self) -> None:
        """Пустой блок не должен оставлять дыру: экран с пропущенным lead
        обязан выглядеть так же плотно, как экран без него."""
        out = ui.screen(head="H", lead=None, facts=["a"], note=None, help=None)
        assert "\n\n\n" not in out

    def test_screen_rejects_too_many_facts(self) -> None:
        """Больше пяти фактов — это уже простыня, а не экран. Ограничение
        стоит в коде, чтобы следующий экран не расползся незаметно."""
        with pytest.raises(ValueError):
            ui.screen(head="H", facts=[f"f{i}" for i in range(ui.MAX_FACTS + 1)])

    def test_screen_allows_the_limit(self) -> None:
        ui.screen(head="H", facts=[f"f{i}" for i in range(ui.MAX_FACTS)])
