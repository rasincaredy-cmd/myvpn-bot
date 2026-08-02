"""Тесты журнала действий."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction, AuditLog, Peer, PeerStatus, ServerStatus
from bot.services import billing, revive, teardown
from bot.services.crypto import encrypt


class TestAuditModel:
    async def test_row_is_stored_with_all_fields(self, session: AsyncSession) -> None:
        session.add(AuditLog(
            actor_tg_id=111,
            actor_is_admin=True,
            action=AuditAction.ADMIN_CREDIT,
            target_user_id=7,
            target_type="user",
            target_id=7,
            amount_kopeks=13000,
            details="Начислено вручную",
        ))
        await session.flush()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.id is not None
        assert row.created_at is not None
        assert row.action == AuditAction.ADMIN_CREDIT
        assert row.amount_kopeks == 13000
        assert row.target_user_id == 7

    async def test_system_event_has_no_actor(self, session: AsyncSession) -> None:
        """События планировщика пишутся без человека-инициатора."""
        session.add(AuditLog(action=AuditAction.SERVER_DOWN, target_type="server", target_id=1))
        await session.flush()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.actor_tg_id is None
        assert row.actor_is_admin is False
        assert row.amount_kopeks is None

    async def test_survives_real_roundtrip(self, session: AsyncSession) -> None:
        """Читаем из базы, а не из identity map: после commit+expunge_all
        объект собирается заново из строк таблицы."""
        session.add(AuditLog(
            action=AuditAction.BALANCE_CHARGE,
            target_user_id=5,
            amount_kopeks=13000,
            details="Подписка 3 мес",
        ))
        await session.commit()
        session.expunge_all()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.action == AuditAction.BALANCE_CHARGE
        assert row.amount_kopeks == 13000
        assert row.target_user_id == 5
        assert row.details == "Подписка 3 мес"


class TestAuditRepo:
    async def test_log_action_writes_row(self, session: AsyncSession) -> None:
        await repo.log_action(
            session, AuditAction.CONFIG_ISSUED,
            actor_tg_id=555, target_user_id=3, target_type="peer", target_id=9,
            details="Телефон",
        )
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit(session)
        assert len(rows) == 1
        assert rows[0].action == AuditAction.CONFIG_ISSUED
        assert rows[0].details == "Телефон"
        assert rows[0].actor_tg_id == 555
        assert rows[0].target_type == "peer"
        assert rows[0].target_id == 9

    async def test_list_is_newest_first(self, session: AsyncSession) -> None:
        for i in range(3):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit(session)
        assert [r.details for r in rows] == ["#2", "#1", "#0"]

    async def test_paging(self, session: AsyncSession) -> None:
        for i in range(5):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        await session.commit()
        session.expunge_all()

        page2 = await repo.list_audit(session, limit=2, offset=2)
        assert [r.details for r in page2] == ["#2", "#1"]
        assert await repo.count_audit(session) == 5

    async def test_history_filters_by_user(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=1)
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=2)
        await session.commit()
        session.expunge_all()

        mine = await repo.list_audit_for_user(session, 1)
        assert len(mine) == 1
        assert mine[0].target_user_id == 1
        assert await repo.count_audit_for_user(session, 1) == 1

    async def test_retention_deletes_only_old(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, details="свежая")
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            details="старая",
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
        )
        session.add(old)
        await session.commit()
        session.expunge_all()

        removed = await repo.delete_audit_older_than(session, days=90)
        assert removed == 1
        left = await repo.list_audit(session)
        assert [r.details for r in left] == ["свежая"]

    async def test_retention_after_reading_feed_from_db(self, session: AsyncSession) -> None:
        """Главный краевой случай: created_at проставлен базой (server_default),
        то есть без таймзоны. Сначала читаем ленту — записи попадают в identity
        map с naive created_at, — и только потом чистим. Если чистка сравнивает
        даты в Python, а не в SQL, здесь ловится TypeError."""
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, details="свежая")
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit(session)
        assert len(rows) == 1
        assert rows[0].created_at.tzinfo is None  # база отдала без таймзоны

        removed = await repo.delete_audit_older_than(session, days=90)
        assert removed == 0
        assert len(await repo.list_audit(session)) == 1

    async def test_retention_zero_days_keeps_everything(self, session: AsyncSession) -> None:
        """0 = ретеншн выключен: журнал не чистится вообще."""
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            created_at=datetime.now(timezone.utc) - timedelta(days=999),
        )
        session.add(old)
        await session.commit()
        session.expunge_all()

        assert await repo.delete_audit_older_than(session, days=0) == 0
        assert len(await repo.list_audit(session)) == 1


class TestAuditMoney:
    """Денежные события попадают в журнал сами, из тех же функций, что двигают
    деньги. Везде читаем после commit+expunge_all — иначе проверялась бы память
    сессии, а не строки таблицы."""

    async def test_purchase_is_logged(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1001, username="buyer", full_name="Buyer"
        )
        user.balance_kopeks = 100_000
        user.sub_max_devices = 2
        user.sub_max_bypass = 1
        await session.flush()
        user_id, tg_id = user.id, user.tg_id

        res = await billing.charge_and_extend(session, user, months=1)
        assert res.ok
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        charges = [r for r in rows if r.action == AuditAction.BALANCE_CHARGE]
        assert len(charges) == 1
        assert charges[0].amount_kopeks == res.price_kopeks
        assert charges[0].actor_tg_id == tg_id
        assert charges[0].actor_is_admin is False
        assert charges[0].details == "Подписка 1 мес (устройств: 2, обходов: 1)"

    async def test_failed_purchase_is_not_logged(self, session: AsyncSession) -> None:
        """Не хватило денег — списания не было, значит и события быть не должно."""
        user = await repo.get_or_create_user(
            session, tg_id=1005, username="poor", full_name="Poor"
        )
        user.balance_kopeks = 10_00
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()
        user_id = user.id

        res = await billing.charge_and_extend(session, user, months=1)
        assert not res.ok
        await session.commit()
        session.expunge_all()

        assert await repo.list_audit_for_user(session, user_id) == []

    async def test_admin_grant_is_logged_as_admin_action(
        self, session: AsyncSession
    ) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1002, username="gifted", full_name="Gifted"
        )
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()
        user_id = user.id

        await billing.grant_term(session, user, months=3, actor_tg_id=111)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        grants = [r for r in rows if r.action == AuditAction.SUB_GRANTED]
        assert len(grants) == 1
        assert grants[0].actor_is_admin is True
        assert grants[0].actor_tg_id == 111
        # Подарок — не списание: денежной суммы у события нет.
        assert grants[0].amount_kopeks is None

    async def test_grant_without_actor_is_not_signed_by_admin(
        self, session: AsyncSession
    ) -> None:
        """Выдача без указания админа — это сделал бот (например, будущая
        автоматическая компенсация). В ленте она не должна выглядеть как
        «выдал админ, неизвестно какой»: без актора и признак админа снят."""
        user = await repo.get_or_create_user(
            session, tg_id=1009, username="noactor", full_name="NoActor"
        )
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()
        user_id = user.id

        await billing.grant_term(session, user, months=3)
        await session.commit()
        session.expunge_all()

        grants = [
            r for r in await repo.list_audit_for_user(session, user_id)
            if r.action == AuditAction.SUB_GRANTED
        ]
        assert len(grants) == 1
        assert grants[0].actor_tg_id is None
        assert grants[0].actor_is_admin is False

    async def test_deposit_is_logged(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1003, username="payer", full_name="Payer"
        )
        await session.flush()
        user_id, tg_id = user.id, user.tg_id
        inv = await repo.create_crypto_invoice(
            session, user_id=user_id, invoice_id=9001,
            amount_kopeks=300_00, url="https://t.me/CryptoBot?start=x",
        )

        await billing.apply_paid_invoice(session, inv)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        tops = [r for r in rows if r.action == AuditAction.BALANCE_TOPUP]
        assert len(tops) == 1
        assert tops[0].amount_kopeks == 300_00
        assert tops[0].actor_tg_id == tg_id
        assert tops[0].details == "Пополнение баланса"

    async def test_repeated_deposit_is_logged_once(self, session: AsyncSession) -> None:
        """Кнопка «Проверить» и поллинг наперегонки не задваивают ни деньги,
        ни запись в журнале."""
        user = await repo.get_or_create_user(
            session, tg_id=1006, username="payer2", full_name="Payer2"
        )
        await session.flush()
        user_id = user.id
        inv = await repo.create_crypto_invoice(
            session, user_id=user_id, invoice_id=9003,
            amount_kopeks=100_00, url="https://t.me/CryptoBot?start=x",
        )

        await billing.apply_paid_invoice(session, inv)
        await billing.apply_paid_invoice(session, inv)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        assert len([r for r in rows if r.action == AuditAction.BALANCE_TOPUP]) == 1

    async def test_referral_reward_is_logged_on_referrer(
        self, session: AsyncSession
    ) -> None:
        """Награда записывается пригласившему: спор разбирается по его карточке."""
        referrer = await repo.get_or_create_user(
            session, tg_id=1004, username="ref", full_name="Ref"
        )
        await session.flush()
        user = await repo.get_or_create_user(
            session, tg_id=1105, username="son", full_name="Son"
        )
        user.referrer_id = referrer.id
        await session.flush()
        referrer_id = referrer.id
        inv = await repo.create_crypto_invoice(
            session, user_id=user.id, invoice_id=9002,
            amount_kopeks=200_00, url="https://t.me/CryptoBot?start=x",
        )

        await billing.apply_paid_invoice(session, inv)
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, referrer_id)
        rewards = [r for r in rows if r.action == AuditAction.REFERRAL_REWARD]
        assert len(rewards) == 1
        assert rewards[0].amount_kopeks == 200_00 * settings.referral_percent // 100
        # Награду начислил бот, а не человек — инициатора у события нет.
        assert rewards[0].actor_tg_id is None
        assert rewards[0].details == f"{settings.referral_percent}% с пополнения реферала"

    async def test_autopay_charge_has_no_human_actor(
        self, session: AsyncSession
    ) -> None:
        """Автосписание не должно выглядеть покупкой, которую сделал юзер: иначе
        на жалобу «я ничего не покупал» админ увидит в истории самого юзера."""
        user = await repo.get_or_create_user(
            session, tg_id=1007, username="auto", full_name="Auto"
        )
        user.balance_kopeks = 100_00
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        user.autopay = True
        user.sub_expires_at = datetime.now(timezone.utc) - timedelta(days=2)
        await session.flush()
        user_id = user.id

        res = await billing.autopay_if_expired(session, user)
        assert res is not None and res.ok
        await session.commit()
        session.expunge_all()

        rows = await repo.list_audit_for_user(session, user_id)
        charges = [r for r in rows if r.action == AuditAction.BALANCE_CHARGE]
        assert len(charges) == 1
        assert charges[0].actor_tg_id is None       # списал бот, не человек
        assert charges[0].amount_kopeks == res.price_kopeks
        # Разница видна прямо в строке ленты, без сверки полей.
        assert "автопродление" in charges[0].details.lower()

    async def test_manual_purchase_keeps_human_actor(
        self, session: AsyncSession
    ) -> None:
        """Парная проверка к автопродлению: обычную покупку по-прежнему
        подписывает сам юзер, иначе различать было бы нечего."""
        user = await repo.get_or_create_user(
            session, tg_id=1008, username="manual", full_name="Manual"
        )
        user.balance_kopeks = 100_00
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()
        user_id, tg_id = user.id, user.tg_id

        assert (await billing.charge_and_extend(session, user, months=1)).ok
        await session.commit()
        session.expunge_all()

        charges = [
            r for r in await repo.list_audit_for_user(session, user_id)
            if r.action == AuditAction.BALANCE_CHARGE
        ]
        assert len(charges) == 1
        assert charges[0].actor_tg_id == tg_id
        assert "автопродление" not in charges[0].details.lower()


class TestAuditAdmin:
    async def test_admin_actions_are_marked(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1004, username="target", full_name="Target"
        )
        await session.flush()

        await repo.log_action(
            session, AuditAction.USER_BLOCKED,
            actor_tg_id=111, actor_is_admin=True,
            target_user_id=user.id, target_type="user", target_id=user.id,
            details="Заблокирован админом",
        )

        rows = await repo.list_audit_for_user(session, user.id)
        assert rows[0].actor_is_admin is True
        assert rows[0].actor_tg_id == 111


# --- Отзыв доступа ----------------------------------------------------------
# Событие пишется ВНУТРИ общих функций отзыва, а не врезками рядом с их
# вызовами: этот класс ошибки в ветке ловили четыре раза подряд — каждый раз
# находился ещё один хендлер, зовущий ту же функцию без врезки. Поэтому тесты
# дёргают сами функции (и один раз — хендлер целиком, чтобы поймать задвоение).


class _FakeSSH:
    """Асинхронный контекст-менеджер вместо SSHClient — соединения нет."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeSSH":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def _mute_ssh(monkeypatch) -> None:
    """Глушим всю работу с VPS: тесты про журнал, а не про снятие пиров."""
    async def noop(*args, **kwargs) -> None:
        return None

    for mod in (revive, teardown):
        monkeypatch.setattr(mod, "SSHClient", _FakeSSH)
        monkeypatch.setattr(mod.repo, "creds_from_server", lambda s: None)
        monkeypatch.setattr(mod.amnezia, "remove_peer_on_server", noop)
        monkeypatch.setattr(mod.wdtt_svc, "remove_access", noop)


async def _user_with_device(session: AsyncSession, *, tg_id: int):
    """Юзер с одним устройством, WG-пиром и доступом обхода на нём."""
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
    device = await repo.create_device(session, user_id=user.id, label="Телефон")
    session.add(Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", ip="10.8.0.2", public_key="pp",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    ))
    await session.flush()
    access = await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", uri_enc=encrypt("wdtt://1.1.1.1:1:2:3:PASS1:hashX"),
        password_enc=encrypt("PASS1"), expires_at=None, platform="android",
    )
    return user, server, device, access


async def _revoked_rows(session: AsyncSession, user_id: int) -> list[AuditLog]:
    return [
        r for r in await repo.list_audit_for_user(session, user_id)
        if r.action == AuditAction.CONFIG_REVOKED
    ]


class TestAuditRevokeAll:
    async def test_logs_who_turned_it_off(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Мгновенное отключение подписки админом: в ленте видно, что погасил
        человек, а не планировщик."""
        _mute_ssh(monkeypatch)
        user, _, device, _ = await _user_with_device(session, tg_id=2001)
        user_id = user.id

        assert await revive.revoke_devices_for_user(
            session, user_id, actor_tg_id=111, actor_is_admin=True,
        ) is True
        await session.commit()
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 1
        assert rows[0].actor_tg_id == 111
        assert rows[0].actor_is_admin is True

    async def test_scheduler_reason_survives(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Планировщик отдаёт готовый текст причины — он и попадает в ленту
        дословно, вместе с числом погашенных устройств."""
        _mute_ssh(monkeypatch)
        user, _, _, _ = await _user_with_device(session, tg_id=2002)
        user_id = user.id

        await revive.revoke_devices_for_user(
            session, user_id, reason="Отозван по истечению подписки (устройств: 1)",
        )
        await session.commit()
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 1
        assert rows[0].details == "Отозван по истечению подписки (устройств: 1)"
        assert rows[0].actor_tg_id is None      # погасил бот, не человек

    async def test_row_per_device(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Строка на каждое погашенное устройство, а не одна на весь отзыв:
        админ в карточке юзера должен видеть, какое именно устройство встало,
        — сводка «отозвано 2» на этот вопрос не отвечает."""
        _mute_ssh(monkeypatch)
        user, _, device, _ = await _user_with_device(session, tg_id=2008)
        user_id, first_id = user.id, device.id
        second = await repo.create_device(session, user_id=user_id, label="Ноут")
        await session.flush()
        second_id = second.id

        await revive.revoke_devices_for_user(session, user_id, actor_tg_id=111)
        await session.commit()
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 2
        assert {r.target_id for r in rows} == {first_id, second_id}
        assert {r.target_type for r in rows} == {"device"}

    async def test_nothing_to_revoke_writes_nothing(
        self, session: AsyncSession
    ) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=2003, username="e", full_name="E"
        )
        await session.flush()
        user_id = user.id

        assert await revive.revoke_devices_for_user(session, user_id) is False
        await session.commit()
        session.expunge_all()

        assert await _revoked_rows(session, user_id) == []


class TestAuditTeardown:
    async def test_delete_device_logs_for_any_caller(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Админское удаление устройства из карточки юзера врезки не имеет —
        событие обязано появиться из самой функции."""
        _mute_ssh(monkeypatch)
        user, _, device, _ = await _user_with_device(session, tg_id=2004)
        user_id, device_id = user.id, device.id

        await teardown.delete_device(session, device)
        await session.commit()
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 1
        assert rows[0].target_type == "device"
        assert rows[0].target_id == device_id
        assert "Телефон" in rows[0].details

    async def test_revoke_bypass_logs_wdtt(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        _mute_ssh(monkeypatch)
        user, _, _, access = await _user_with_device(session, tg_id=2005)
        user_id, access_id = user.id, access.id

        await teardown.revoke_bypass(session, access)
        await session.commit()
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 1
        assert rows[0].target_type == "wdtt"
        assert rows[0].target_id == access_id


class TestAuditRevokePeer:
    async def test_revoke_peer_logs_peer_target(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Отзыв одиночного пира из карточки сервера: общей сервисной функции у
        этого пути нет, поэтому запись живёт в самом примитиве отзыва."""
        _mute_ssh(monkeypatch)
        user, _, _, _ = await _user_with_device(session, tg_id=2007)
        user_id = user.id
        peer = (await repo.list_peers_for_user(session, user_id))[0]
        peer_id = peer.id

        await repo.revoke_peer(
            session, peer_id, actor_tg_id=111, actor_is_admin=True,
            details="Пир «Телефон» отозван админом",
        )
        await session.commit()
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 1
        assert rows[0].target_type == "peer"
        assert rows[0].target_id == peer_id
        assert rows[0].actor_is_admin is True


class TestAuditNoDoubleRow:
    async def test_user_device_delete_is_one_row(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Юзерское удаление устройства проходит через ту же функцию. Если
        врезку у вызывающего не снять, на одно действие в ленте будет две
        строки — а админ решит, что юзер удалил устройство дважды."""
        from bot.handlers import devices as devices_h

        _mute_ssh(monkeypatch)
        user, _, device, _ = await _user_with_device(session, tg_id=2006)
        user_id, device_id, tg_id = user.id, device.id, user.tg_id
        call = _FakeCall(f"dev:revoke:{device_id}", tg_id)

        await devices_h.cb_dev_revoke(call, session)
        session.expunge_all()

        rows = await _revoked_rows(session, user_id)
        assert len(rows) == 1, "врезка у вызывающего задваивает событие"
        assert rows[0].actor_tg_id == tg_id
        assert rows[0].details == "Устройство «Телефон» удалено юзером"


class _FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)


class _FakeFrom:
    def __init__(self, uid: int) -> None:
        self.id = uid


class _FakeCall:
    """Минимальный CallbackQuery — хендлеру нужны только эти четыре вещи."""

    def __init__(self, data: str, uid: int) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)
