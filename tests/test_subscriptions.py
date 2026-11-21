"""Тесты выдачи и форматов персонализированных подписок (/sub/{token})."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, Server, ServerStatus, User, WdttAccess
from bot.services import subscriptions
from bot.services.amnezia import AmneziaParams
from bot.services.crypto import encrypt


@pytest.mark.asyncio
async def test_sub_token_generation_and_lookup(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(session, tg_id=999001, username="sub_test", full_name="Sub Test")
    token = await repo.get_or_create_sub_token(session, user.id)
    assert token is not None
    assert len(token) > 20

    # Повторный вызов возвращает тот же токен
    token2 = await repo.get_or_create_sub_token(session, user.id)
    assert token == token2

    # Поиск юзера по токену
    found = await repo.get_user_by_sub_token(session, token)
    assert found is not None
    assert found.id == user.id

    # Ротация токена
    new_token = await repo.rotate_sub_token(session, user.id)
    assert new_token != token
    assert await repo.get_user_by_sub_token(session, token) is None
    assert (await repo.get_user_by_sub_token(session, new_token)).id == user.id


@pytest.mark.asyncio
async def test_build_subscription_payload(session: AsyncSession) -> None:
    # 1. Создаем пользователя с активной подпиской
    user = await repo.get_or_create_user(session, tg_id=999002, username="sub_user2", full_name="User Two")
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    user.sub_traffic_limit_bytes = 10 * 1024**3
    user.is_trial = False
    await session.commit()

    # 2. Создаем сервер
    params = AmneziaParams(Jc=3, Jmin=40, Jmax=800, S1=0, S2=0, H1=1, H2=2, H3=3, H4=4)
    server = Server(
        name="Сервер Нидерланды",
        host="48.102.173.96",
        ssh_port=22,
        wg_port=585,
        wg_interface="awg0",
        wg_subnet="10.8.0.0/24",
        server_public_key="pubkey_server_1234567890123456789012345678=",
        server_endpoint="48.102.173.96:585",
        awg_params_json=params.to_json(),
        status=ServerStatus.READY,
        owner_tg_id=0,
        location="🇳🇱 Нидерланды",
        wdtt_enabled=True,
    )
    session.add(server)
    await session.flush()

    # 3. Добавляем пир WireGuard
    peer = Peer(
        server_id=server.id,
        user_id=user.id,
        label="Телефон",
        ip="10.8.0.2",
        public_key="pubkey_peer_123=",
        private_key_enc=encrypt("privkey_peer_123="),
        status=PeerStatus.ACTIVE,
        traffic_used_bytes=50 * 1024**2,
    )
    session.add(peer)

    # 4. Добавляем доступ обхода БС (qWDTT)
    wdtt = WdttAccess(
        server_id=server.id,
        user_id=user.id,
        label="Обход БС",
        uri_enc=encrypt("wdtt://127.0.0.1:56000:56001:9000:pass123:vk.com/call/join/xyz#Обход"),
        password_enc=encrypt("pass123"),
        status=PeerStatus.ACTIVE,
        traffic_used_bytes=10 * 1024**2,
    )
    session.add(wdtt)
    await session.commit()

    # 5. Генерируем подписку
    sub_data = await subscriptions.build_subscription_data(session, user)

    assert sub_data["is_active"] is True
    assert sub_data["subscriptionName"] == "Moschata VPN"
    assert len(sub_data["servers"]) == 1
    assert "🇳🇱 Нидерланды" in sub_data["servers"][0]["name"]
    assert "[Interface]" in sub_data["servers"][0]["config"]
    assert "Jc = 3" in sub_data["servers"][0]["config"]

    assert len(sub_data["profiles"]) == 1
    prof = sub_data["profiles"][0]
    assert "48.102.173.96:56003" in prof["peer"]
    assert prof["password"] == "pass123"
    assert prof["workers"] == 9

    # 6. Проверяем форматирование ответа
    body_json, ctype_json, headers_json = subscriptions.format_subscription_response(sub_data, "json")
    assert ctype_json.startswith("application/json")
    parsed = json.loads(body_json)
    assert parsed["version"] == 1
    assert "Subscription-Userinfo" in headers_json
    assert "upload=0" in headers_json["Subscription-Userinfo"]

    # Проверяем base64
    body_b64, ctype_b64, _ = subscriptions.format_subscription_response(sub_data, "base64")
    assert ctype_b64.startswith("text/plain")
    import base64
    decoded = base64.b64decode(body_b64).decode("utf-8")
    assert "wdtt://" in decoded
