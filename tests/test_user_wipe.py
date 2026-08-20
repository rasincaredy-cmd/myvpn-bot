"""Тесты стирания юзера (services/user_wipe.py).

Инвариант, который тут защищается: стирание должно стирать. После wipe'а не
остаётся REVOKED-строк, которые могли бы «прилипнуть» к новому юзеру с тем же
id — ровно это происходило с Владом на проде (см. докстринг user_wipe).

НО: строку с ключами можно удалять, только если конфиг реально снят с
сервера. Если SSH не поднялся — строка остаётся, чтобы ретеншн планировщика
повторил снятие по ключам и потом удалил сам.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Device, Peer, PeerStatus, ServerStatus, User, WdttAccess
from bot.services import revive, user_wipe
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


class FakeSSH:
    """Асинхронный контекст-менеджер вместо SSHClient — соединение есть."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "FakeSSH":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class DeadSSH:
    """SSH, который не поднимается: сервер недоступен в момент стирания."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "DeadSSH":
        raise SSHError("host unreachable")

    async def __aexit__(self, *exc) -> None:
        return None


async def _make_user(session: AsyncSession, *, tg_id: int = 555):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 2
    user.sub_max_bypass = 2
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    device = await repo.create_device(session, user_id=user.id, label="phone")
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", ip="10.8.0.2", public_key="pp",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    access = await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, device_id=device.id,
        label="phone", uri_enc=encrypt("wdtt://1.1.1.1:56000:56001:9000:PASS1:hashX"),
        password_enc=encrypt("PASS1"), expires_at=None, platform="android",
    )
    return user, server, device, peer, access


def _patch_ssh(monkeypatch, ssh_cls=FakeSSH) -> None:
    monkeypatch.setattr(revive, "SSHClient", ssh_cls)
    monkeypatch.setattr(revive.repo, "creds_from_server", lambda s: None)

    async def ok_remove_peer(ssh, *, public_key: str) -> None:
        return None

    async def ok_remove_access(ssh, *, password: str, binary: str) -> bool:
        return True

    monkeypatch.setattr(revive.amnezia, "remove_peer_on_server", ok_remove_peer)
    monkeypatch.setattr(revive.wdtt_svc, "remove_access", ok_remove_access)


class TestWipeRemovesUser:
    @pytest.mark.asyncio
    async def test_user_row_is_gone(self, session: AsyncSession, monkeypatch) -> None:
        _patch_ssh(monkeypatch)
        user, *_ = await _make_user(session)
        user_id = user.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        left = (await session.execute(
            select(func.count()).select_from(User).where(User.id == user_id)
        )).scalar_one()
        assert left == 0

    @pytest.mark.asyncio
    async def test_counts_revoked_items(self, session: AsyncSession, monkeypatch) -> None:
        _patch_ssh(monkeypatch)
        user, *_ = await _make_user(session)

        res = await user_wipe.wipe_user(session, user)

        # один активный пир + один активный обход
        assert res.revoked_items == 2


class TestWipeDeletesClearedConfigs:
    """Снятые с сервера конфиги удаляются сразу — стирание должно стирать."""

    @pytest.mark.asyncio
    async def test_cleared_peer_and_wdtt_rows_are_deleted(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        _patch_ssh(monkeypatch)
        user, _server, device, peer, access = await _make_user(session)
        peer_id, access_id, device_id = peer.id, access.id, device.id

        res = await user_wipe.wipe_user(session, user)
        await session.commit()

        assert (await session.get(Peer, peer_id)) is None, "снятый пир остался в БД"
        assert (await session.get(WdttAccess, access_id)) is None, "снятый обход остался в БД"
        assert (await session.get(Device, device_id)) is None, "пустое устройство осталось в БД"
        assert res.deleted_configs == 2, f"ожидали 2 удалённых конфига, получили {res.deleted_configs}"

    @pytest.mark.asyncio
    async def test_device_row_survives_if_peer_was_left(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """SSH не прошёл → пир остался → устройство тоже остаётся (не пустышка).
        Зомби-чистка планировщика уберёт его после ретеншна."""
        _patch_ssh(monkeypatch, DeadSSH)
        user, _server, device, peer, access = await _make_user(session)
        peer_id, access_id, device_id = peer.id, access.id, device.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        left_peer = await session.get(Peer, peer_id)
        left_acc = await session.get(WdttAccess, access_id)
        left_dev = await session.get(Device, device_id)
        assert left_peer is not None and left_peer.status == PeerStatus.REVOKED
        assert left_acc is not None and left_acc.status == PeerStatus.REVOKED
        assert left_peer.revoked_at is not None, "без revoked_at ретеншн строку не найдёт"
        assert left_dev is not None, "устройство с живым содержимым снесено"

    @pytest.mark.asyncio
    async def test_already_revoked_rows_are_deleted(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Строки, отозванные ДО стирания (например, по истечению подписки), тоже
        удаляются: их снятие случилось при том отзыве."""
        _patch_ssh(monkeypatch)
        user, _server, _device, peer, access = await _make_user(session)
        # Имитируем отзыв до стирания: ревайв-путь, которым планировщик гасит
        # устройства по истечению.
        await revive.revoke_devices_for_user(session, user.id)
        await session.commit()
        peer_id, access_id = peer.id, access.id

        await user_wipe.wipe_user(session, user)
        await session.commit()

        assert (await session.get(Peer, peer_id)) is None
        assert (await session.get(WdttAccess, access_id)) is None


class TestPlategaRowsArePurged:
    """Платежи Platega при стирании юзера оставались в базе (аудит 20.08.2026).

    Инвойсы CryptoBot и звёздные платежи чистились, а карточные — нет. Два
    следствия, и второе денежное:

    1. Обещание «сотрите мои данные» не выполнялось: записи об оплатах
       оставались лежать с id стёртого человека.
    2. `users.id` объявлен БЕЗ AUTOINCREMENT, и SQLite переиспользует
       освободившийся максимальный rowid — этим же файл и мотивирует чистку
       журналов. Значит `pending`-платёж стёртого юзера доставался следующему
       зарегистрировавшемуся, и поллинг планировщика зачислял бы ЕМУ чужие
       деньги.
    """

    @pytest.mark.asyncio
    async def test_platega_rows_are_deleted(self, session: AsyncSession) -> None:
        from sqlalchemy import select

        from bot.db.models import PlategaPayment

        user = await repo.get_or_create_user(
            session, tg_id=9101, username="u", full_name="U"
        )
        await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-wipe-1",
            amount_kopeks=50000, url="https://example.com/pay",
        )
        await session.flush()

        await repo.purge_user_records(session, user.id)

        left = (await session.execute(
            select(PlategaPayment).where(PlategaPayment.user_id == user.id)
        )).scalars().all()
        assert not left, "платежи Platega пережили стирание юзера"

    @pytest.mark.asyncio
    async def test_purge_reports_the_count(self, session: AsyncSession) -> None:
        """Счётчик нужен админу в отчёте о стирании: «удалено N записей»."""
        user = await repo.get_or_create_user(
            session, tg_id=9102, username="u", full_name="U"
        )
        await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-wipe-2",
            amount_kopeks=50000, url="https://example.com/pay",
        )
        await session.flush()

        counts = await repo.purge_user_records(session, user.id)
        assert counts.get("platega_payments") == 1
