"""Тесты приёмника уведомлений Platega.

Уведомление приходит POST-ом на наш адрес и несёт статус платежа. Проверяем
ровно то, от чего зависят деньги: чужой запрос отбит, свой — зачислен один раз,
а тело уведомления не может ни назначить получателя, ни изменить сумму.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.services import platega_webhook


async def _user(session: AsyncSession, tg_id: int = 801):
    return await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )


async def _payment(session: AsyncSession, user, tx_id: str, kopeks: int = 300_00):
    row = await repo.create_platega_payment(
        session, user_id=user.id, transaction_id=tx_id,
        amount_kopeks=kopeks, url="https://pay.platega.io?id=" + tx_id,
    )
    await session.commit()
    return row


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platega_webhook.settings, "platega_merchant_id", "mid-1")
    monkeypatch.setattr(platega_webhook.settings, "platega_secret", "sec-1")


class TestAuth:
    def test_our_keys_pass(self) -> None:
        assert platega_webhook.headers_ok(
            {"x-merchantid": "mid-1", "x-secret": "sec-1"}
        ) is True

    def test_case_of_header_names_ignored(self) -> None:
        """Заголовки они шлют то так, то эдак — регистр не должен решать."""
        assert platega_webhook.headers_ok(
            {"X-MerchantId": "mid-1", "X-Secret": "sec-1"}
        ) is True

    def test_wrong_secret_rejected(self) -> None:
        assert platega_webhook.headers_ok(
            {"x-merchantid": "mid-1", "x-secret": "чужой"}
        ) is False

    def test_missing_headers_rejected(self) -> None:
        assert platega_webhook.headers_ok({}) is False


class TestHandling:
    @pytest.mark.asyncio
    async def test_confirmed_credits_once(self, session: AsyncSession) -> None:
        user = await _user(session)
        row = await _payment(session, user, "wh-1", 300_00)
        body = {"id": "wh-1", "status": "CONFIRMED", "amount": 300, "currency": "RUB"}

        first = await platega_webhook.handle_payload(session, body)
        second = await platega_webhook.handle_payload(session, body)
        await session.refresh(user)
        await session.refresh(row)

        assert first.credited is True
        assert second.credited is False   # повторную доставку глотаем
        assert user.balance_kopeks == 300_00
        assert row.status == "paid"

    @pytest.mark.asyncio
    async def test_amount_comes_from_our_row(self, session: AsyncSession) -> None:
        """Сумма берётся из нашей строки, а не из тела запроса: иначе кто угодно
        со знанием id транзакции нарисовал бы себе любой баланс."""
        user = await _user(session, tg_id=802)
        await _payment(session, user, "wh-2", 100_00)
        body = {"id": "wh-2", "status": "CONFIRMED", "amount": 1_000_000}

        await platega_webhook.handle_payload(session, body)
        await session.refresh(user)
        assert user.balance_kopeks == 100_00

    @pytest.mark.asyncio
    async def test_unknown_transaction_ignored(self, session: AsyncSession) -> None:
        """Чужой id (у них по id отдаются и чужие транзакции) не создаёт денег."""
        user = await _user(session, tg_id=803)
        res = await platega_webhook.handle_payload(
            session, {"id": "не-наш", "status": "CONFIRMED", "amount": 999}
        )
        await session.refresh(user)
        assert res is None
        assert user.balance_kopeks == 0

    @pytest.mark.asyncio
    async def test_canceled_closes_row(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=804)
        row = await _payment(session, user, "wh-3", 100_00)
        await platega_webhook.handle_payload(
            session, {"id": "wh-3", "status": "CANCELED"}
        )
        await session.refresh(row)
        await session.refresh(user)
        assert row.status == "canceled"
        assert user.balance_kopeks == 0

    @pytest.mark.asyncio
    async def test_capitalized_keys_understood(self, session: AsyncSession) -> None:
        """В их документации поле называется «Id» с большой буквы, а в живом
        ответе API — «id». Понимать надо оба."""
        user = await _user(session, tg_id=805)
        await _payment(session, user, "wh-4", 150_00)
        await platega_webhook.handle_payload(
            session, {"Id": "wh-4", "Status": "CONFIRMED"}
        )
        await session.refresh(user)
        assert user.balance_kopeks == 150_00

    @pytest.mark.asyncio
    async def test_pending_changes_nothing(self, session: AsyncSession) -> None:
        user = await _user(session, tg_id=806)
        row = await _payment(session, user, "wh-5", 100_00)
        await platega_webhook.handle_payload(
            session, {"id": "wh-5", "status": "PENDING"}
        )
        await session.refresh(row)
        await session.refresh(user)
        assert row.status == "pending"
        assert user.balance_kopeks == 0


class TestHttpLayer:
    """Сам эндпоинт: что отвечает и кого пускает. База здесь не нужна —
    запросы либо не доходят до неё, либо это проверка адреса кабинетом.

    Клиент поднимаем средствами самого aiohttp: плагина pytest-aiohttp в
    проекте нет, и тащить зависимость ради трёх проверок незачем."""

    @staticmethod
    async def _post(data: str, headers: dict | None = None) -> int:
        from aiohttp.test_utils import TestClient, TestServer

        from bot.services import webserver

        async with TestClient(TestServer(webserver.build_app())) as client:
            resp = await client.post(
                "/platega/webhook", data=data, headers=headers or {}
            )
            return resp.status

    @pytest.mark.asyncio
    async def test_stranger_gets_401(self) -> None:
        assert await self._post("{}") == 401

    @pytest.mark.asyncio
    async def test_empty_post_is_address_check(self) -> None:
        """Сохраняя адрес, провайдер шлёт на него пустой POST и ждёт 200 —
        иначе адрес не сохранится вовсе."""
        status = await self._post(
            "", {"X-MerchantId": "mid-1", "X-Secret": "sec-1"}
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_garbage_body_still_200(self) -> None:
        """На мусор в теле отвечаем 200: 500 заставил бы провайдера долбить
        нас повторами, а обработать это всё равно нечем."""
        status = await self._post(
            "не-json", {"X-MerchantId": "mid-1", "X-Secret": "sec-1"}
        )
        assert status == 200
