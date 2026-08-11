"""Адрес в ссылке резервного подключения берётся из карточки сервера.

Ссылка обхода собирается НЕ ботом, а демоном на сервере: тот спрашивает свой
внешний адрес у api.ipify.org и запоминает ответ до перезапуска. Поэтому после
смены IP у хостера демон ещё сутками отдаёт мёртвый адрес, а выданная раньше
ссылка лежит в базе замороженной строкой и не чинится вообще никогда.

Конфиг VPN этой болезнью не болеет — он каждый раз собирается заново из
`server.host`. Здесь приводим обход к тому же правилу: адрес в ссылке всегда
подставляется из карточки сервера — и при выдаче, и при показе.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus, ServerStatus
from bot.handlers import wdtt as h
from bot.services.crypto import encrypt
from bot.services.wdtt import link_with_host

from tests.test_bypass_standalone import (  # переиспользуем заготовки
    _FakeCall,
    _FakeSSH,
    _fsm,
)

OLD_IP = "84.21.173.132"   # адрес, зарезанный РКН
NEW_IP = "31.77.148.187"   # адрес, выданный взамен


# --------------------------- подстановка адреса ----------------------------

class TestLinkWithHost:
    def test_address_is_replaced_and_everything_else_survives(self) -> None:
        got = link_with_host(f"wdtt://{OLD_IP}:56000:56001:9000:SECRET:hashA", NEW_IP)
        assert got == f"wdtt://{NEW_IP}:56000:56001:9000:SECRET:hashA"

    def test_label_for_pc_is_kept(self) -> None:
        got = link_with_host(f"wdtt://{OLD_IP}:1:2:3:SECRET:hashA#Ноут", NEW_IP)
        assert got == f"wdtt://{NEW_IP}:1:2:3:SECRET:hashA#Ноут"

    def test_several_vk_hashes_are_kept(self) -> None:
        # Хеши идут через запятую в последнем поле — дробить их нельзя.
        got = link_with_host(f"wdtt://{OLD_IP}:1:2:3:SECRET:hA,hB,hC", NEW_IP)
        assert got.endswith(":SECRET:hA,hB,hC")

    def test_link_of_unknown_shape_is_returned_untouched(self) -> None:
        # Чужой формат лучше отдать как есть, чем собрать из него мусор.
        for weird in ("wdtt://x", "", "не ссылка вовсе", f"wdtt://{OLD_IP}:1:2"):
            assert link_with_host(weird, NEW_IP) == weird

    def test_empty_host_changes_nothing(self) -> None:
        uri = f"wdtt://{OLD_IP}:1:2:3:SECRET:hA"
        assert link_with_host(uri, "") == uri


# ----------------------------- показ ссылки --------------------------------

async def _user_with_access(session: AsyncSession, *, tg_id: int, stored_uri: str):
    """Юзер с активным доступом, в котором зашит СТАРЫЙ адрес сервера."""
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_bypass = 3
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    server = await repo.create_server(
        session, name="de", host=NEW_IP, wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint=f"{NEW_IP}:585",
    )
    server.wdtt_enabled = True
    server.wdtt_ports = "56000,56001,9000"
    await session.flush()
    access = await repo.create_wdtt_access(
        session,
        server_id=server.id,
        user_id=user.id,
        device_id=None,
        label="Телефон",
        uri_enc=encrypt(stored_uri),
        password_enc=encrypt("SECRET"),
        expires_at=None,
        platform="android",
        vk_own=False,
    )
    await session.flush()
    return user, server, access


class TestUserGetsCurrentAddress:
    async def test_stale_address_is_healed_on_the_way_out(
        self, session: AsyncSession
    ) -> None:
        user, server, access = await _user_with_access(
            session, tg_id=901, stored_uri=f"wdtt://{OLD_IP}:56000:56001:9000:SECRET:hA"
        )
        call = _FakeCall(f"wd:mylink:{access.id}", user.tg_id)
        await h.cb_wdtt_my_link(call, session)
        sent = "\n".join(call.message.texts)
        assert NEW_IP in sent
        assert OLD_IP not in sent

    async def test_password_and_hash_are_not_lost(
        self, session: AsyncSession
    ) -> None:
        user, server, access = await _user_with_access(
            session, tg_id=902, stored_uri=f"wdtt://{OLD_IP}:56000:56001:9000:SECRET:hA"
        )
        call = _FakeCall(f"wd:mylink:{access.id}", user.tg_id)
        await h.cb_wdtt_my_link(call, session)
        assert f"wdtt://{NEW_IP}:56000:56001:9000:SECRET:hA" in "\n".join(
            call.message.texts
        )

    async def test_admin_sees_the_same_link_as_the_user(
        self, session: AsyncSession
    ) -> None:
        # Поддержка разбирает жалобу по ссылке: она обязана совпадать с той,
        # что юзер только что получил, иначе разбор уводит в сторону.
        from bot.handlers.admin import user_items as ui

        user, server, access = await _user_with_access(
            session, tg_id=903, stored_uri=f"wdtt://{OLD_IP}:56000:56001:9000:SECRET:hA"
        )
        call = _FakeCall(f"pnl:ubpl:{access.id}", user.tg_id)
        await ui.cb_panel_user_bypass_link(call, session)
        sent = "\n".join(call.message.texts)
        assert NEW_IP in sent
        assert OLD_IP not in sent


# ---------------------------- выдача новой ---------------------------------

class TestIssuedLinkIgnoresDaemonGuess:
    async def test_daemon_stale_guess_does_not_reach_the_base(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Демон отдаёт протухший адрес — в базу должен лечь адрес из карточки."""
        user, server, access = await _user_with_access(
            session, tg_id=904, stored_uri=f"wdtt://{NEW_IP}:1:2:3:OLD:hA"
        )

        async def fake_create(ssh, *, days, label, vk_hashes, ports, binary):
            return {
                "password": "PASS2",
                "link": f"wdtt://{OLD_IP}:56000:56001:9000:PASS2:hx",
            }

        monkeypatch.setattr(h, "SSHClient", lambda creds: _FakeSSH())
        monkeypatch.setattr(h.repo, "creds_from_server", lambda s: None)
        monkeypatch.setattr(h.wdtt_svc, "create_access", fake_create)
        monkeypatch.setattr(h.settings, "wdtt_vk_hashes", "hash1")

        state = await _fsm()
        await state.update_data(server_id=server.id)
        call = _FakeCall("wdtt:plat:android", user.tg_id)
        await h.cb_wdtt_platform(call, state, session)

        fresh = [
            a for a in await repo.list_wdtt_for_user(session, user.id)
            if a.status == PeerStatus.ACTIVE and a.id != access.id
        ]
        assert fresh, "доступ не создался"
        from bot.services.crypto import decrypt

        assert decrypt(fresh[0].uri_enc).startswith(f"wdtt://{NEW_IP}:")
