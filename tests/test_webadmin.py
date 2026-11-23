"""Тесты авторизации и API веб-панели администратора."""
from __future__ import annotations

import pytest
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.webadmin import auth, views_api


def test_magic_login_token() -> None:
    admin_id = settings.admin_ids[0] if settings.admin_ids else 111
    if admin_id not in settings.admin_ids:
        settings.admin_ids.append(admin_id)

    token = auth.create_magic_login_token(admin_id)
    assert token is not None
    verified = auth.verify_magic_login_token(token)
    assert verified == admin_id

    # Поддельный токен не проходит
    fake_token = token[:-5] + "aaaaa"
    assert auth.verify_magic_login_token(fake_token) is None


def test_session_creation() -> None:
    admin_id = 111
    sess_token = auth.create_session(admin_id)
    assert sess_token in auth._ACTIVE_SESSIONS
    assert auth._ACTIVE_SESSIONS[sess_token].admin_id == admin_id


@pytest.mark.asyncio
async def test_api_overview(session: AsyncSession) -> None:
    # Создаем тестового пользователя
    await repo.get_or_create_user(session, tg_id=888001, username="admin_tester", full_name="Tester")

    # Получаем данные сводки
    data = await views_api.get_overview_data(session)
    assert "users" in data
    assert "infrastructure" in data
    assert "finance" in data
    assert data["users"]["total"] >= 1


@pytest.mark.asyncio
async def test_api_users_list_handles_naive_datetime(session: AsyncSession) -> None:
    from datetime import datetime
    # Имитируем наивную дату из SQLite
    naive_dt = datetime(2026, 12, 31, 23, 59, 59)
    user = await repo.get_or_create_user(session, tg_id=888002, username="naive_tester", full_name="Naive")
    user.sub_expires_at = naive_dt
    await session.commit()

    data = await views_api.get_users_list_data(session)
    assert data["total"] >= 1
    item = next(u for u in data["items"] if u["tg_id"] == 888002)
    assert item["is_expired"] is False


@pytest.mark.asyncio
async def test_api_user_subscription_revives_devices(session: AsyncSession, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from bot.db.models import Device, Peer, PeerStatus, ServerStatus, User
    from bot.services.crypto import encrypt
    from aiohttp.test_utils import make_mocked_request
    import json

    # Mock SSH for revive
    class FakeSSH:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.services.revive.SSHClient", lambda *a, **k: FakeSSH())

    async def fake_add_peer(*args, **kwargs):
        pass
    monkeypatch.setattr("bot.services.amnezia.add_peer_on_server", fake_add_peer)

    @asynccontextmanager
    async def fake_session_scope():
        yield session
    monkeypatch.setattr("bot.webadmin.views_api.session_scope", fake_session_scope)

    # Создаем пользователя с отозванным устройством
    user = await repo.get_or_create_user(session, tg_id=888003, username="revive_me", full_name="Revive")
    user.sub_max_devices = 2
    user.sub_max_bypass = 2
    user.sub_expires_at = None

    server = await repo.create_server(
        session, name="s_web", host="1.1.1.1", wg_port=585,
        owner_tg_id=111, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    device = await repo.create_device(session, user_id=user.id, label="phone")
    device.status = PeerStatus.REVOKED
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", ip="10.8.0.2", public_key="pp",
        private_key_enc=encrypt("priv"), status=PeerStatus.REVOKED,
    )
    session.add(peer)
    await session.commit()

    # Запрос на продление на 30 дней
    payload = json.dumps({"add_days": 30, "reset_traffic": True, "mark_paid": True}).encode("utf-8")
    req = make_mocked_request(
        "POST",
        f"/admin/api/users/{user.id}/subscription",
        match_info={"id": str(user.id)},
        headers={"Content-Type": "application/json"},
    )
    req["admin_id"] = 111

    # Задаем чтение json payload
    async def fake_json():
        return json.loads(payload.decode("utf-8"))
    req.json = fake_json

    resp = await views_api.api_user_subscription(req)
    assert resp.status == 200
    res_data = json.loads(resp.text)
    assert res_data["ok"] is True
    assert res_data["revived"]["devices"] == 1

    # Проверяем БД
    await session.refresh(device)
    await session.refresh(peer)
    await session.refresh(user)
    assert device.status == PeerStatus.ACTIVE
    assert peer.status == PeerStatus.ACTIVE
    assert user.sub_expires_at is not None


@pytest.mark.asyncio
async def test_api_user_subscription_revokes_devices_when_expired(session: AsyncSession, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from bot.db.models import Device, Peer, PeerStatus, ServerStatus, User
    from bot.services.crypto import encrypt
    from aiohttp.test_utils import make_mocked_request
    import json

    class FakeSSH:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("bot.services.revive.SSHClient", lambda *a, **k: FakeSSH())

    async def fake_remove_peer(*args, **kwargs):
        pass
    monkeypatch.setattr("bot.services.amnezia.remove_peer_on_server", fake_remove_peer)

    @asynccontextmanager
    async def fake_session_scope():
        yield session
    monkeypatch.setattr("bot.webadmin.views_api.session_scope", fake_session_scope)

    user = await repo.get_or_create_user(session, tg_id=888004, username="revoke_me", full_name="Revoke")
    user.sub_max_devices = 2
    user.sub_max_bypass = 2
    user.sub_expires_at = datetime(2026, 12, 31, tzinfo=timezone.utc)

    server = await repo.create_server(
        session, name="s_web2", host="1.1.1.2", wg_port=585,
        owner_tg_id=111, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.2:585",
    )
    device = await repo.create_device(session, user_id=user.id, label="phone")
    device.status = PeerStatus.ACTIVE
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", ip="10.8.0.3", public_key="pp2",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.commit()

    # Запрос на установку даты в прошлом (отключение)
    past_iso = "2020-01-01T00:00:00Z"
    payload = json.dumps({"expires_at": past_iso}).encode("utf-8")
    req = make_mocked_request(
        "POST",
        f"/admin/api/users/{user.id}/subscription",
        match_info={"id": str(user.id)},
        headers={"Content-Type": "application/json"},
    )
    req["admin_id"] = 111

    async def fake_json():
        return json.loads(payload.decode("utf-8"))
    req.json = fake_json

    resp = await views_api.api_user_subscription(req)
    assert resp.status == 200

    await session.refresh(device)
    await session.refresh(peer)
    assert device.status == PeerStatus.REVOKED
    assert peer.status == PeerStatus.REVOKED


@pytest.mark.asyncio
async def test_api_user_device_send_config_and_revoke(session: AsyncSession, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from aiohttp.test_utils import make_mocked_request
    from bot.db.models import Device, Peer, PeerStatus, ServerStatus, User
    from bot.services import amnezia
    from bot.services.crypto import encrypt
    import json

    @asynccontextmanager
    async def fake_session_scope():
        yield session
    monkeypatch.setattr("bot.webadmin.views_api.session_scope", fake_session_scope)

    user = await repo.get_or_create_user(session, tg_id=888005, username="dev_test", full_name="Device Tester")
    server = await repo.create_server(
        session, name="s_dev", host="1.1.1.3", wg_port=585,
        owner_tg_id=111, status=ServerStatus.READY,
        server_public_key="pub3", server_endpoint="1.1.1.3:585",
        awg_params_json=amnezia.generate_amnezia_params().to_json(),
    )
    device = await repo.create_device(session, user_id=user.id, label="laptop")
    device.status = PeerStatus.ACTIVE
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="laptop", ip="10.8.0.4", public_key="pp3",
        private_key_enc=encrypt("priv3"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.commit()

    # 1. Send config
    req_cfg = make_mocked_request(
        "POST",
        f"/admin/api/users/{user.id}/devices/{device.id}/send_config",
        match_info={"id": str(user.id), "device_id": str(device.id)},
    )
    req_cfg["admin_id"] = 111
    async def fake_json():
        return {"send_to_user": False}
    req_cfg.json = fake_json

    resp_cfg = await views_api.api_user_device_send_config(req_cfg)
    assert resp_cfg.status == 200
    data_cfg = json.loads(resp_cfg.text)
    assert data_cfg["ok"] is True
    assert "Interface" in data_cfg["conf"]
    assert "laptop" in data_cfg["filename"]

    # 2. Revoke device
    class FakeSSH:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr("bot.services.teardown.SSHClient", lambda *a, **k: FakeSSH())
    async def fake_remove_peer(*args, **kwargs):
        pass
    monkeypatch.setattr("bot.services.amnezia.remove_peer_on_server", fake_remove_peer)

    req_rev = make_mocked_request(
        "POST",
        f"/admin/api/users/{user.id}/devices/{device.id}/revoke",
        match_info={"id": str(user.id), "device_id": str(device.id)},
    )
    req_rev["admin_id"] = 111
    resp_rev = await views_api.api_user_device_revoke(req_rev)
    assert resp_rev.status == 200
    data_rev = json.loads(resp_rev.text)
    assert data_rev["ok"] is True


@pytest.mark.asyncio
async def test_api_user_bypass_revoke(session: AsyncSession, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from aiohttp.test_utils import make_mocked_request
    from bot.db.models import PeerStatus, ServerStatus, WdttAccess
    import json

    @asynccontextmanager
    async def fake_session_scope():
        yield session
    monkeypatch.setattr("bot.webadmin.views_api.session_scope", fake_session_scope)

    user = await repo.get_or_create_user(session, tg_id=888006, username="bp_test", full_name="Bypass Tester")
    server = await repo.create_server(
        session, name="s_bp", host="1.1.1.4", wg_port=585,
        owner_tg_id=111, status=ServerStatus.READY,
        server_public_key="pub4", server_endpoint="1.1.1.4:585",
        wdtt_enabled=True,
    )
    from bot.services.crypto import encrypt
    access = await repo.create_wdtt_access(
        session,
        user_id=user.id,
        server_id=server.id,
        label="bp_phone",
        platform="android",
        uri_enc=encrypt("uri"),
        password_enc=encrypt("pass"),
        expires_at=None,
    )
    access.status = PeerStatus.ACTIVE
    await session.commit()

    async def fake_remove_bypass(*args, **kwargs):
        return True
    monkeypatch.setattr("bot.services.teardown._remove_bypass_on_server", fake_remove_bypass)

    req = make_mocked_request(
        "POST",
        f"/admin/api/users/{user.id}/bypass/{access.id}/revoke",
        match_info={"id": str(user.id), "access_id": str(access.id)},
    )
    req["admin_id"] = 111
    resp = await views_api.api_user_bypass_revoke(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["ok"] is True
    # Проверяем, что запись удалена
    check = await repo.get_wdtt_access(session, access.id)
    assert check is None




