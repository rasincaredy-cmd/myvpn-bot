"""Доставка конфига юзеру: сборка текста, права, выбор формата."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services.amnezia import AmneziaParams
from bot.services.crypto import encrypt


async def _user_with_peer(session: AsyncSession, *, tg_id: int):
    """Юзер с активной подпиской, устройством и одним пиром на READY-сервере."""
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 2
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="SRVPUB", server_endpoint="1.1.1.1:585",
        # Параметры обфускации заполняет установщик; у READY-сервера они есть
        # всегда, и без них конфиг собрать нельзя — это не WireGuard.
        awg_params_json=AmneziaParams(
            Jc=5, Jmin=50, Jmax=1000, S1=50, S2=80, H1=10, H2=20, H3=30, H4=40
        ).to_json(),
    )
    server.location = "🇳🇱 Нидерланды"
    device = await repo.create_device(session, user_id=user.id, label="Телефон")
    await session.flush()
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", ip="10.8.0.2", public_key="PP",
        private_key_enc=encrypt("PRIVKEY"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    return user, server, device, peer


class TestBuildConf:
    async def test_conf_has_keys_and_endpoint(self, session: AsyncSession) -> None:
        """Собранный конфиг обязан содержать приватный ключ пира, его адрес и
        endpoint сервера — без любого из трёх он не подключится."""
        from bot.handlers.config_delivery import build_conf_for_peer

        _, server, _, peer = await _user_with_peer(session, tg_id=3001)

        got = await build_conf_for_peer(session, peer)

        assert got is not None
        srv, conf = got
        assert srv.id == server.id
        assert "PRIVKEY" in conf
        assert "10.8.0.2" in conf
        assert "SRVPUB" in conf
        assert "1.1.1.1:585" in conf

    async def test_missing_server_gives_none(self, session: AsyncSession) -> None:
        """Сервер удалили, а строка пира осталась: собирать нечего, и молча
        подсунуть пустой конфиг хуже, чем честно сказать «нет»."""
        from bot.handlers.config_delivery import build_conf_for_peer

        _, _, _, peer = await _user_with_peer(session, tg_id=3002)
        peer.server_id = 999999
        await session.flush()

        assert await build_conf_for_peer(session, peer) is None


class _FakeBot:
    """Ловит отправленное вместо реального Telegram."""

    def __init__(self) -> None:
        self.documents: list[tuple[int, bytes, str]] = []
        self.photos: list[tuple[int, bytes]] = []
        self.messages: list[tuple[int, str]] = []

    async def send_document(self, chat_id, document, caption=None, **kw) -> None:
        self.documents.append((chat_id, document.data, document.filename))

    async def send_photo(self, chat_id, photo, caption=None, **kw) -> None:
        self.photos.append((chat_id, photo.data))

    async def send_message(self, chat_id, text, **kw) -> None:
        self.messages.append((chat_id, text))


class _FakeFrom:
    def __init__(self, uid: int) -> None:
        self.id = uid


class _FakeMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = type("C", (), {"id": chat_id})()
        self.texts: list[str] = []

    async def answer(self, text: str, **kw) -> None:
        self.texts.append(text)


class _FakeCall:
    def __init__(self, data: str, uid: int, chat_id: int = 555) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage(chat_id)
        self.answers: list[str] = []
        self.alerts: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        (self.alerts if show_alert else self.answers).append(text)


class TestConfigFormatAuth:
    async def test_stranger_cannot_pull_someones_config(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Подстановка чужого peer_id в кнопку не должна отдавать чужой конфиг."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        _, _, _, peer = await _user_with_peer(session, tg_id=3010)
        stranger = await repo.get_or_create_user(
            session, tg_id=3011, username="bad", full_name="Bad"
        )
        await session.flush()

        call = _FakeCall(f"cfg:file:{peer.id}", stranger.tg_id)
        await cd.cb_config_format(call, session)

        assert fake.documents == []
        assert call.alerts, "юзеру должно прийти явное «не найдено»"

    async def test_owner_gets_the_file(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3012)

        call = _FakeCall(f"cfg:file:{peer.id}", user.tg_id)
        await cd.cb_config_format(call, session)

        assert len(fake.documents) == 1
        _chat, data, filename = fake.documents[0]
        assert b"PRIVKEY" in data
        assert filename.endswith(".conf")

    async def test_admin_may_pull_any_config(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Админу конфиг юзера нужен для разбора жалоб — ему можно."""
        from bot.config import settings
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        _, _, _, peer = await _user_with_peer(session, tg_id=3013)
        admin_id = 999001
        monkeypatch.setattr(settings, "admin_ids", [admin_id])

        call = _FakeCall(f"cfg:file:{peer.id}", admin_id)
        await cd.cb_config_format(call, session)

        assert len(fake.documents) == 1


class TestConfigFormats:
    async def test_only_the_chosen_thing_is_sent(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Смысл всей задачи: одно нажатие — одно сообщение, а не три."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3014)

        await cd.cb_config_format(_FakeCall(f"cfg:link:{peer.id}", user.tg_id), session)

        assert fake.documents == []
        assert fake.photos == []
        assert len(fake.messages) == 1
        assert "vpn://" in fake.messages[0][1]

    async def test_revoked_peer_is_refused(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Отозванный конфиг на сервере уже не работает — отдавать его значит
        отправить юзера настраивать заведомо мёртвое подключение."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3015)
        peer.status = PeerStatus.REVOKED
        await session.flush()

        call = _FakeCall(f"cfg:file:{peer.id}", user.tg_id)
        await cd.cb_config_format(call, session)

        assert fake.documents == []
        assert call.alerts
