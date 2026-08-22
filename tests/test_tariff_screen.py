"""Экран «⚙️ Тариф» и его клавиатура (Блоки «Тариф» и «Облик»).

До 20.08.2026 экран назывался «Продление подписки», кнопки смены тарифа не
существовало, а справка про расчёт цены занимала абзац прямо на экране.
"""
from __future__ import annotations

import pytest

from bot.keyboards.inline import tariff_kb


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _datas(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


TERMS = [(1, "месяц — 160 ₽"), (12, "год — 1440 ₽")]


class TestTariffKeyboard:
    def test_has_steppers_for_both_positions(self) -> None:
        texts = _texts(tariff_kb(2, 1, TERMS, 10, 10, switch_days=None))
        assert "📱 2" in texts and "⚡ 1" in texts
        assert texts.count("−") == 2 and texts.count("+") == 2

    def test_switch_button_names_the_resulting_term(self) -> None:
        """Кнопка обязана назвать исход: человек меняет тариф и должен видеть,
        во что превратится его срок, ДО нажатия."""
        texts = _texts(tariff_kb(2, 1, TERMS, 10, 10, switch_days=168))
        assert any("168" in t for t in texts), texts

    def test_no_switch_button_when_tariff_unchanged(self) -> None:
        """Тариф не тронут — менять нечего, кнопка не рисуется."""
        texts = _texts(tariff_kb(2, 1, TERMS, 10, 10, switch_days=None))
        assert not any("Сменить" in t for t in texts)

    def test_term_buttons_carry_the_tariff(self) -> None:
        """Тариф едет в callback_data покупки: FSM здесь не используется, и
        состояние экрана обязано жить в самих кнопках."""
        datas = _datas(tariff_kb(3, 2, TERMS, 10, 10, switch_days=None))
        assert "bal:buy:3:2:12" in datas

    def test_minus_is_dead_at_zero(self) -> None:
        """На нуле «−» — заглушка, а не живая кнопка: иначе Telegram крутит
        спиннер до таймаута на действии, которого нет."""
        kb = tariff_kb(0, 1, TERMS, 10, 10, switch_days=None)
        first_row = kb.inline_keyboard[0]
        assert first_row[0].callback_data == "nop"

    def test_plus_is_dead_at_ceiling(self) -> None:
        kb = tariff_kb(10, 1, TERMS, 10, 10, switch_days=None)
        assert kb.inline_keyboard[0][2].callback_data == "nop"

    def test_last_position_cannot_be_removed(self) -> None:
        """Тариф 0+0 не существует — последнюю позицию убрать нельзя."""
        kb = tariff_kb(1, 0, TERMS, 10, 10, switch_days=None)
        assert kb.inline_keyboard[0][0].callback_data == "nop"

    def test_always_offers_topup_and_way_back(self) -> None:
        # Возврат ведёт в витрину тарифов (с 22.08.2026 покупка начинается с
        # неё), а оттуда уже в «Подписку».
        texts = " ".join(_texts(tariff_kb(1, 1, TERMS, 10, 10, switch_days=None)))
        assert "Пополнить" in texts
        assert "Тарифы" in texts

    def test_preset_mode_hides_steppers_but_keeps_terms(self) -> None:
        """Пришли из витрины — состав уже выбран, и первое решение теперь
        одно: срок. Менять состав можно, но отдельной кнопкой."""
        kb = tariff_kb(2, 1, TERMS, 10, 10, switch_days=None, builder=False)
        texts = _texts(kb)
        assert "−" not in texts and "+" not in texts
        assert any("Изменить состав" in t for t in texts)
        assert "bal:buy:2:1:12" in _datas(kb)


class TestTariffText:
    """Текст экрана — чистая функция: считать, что произойдёт, она не пытается.

    Число дней ей ПЕРЕДАЮТ — то же самое, что уходит на кнопку. Иначе экран и
    кнопка считали бы исход независимо и однажды разошлись бы в цифрах.
    """

    @staticmethod
    def _user(days: int, dev: int, byp: int, trial: bool = False):
        from datetime import datetime, timedelta, timezone

        from bot.db.models import User

        return User(
            tg_id=1, balance_kopeks=340_00,
            sub_max_devices=dev, sub_max_bypass=byp,
            sub_expires_at=datetime.now(timezone.utc) + timedelta(days=days),
            is_trial=trial,
        )

    def test_shows_new_price(self) -> None:
        from bot.handlers.balance import build_tariff_text

        text = build_tariff_text(self._user(365, 1, 0), 2, 0, switch_days=252)
        assert "130 ₽" in text, "цена нового тарифа (90 + 40) не названа"

    def test_names_both_terms_when_switch_is_offered(self) -> None:
        """«Было столько — станет столько»: одна цифра без другой не даёт
        человеку понять, чем он платит за смену.

        Сколько «было» — берём тем же способом, что и код: до полного дня
        остатку не хватает доли секунды, прошедшей с создания юзера, поэтому
        365 выданных суток честно показываются как 364 полных дня.
        """
        from bot.handlers.balance import build_tariff_text
        from bot.services import billing

        user = self._user(365, 1, 0)
        was = billing.remaining_seconds(user) // 86400
        text = build_tariff_text(user, 2, 0, switch_days=252)
        assert str(was) in text and "252" in text

    def test_no_term_talk_when_switch_unavailable(self) -> None:
        from bot.handlers.balance import build_tariff_text

        text = build_tariff_text(self._user(7, 1, 1, trial=True), 2, 1, switch_days=None)
        assert "станет" not in text

    def test_help_is_collapsed(self) -> None:
        """Объяснение «как считается цена» обязано быть свёрнутым: раньше оно
        занимало абзац прямо на экране."""
        from bot.handlers.balance import build_tariff_text

        text = build_tariff_text(self._user(30, 1, 1), 1, 1, switch_days=None)
        assert "<blockquote expandable>" in text

    def test_balance_is_visible(self) -> None:
        """С этого экрана покупают — остаток на балансе должен быть перед
        глазами, а не в другом разделе."""
        from bot.handlers.balance import build_tariff_text

        text = build_tariff_text(self._user(30, 1, 1), 1, 1, switch_days=None)
        assert "340 ₽" in text


class TestPreview:
    """«Сухой прогон» смены: тот же расчёт и те же запреты, но без записи."""

    @pytest.mark.asyncio
    async def test_dry_run_does_not_touch_the_user(self, session) -> None:
        from datetime import datetime, timedelta, timezone

        from bot.db import repo
        from bot.services import billing

        user = await repo.get_or_create_user(session, tg_id=901, username="u", full_name="U")
        user.sub_max_devices, user.sub_max_bypass = 1, 0
        user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=365)
        user.is_trial = False
        await session.flush()

        res = await billing.change_tariff(
            session, user, max_devices=2, max_bypass=0, dry_run=True
        )
        assert res.ok and res.new_days == 252
        assert user.sub_max_devices == 1, "сухой прогон изменил тариф"
        assert (user.sub_expires_at - datetime.now(timezone.utc)).days >= 364, \
            "сухой прогон сдвинул срок"
