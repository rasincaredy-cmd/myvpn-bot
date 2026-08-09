"""Оплата звёздами: пересчёт и защита от двойного зачисления."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import StarPayment
from bot.keyboards.inline import star_invoice_kb
from bot.services.pricing import monthly_price_kopeks, stars_for_kopeks


class TestStarPrice:
    def test_markup_is_twentyfive_percent(self) -> None:
        assert stars_for_kopeks(120_00) == 150
        assert stars_for_kopeks(160_00) == 200
        assert stars_for_kopeks(1080_00) == 1350

    def test_fraction_rounds_up(self) -> None:
        """Дробной звезды не бывает. Округление вниз означало бы, что сервис
        дарит долю звезды на каждой покупке."""
        assert stars_for_kopeks(90_00) == 113        # 112,5 → 113

    def test_typical_tariff_in_stars(self) -> None:
        assert stars_for_kopeks(monthly_price_kopeks(1, 1)) == 150


class TestStarInvoiceKeyboard:
    """Повод: 9.08.2026 Влад нажал «Пополнить → Звёзды» и уткнулся в счёт, из
    которого нечем выйти, — Telegram рисует у счёта одну кнопку «Оплатить»."""

    def test_has_way_out(self) -> None:
        rows = star_invoice_kb(150).inline_keyboard
        cancels = [b for row in rows for b in row if b.callback_data]
        assert cancels, "из счёта в звёздах нечем выйти"

    def test_pay_button_goes_first(self) -> None:
        """Telegram не примет клавиатуру счёта, где pay-кнопка не первая."""
        first = star_invoice_kb(150).inline_keyboard[0][0]
        assert first.pay is True


class _FakeMessage:
    """Сообщение, которое помнит, что через него отправляли."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.invoices: list[dict] = []
        self.texts: list[str] = []

    async def answer_invoice(self, **kwargs) -> None:
        self.invoices.append(kwargs)

    async def answer(self, text: str, **kwargs) -> None:
        self.texts.append(text)


class _FakeFrom:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.username = "u"
        self.full_name = "U"


class _FakeCall:
    def __init__(self, data: str, uid: int) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


async def _fsm(**data):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1)
    )
    if data:
        await state.update_data(**data)
    return state


class TestStarInvoiceIsActuallySent:
    """К счёту ведут ДВА пути — кнопка с готовой суммой и «своя сумма», — и
    ошибиться можно в каждом по отдельности: 9.08.2026 второй передавал в
    отправку не сообщение, а его метод, и падал бы у первого же юзера."""

    async def test_amount_button(self, session: AsyncSession) -> None:
        from bot.handlers.balance import cb_bal_star_amount

        call = _FakeCall("bal:star:120", 4310)
        await cb_bal_star_amount(call, session)

        assert len(call.message.invoices) == 1
        inv = call.message.invoices[0]
        assert inv["currency"] == "XTR"
        assert inv["prices"][0].amount == stars_for_kopeks(120_00)

    async def test_custom_amount(self, session: AsyncSession) -> None:
        from bot.handlers.balance import step_bal_custom_amount

        message = _FakeMessage("300")
        message.from_user = _FakeFrom(4311)
        await step_bal_custom_amount(
            message, await _fsm(method="stars"), session
        )

        assert len(message.invoices) == 1, "своя сумма звёздами счёт не выставила"
        assert message.invoices[0]["prices"][0].amount == stars_for_kopeks(300_00)


class TestStarCredit:
    async def test_credits_balance_once(self, session: AsyncSession) -> None:
        """Повторно доставленный платёж не должен зачислиться дважды."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4301, username="u", full_name="U"
        )
        await session.commit()

        first = await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-1",
            amount_kopeks=120_00, stars=150,
        )
        await session.commit()
        second = await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-1",
            amount_kopeks=120_00, stars=150,
        )
        await session.commit()

        assert first.credited is True
        assert second.credited is False, "повторная доставка зачислила деньги второй раз"
        assert user.balance_kopeks == 120_00

    async def test_no_bonus_on_stars(self, session: AsyncSession) -> None:
        """У звёзд своя наценка 25 % — бонус поверх неё был бы
        взаимоисключающим."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4302, username="u", full_name="U"
        )
        await session.commit()

        await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-2",
            amount_kopeks=100_00, stars=125,
        )
        await session.commit()

        rows = await repo.list_balance_txs(session, user.id, limit=10)
        assert {r.kind for r in rows} == {"deposit"}
        assert user.balance_kopeks == 100_00

    async def test_referrer_gets_his_percent(self, session: AsyncSession) -> None:
        """Пополнение звёздами — такое же пополнение: пригласивший получает
        свои проценты, иначе способ оплаты молча отменял бы рефералку."""
        from bot.services import stars as stars_svc

        boss = await repo.get_or_create_user(
            session, tg_id=4303, username="boss", full_name="B"
        )
        await session.commit()
        friend = await repo.get_or_create_user(
            session, tg_id=4304, username="f", full_name="F"
        )
        friend.referrer_id = boss.id
        await session.commit()

        await stars_svc.credit_star_payment(
            session, user_id=friend.id, charge_id="ch-3",
            amount_kopeks=200_00, stars=250,
        )
        await session.commit()

        assert boss.balance_kopeks == 200_00 * settings.referral_percent // 100

    async def test_wipe_takes_star_payments_too(self, session: AsyncSession) -> None:
        """«Сотрите мои данные» обязано унести и звёздные платежи: это такая же
        история оплат, как инвойсы."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4305, username="u", full_name="U"
        )
        await session.commit()
        await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-4",
            amount_kopeks=100_00, stars=125,
        )
        await session.commit()

        purged = await repo.purge_user_records(session, user.id)
        await session.commit()

        assert purged["star_payments"] == 1
        assert await session.get(StarPayment, "ch-4") is None
