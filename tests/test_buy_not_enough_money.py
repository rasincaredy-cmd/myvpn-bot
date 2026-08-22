"""Покупка при нехватке денег обязана назвать сумму, а не упасть.

Повод: 11.08.2026 вечером Влад трижды нажал «Купить» с пустым балансом и
трижды получил «что-то пошло не так». Откат транзакции в аварийной ветке
гасит загруженного юзера, и следующее же чтение его баланса лезет в базу
из места, где ждать нельзя (та же мина, что описана в config_move.py).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.services.pricing import monthly_price_kopeks


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
    def __init__(self, data: str, uid: int) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


async def test_screen_names_the_missing_sum(session: AsyncSession) -> None:
    from bot.handlers.balance import cb_bal_buy

    user = await repo.get_or_create_user(
        session, tg_id=4501, username="u", full_name="U"
    )
    user.balance_kopeks = 50_00
    await session.commit()

    call = _FakeCall("bal:buy:1:1:1", 4501)
    await cb_bal_buy(call, session)

    # С 22.08.2026 это ЭКРАН, а не всплывашка: человек упёрся в отказ ровно
    # тогда, когда собрался платить, и ему нужен следующий шаг, а не «ок».
    price = monthly_price_kopeks(1, 1)          # 120 ₽ за 1 устр. + 1 обход
    assert call.message.texts, "юзеру вообще ничего не показали"
    screen = call.message.texts[-1]
    assert "70" in screen, f"не названа нехватка 70 ₽: {screen}"
    assert str(price // 100) in screen, f"не названа цена: {screen}"

    # И выход с экрана — сразу на пополнение нужной суммы.
    buttons = [
        b for row in call.message.markups[-1].inline_keyboard for b in row
    ]
    assert any("Пополнить на 70 ₽" in b.text for b in buttons), [b.text for b in buttons]
    assert any(b.callback_data == "bal:need:70" for b in buttons)


async def test_balance_survives_the_failed_purchase(session: AsyncSession) -> None:
    """Откат не должен списать деньги и не должен «потерять» юзера."""
    from bot.handlers.balance import cb_bal_buy

    user = await repo.get_or_create_user(
        session, tg_id=4502, username="u", full_name="U"
    )
    user.balance_kopeks = 50_00
    await session.commit()

    await cb_bal_buy(_FakeCall("bal:buy:1:1:1", 4502), session)

    fresh = await repo.get_user_by_tg_id(session, 4502)
    assert fresh is not None
    assert fresh.balance_kopeks == 50_00
