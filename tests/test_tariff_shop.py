"""Витрина тарифов, цена «в месяц» и путь на пополнение (22.08.2026).

Три просьбы Влада одним блоком:
  • покупка начинается с готовых тарифов, а не с конструктора «−/+»;
  • у каждого срока видно, во что он выходит В МЕСЯЦ, — иначе «1440 ₽ за год»
    и «160 ₽ за месяц» несравнимы;
  • «оплатить» без денег ведёт на пополнение нужной суммы, а не в отказ.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.handlers import balance as h
from bot.services.pricing import (
    PRESETS,
    fmt_rub,
    monthly_price_kopeks,
    month_of_term_kopeks,
    per_device_kopeks,
    term_price_kopeks,
    topup_need_rub,
)


class _FakeFrom:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.username = "u"
        self.full_name = "U"


class _FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class _FakeCall:
    def __init__(self, data: str, uid: int = 7700) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)

    def buttons(self) -> list:
        return [b for row in self.message.markups[-1].inline_keyboard for b in row]

    def texts(self) -> list[str]:
        return [b.text for b in self.buttons()]

    def datas(self) -> list[str]:
        return [b.callback_data for b in self.buttons() if b.callback_data]


async def _user(session: AsyncSession, uid: int = 7700, *, balance: int = 0):
    user = await repo.get_or_create_user(session, tg_id=uid, username="u", full_name="U")
    user.balance_kopeks = balance
    await session.commit()
    return user


class TestMonthlyOnTermButtons:
    def test_long_terms_show_price_per_month(self) -> None:
        rows = dict(h._term_price_rows(1, 1))
        year = term_price_kopeks(monthly_price_kopeks(1, 1), 12)
        assert fmt_rub(year) in rows[12]
        assert f"{fmt_rub(month_of_term_kopeks(year, 12))}/мес" in rows[12]

    def test_single_month_says_it_once(self) -> None:
        """«120 ₽ · 120 ₽/мес» — это одно и то же число дважды."""
        assert "/мес" not in dict(h._term_price_rows(1, 1))[1]

    def test_per_month_falls_as_the_term_grows(self) -> None:
        """Ради этой цифры всё и делалось: выгода срока видна в рублях."""
        rows = h._term_price_rows(2, 1)
        monthly = monthly_price_kopeks(2, 1)
        per = [
            month_of_term_kopeks(term_price_kopeks(monthly, months), months)
            for months, _label in rows
        ]
        assert per == sorted(per, reverse=True), per
        assert per[0] > per[-1]


class TestShopRows:
    def test_every_preset_gets_a_button_with_its_real_price(self) -> None:
        buttons, facts = h._shop_rows(None)
        assert [key for key, _ in buttons] == [p.key for p in PRESETS]
        for (_key, label), preset in zip(buttons, PRESETS):
            monthly = monthly_price_kopeks(preset.devices, preset.bypass)
            assert fmt_rub(monthly) in label
            assert preset.name in label
        assert len(facts) == len(PRESETS)

    def test_best_value_badge_stands_on_the_cheapest_device(self) -> None:
        """Метка считается из цен, а не проставлена руками: поменяются цены —
        переедет сама. Забытая метка была бы уже обманом."""
        _buttons, facts = h._shop_rows(None)
        marked = [f for f in facts if "🔥" in f]
        assert len(marked) == 1
        cheapest = min(
            PRESETS,
            key=lambda p: per_device_kopeks(
                monthly_price_kopeks(p.devices, p.bypass), p.devices
            ) or 10 ** 9,
        )
        assert cheapest.name in marked[0]

    def test_multi_device_presets_show_price_per_device(self) -> None:
        _buttons, facts = h._shop_rows(None)
        for fact, preset in zip(facts, PRESETS):
            monthly = monthly_price_kopeks(preset.devices, preset.bypass)
            if preset.devices > 1:
                assert fmt_rub(per_device_kopeks(monthly, preset.devices)) in fact


class TestShopScreen:
    async def test_buying_starts_with_ready_made_tariffs(self, session: AsyncSession) -> None:
        await _user(session)
        call = _FakeCall("bal:extend")
        await h.cb_bal_shop(call, session)
        datas = call.datas()
        for preset in PRESETS:
            assert f"bal:pre:{preset.key}" in datas
        assert any(d.startswith("bal:ext:") for d in datas), "конструктор пропал"
        assert any("Собрать свой" in t for t in call.texts())

    async def test_preset_leads_to_terms_without_steppers(self, session: AsyncSession) -> None:
        await _user(session)
        call = _FakeCall("bal:pre:duo")
        await h.cb_bal_preset(call, session)
        texts = call.texts()
        assert "−" not in texts and "+" not in texts
        assert any(d == "bal:buy:2:1:12" for d in call.datas()), call.datas()

    async def test_unknown_preset_does_not_crash(self, session: AsyncSession) -> None:
        await _user(session)
        call = _FakeCall("bal:pre:zzz")
        await h.cb_bal_preset(call, session)
        assert call.answers and "нет" in call.answers[0].lower()

    async def test_perpetual_subscription_has_nothing_to_buy(self, session: AsyncSession) -> None:
        user = await _user(session)
        user.sub_expires_at = None
        user.is_trial = False
        await session.commit()
        call = _FakeCall("bal:extend")
        await h.cb_bal_shop(call, session)
        assert call.answers and "бессрочная" in call.answers[0]
        assert not call.message.texts


class TestTopUpForTheMissingSum:
    def test_missing_sum_rounds_up_to_a_tenner(self) -> None:
        # Правило живёт в прайсинге: его спрашивают и бот, и мини-приложение.
        assert topup_need_rub(6_150) == 70      # 61.50 ₽ → 70 ₽
        assert topup_need_rub(7_000) == 70
        assert topup_need_rub(50) == 10         # меньше минимума — минимум

    async def test_screen_offers_every_payment_method(self, session: AsyncSession) -> None:
        await _user(session)
        call = _FakeCall("bal:need:70")
        await h.cb_bal_need(call, session)
        datas = call.datas()
        # Сумма едет в каждом способе: спрашивать её второй раз значит терять
        # того, кто уже достал карту.
        assert "bal:star:70" in datas
        assert any(d.endswith(":70") for d in datas)
        assert any("70 ₽" in t for t in call.message.texts)

    async def test_bad_amount_is_refused(self, session: AsyncSession) -> None:
        await _user(session)
        call = _FakeCall("bal:need:999999999")
        await h.cb_bal_need(call, session)
        assert call.answers and "Некорректная" in call.answers[0]


class TestNoDeadButtons:
    """Каждая новая кнопка обязана дойти до обработчика — и до СВОЕГО.

    Порядок фильтров в aiogram решает: побеждает первый совпавший. Экраны
    покупки висят на соседних префиксах (`pre:`, `pick:`, `pg:`, `pgchk:`), и
    один неудачный `startswith` молча съедает чужие нажатия — кнопка при этом
    выглядит живой и крутит спиннер до таймаута.
    """

    @staticmethod
    def _first_handler(data: str) -> str | None:
        from types import SimpleNamespace

        from bot.handlers.balance import router

        event = SimpleNamespace(data=data)
        for handler in router.callback_query.handlers:
            try:
                if all(f.callback(event) for f in handler.filters):
                    return handler.callback.__name__
            except Exception:
                continue
        return None

    def test_every_purchase_button_reaches_its_own_handler(self) -> None:
        expected = {
            "bal:extend": "cb_bal_shop",
            "bal:shop": "cb_bal_shop",
            "bal:pre:solo": "cb_bal_preset",
            "bal:pick:2:1": "cb_bal_pick",
            "bal:need:70": "cb_bal_need",
            "bal:ext:1:1": "cb_bal_extend_adjust",
            "bal:buy:1:1:1": "cb_bal_buy",
            "bal:dep:70": "cb_bal_deposit_amount",
            "bal:star:70": "cb_bal_star_amount",
            "bal:pg:70": "cb_bal_platega_amount",
        }
        got = {data: self._first_handler(data) for data in expected}
        assert got == expected

    def test_the_guard_would_notice_a_dead_button(self) -> None:
        """Молчащий сторож выглядит как сторож, которому нечего сказать."""
        assert self._first_handler("bal:tariff-that-never-was") is None
