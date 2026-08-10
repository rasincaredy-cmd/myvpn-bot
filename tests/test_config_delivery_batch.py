"""Все конфиги устройства — одним сообщением, а не очередью.

Повод — Влад, 10.08.2026: «в устройстве теперь есть два конфига, а кнопка
получить все последовательно присылает их». Бот спрашивал формат отдельно
на каждую локацию, и юзер жал одно и то же N раз.

Одной ссылкой все локации отдать нельзя: формат `vpn://` описывает ровно
один сервер. Поэтому объединяем доставку — вопрос один, ответ приходит
пачкой вложений.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services.amnezia import AmneziaParams
from bot.services.crypto import encrypt


async def _server(session: AsyncSession, *, tg_id: int, name: str, host: str, location: str):
    server = await repo.create_server(
        session, name=name, host=host, wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key=f"PUB{name}", server_endpoint=f"{host}:585",
        awg_params_json=AmneziaParams(
            Jc=5, Jmin=50, Jmax=1000, S1=50, S2=80, H1=10, H2=20, H3=30, H4=40
        ).to_json(),
    )
    server.location = location
    return server


async def _device_on_many(session: AsyncSession, *, tg_id: int, count: int = 2):
    """Юзер с одним устройством и конфигами на `count` разных серверах."""
    user = await repo.get_or_create_user(session, tg_id=tg_id, username="u", full_name="U")
    user.sub_max_devices = 2
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    device = await repo.create_device(session, user_id=user.id, label="Телефон")
    await session.flush()
    peers = []
    for i in range(count):
        srv = await _server(
            session, tg_id=tg_id, name=f"s{i}", host=f"10.0.0.{i + 1}",
            location=f"🇩🇪 Локация {i}",
        )
        await session.flush()
        peer = Peer(
            server_id=srv.id, user_id=user.id, device_id=device.id,
            label="Телефон", ip=f"10.8.0.{i + 2}", public_key=f"PP{i}",
            private_key_enc=encrypt(f"PRIV{i}"), status=PeerStatus.ACTIVE,
        )
        session.add(peer)
        peers.append(peer)
    await session.flush()
    return user, device, peers


class _FakeBot:
    def __init__(self) -> None:
        self.documents: list[tuple[int, bytes, str]] = []
        self.photos: list[tuple[int, bytes]] = []
        self.messages: list[tuple[int, str]] = []
        self.media_groups: list[tuple[int, list]] = []

    async def send_document(self, chat_id, document, caption=None, **kw) -> None:
        self.documents.append((chat_id, document.data, document.filename))

    async def send_photo(self, chat_id, photo, caption=None, **kw) -> None:
        self.photos.append((chat_id, photo.data))

    async def send_message(self, chat_id, text, **kw) -> None:
        self.messages.append((chat_id, text))

    async def send_media_group(self, chat_id, media, **kw) -> None:
        self.media_groups.append((chat_id, list(media)))


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


class TestOneQuestionPerDevice:
    async def test_send_all_asks_once(self, session: AsyncSession, monkeypatch) -> None:
        """Две локации — один вопрос про формат, а не два."""
        from bot.handlers import config_delivery as cd
        from bot.handlers import devices as dev

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        monkeypatch.setattr(dev, "bot", fake, raising=False)
        user, device, _ = await _device_on_many(session, tg_id=3101, count=2)

        call = _FakeCall(f"dev:send:{device.id}", user.tg_id)
        await dev.cb_dev_send(call, session)

        assert len(fake.messages) == 1, (
            f"вопрос про формат задан {len(fake.messages)} раз вместо одного"
        )

    async def test_question_mentions_all_locations(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers import config_delivery as cd
        from bot.handlers import devices as dev

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, _ = await _device_on_many(session, tg_id=3102, count=3)

        call = _FakeCall(f"dev:send:{device.id}", user.tg_id)
        await dev.cb_dev_send(call, session)

        text = fake.messages[0][1]
        assert "3" in text, "юзер должен видеть, сколько конфигов ему придёт"


class TestBatchDelivery:
    async def test_files_go_in_one_group(self, session: AsyncSession, monkeypatch) -> None:
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, _ = await _device_on_many(session, tg_id=3110, count=2)

        call = _FakeCall(f"cfg:file:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        assert len(fake.media_groups) == 1, "файлы ушли не одной пачкой"
        assert len(fake.media_groups[0][1]) == 2, "в пачке не все конфиги"

    async def test_each_file_labeled_with_location(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Два одинаковых файла без подписи юзер не различит."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, _ = await _device_on_many(session, tg_id=3111, count=2)

        call = _FakeCall(f"cfg:file:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        captions = [getattr(m, "caption", "") or "" for m in fake.media_groups[0][1]]
        assert all(c.strip() for c in captions), "у файлов в пачке нет подписей"
        assert len(set(captions)) == 2, "подписи одинаковые — локации не различить"

    async def test_links_come_as_single_message(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Ссылки в пачку вложений не положить — они уходят одним списком."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, _ = await _device_on_many(session, tg_id=3112, count=2)

        call = _FakeCall(f"cfg:link:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        assert len(fake.messages) == 1, "ссылки пришли не одним сообщением"
        assert fake.messages[0][1].count("vpn://") == 2, "в списке не все ссылки"

    async def test_more_than_ten_split_into_groups(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Telegram не принимает больше 10 вложений в одной пачке."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, _ = await _device_on_many(session, tg_id=3113, count=12)

        call = _FakeCall(f"cfg:file:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        assert [len(g[1]) for g in fake.media_groups] == [10, 2]

    async def test_single_config_goes_without_group(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Пачка из одного вложения Telegram не принимает — шлём как раньше."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, _ = await _device_on_many(session, tg_id=3114, count=1)

        call = _FakeCall(f"cfg:file:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        assert not fake.media_groups, "одиночный конфиг ушёл пачкой"
        assert len(fake.documents) == 1


class TestBatchAuth:
    async def test_stranger_cannot_pull_device(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Номер устройства в кнопке подделывается так же легко, как номер пира."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        _, device, _ = await _device_on_many(session, tg_id=3120, count=2)
        stranger = await repo.get_or_create_user(
            session, tg_id=3121, username="bad", full_name="Bad"
        )
        await session.flush()

        call = _FakeCall(f"cfg:file:dev:{device.id}", stranger.tg_id)
        await cd.cb_config_format_device(call, session)

        assert not fake.media_groups and not fake.documents, "отдан чужой конфиг"
        assert call.alerts, "чужаку не показали отказ"

    async def test_revoked_peers_not_in_batch(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Отозванный конфиг в пачке — это настроенный в приложении мертвец."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, peers = await _device_on_many(session, tg_id=3122, count=3)
        peers[0].status = PeerStatus.REVOKED
        await session.flush()

        call = _FakeCall(f"cfg:file:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        assert len(fake.media_groups[0][1]) == 2, "отозванный конфиг попал в пачку"

    async def test_grace_peers_not_in_batch(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Доживающий после переезда конфиг гаснет через сутки — в приложении
        уже нужен новый, и слать старый вместе с новым нельзя."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, device, peers = await _device_on_many(session, tg_id=3123, count=3)
        peers[0].grace_until = datetime.now(timezone.utc) + timedelta(hours=20)
        await session.flush()

        call = _FakeCall(f"cfg:file:dev:{device.id}", user.tg_id)
        await cd.cb_config_format_device(call, session)

        assert len(fake.media_groups[0][1]) == 2, "доживающий конфиг попал в пачку"


class TestRoutingOrder:
    def test_device_handler_registered_before_peer_handler(self) -> None:
        """Оба обработчика ловят префикс `cfg:`. Если общий зарегистрирован
        первым, он попытается разобрать `cfg:file:dev:10` как номер пира
        и упадёт с ошибкой вместо выдачи."""
        import inspect

        from bot.handlers import config_delivery as cd

        src = inspect.getsource(cd)
        assert src.index("async def cb_config_format_device") < src.index(
            "async def cb_config_format("
        ), "обработчик пачки должен стоять выше одиночного"
