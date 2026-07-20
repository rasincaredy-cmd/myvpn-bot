from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import (
    BalanceTx,
    CryptoInvoice,
    Device,
    Invite,
    Peer,
    PeerStatus,
    Server,
    ServerStatus,
    SupportMsg,
    User,
    WdttAccess,
)
from bot.services.crypto import decrypt
from bot.services.ssh import SSHCredentials


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def user_sub_tier(user: "User") -> str:
    """Уровень юзера для сегментации: 'paid' | 'trial' | 'none'.
    Бессрочная (NULL) подписка считается платной/активной, триал — только с конечным сроком."""
    exp = user.sub_expires_at
    active = exp is None or _as_utc(exp) > datetime.now(timezone.utc)
    if not active:
        return "none"
    if user.is_trial and exp is not None:
        return "trial"
    return "paid"


async def server_labels_map(session: AsyncSession) -> dict[int, str]:
    """id сервера → человекочитаемое имя «Локация N» (номер = порядок сервера
    в своей локации по id). Если локация не задана — имя сервера."""
    servers = list((await session.execute(select(Server).order_by(Server.id))).scalars())
    idx: dict[str, int] = {}
    result: dict[int, str] = {}
    for s in servers:
        if s.location:
            idx[s.location] = idx.get(s.location, 0) + 1
            result[s.id] = f"{s.location} {idx[s.location]}"
        else:
            result[s.id] = s.name
    return result


def group_by_location(servers: list[Server]) -> dict[str, list[Server]]:
    """Группирует сервера по локации (Блок «Распределение»). Сервер без локации —
    сам себе группа (ключ `#id`), чтобы не слипались в одну псевдо-локацию."""
    groups: dict[str, list[Server]] = {}
    for s in servers:
        groups.setdefault(s.location or f"#{s.id}", []).append(s)
    return groups


async def count_active_peers_by_server(session: AsyncSession) -> dict[int, int]:
    """id сервера → число АКТИВНЫХ пиров. Метрика загрузки для распределения
    новых устройств внутри локации."""
    rows = await session.execute(
        select(Peer.server_id, func.count())
        .where(Peer.status == PeerStatus.ACTIVE)
        .group_by(Peer.server_id)
    )
    return {sid: n for sid, n in rows.all()}


async def count_active_wdtt_by_server(session: AsyncSession) -> dict[int, int]:
    """id сервера → число АКТИВНЫХ wdtt-доступов. Для ёмкости обхода
    (Server.wdtt_max_accesses) и распределения внутри локации."""
    rows = await session.execute(
        select(WdttAccess.server_id, func.count())
        .where(WdttAccess.status == PeerStatus.ACTIVE)
        .group_by(WdttAccess.server_id)
    )
    return {sid: n for sid, n in rows.all()}


async def list_known_locations(session: AsyncSession) -> list[str]:
    """Уникальные локации всех серверов — для выбора кнопками (защита от опечаток:
    «🇩🇪 Германия» и «🇩🇪  Германия» стали бы двумя разными локациями)."""
    rows = await session.execute(
        select(Server.location)
        .where(Server.location.is_not(None))
        .distinct()
        .order_by(Server.location)
    )
    return [loc for (loc,) in rows.all()]


def creds_from_server(server: Server) -> SSHCredentials:
    """Распаковывает зашифрованные SSH-креды из БД в SSHCredentials."""
    return SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        password=decrypt(server.ssh_password_enc),
        private_key=decrypt(server.ssh_key_enc),
        key_passphrase=decrypt(server.ssh_key_passphrase_enc),
    )


# --- Users -----------------------------------------------------------------

async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    full_name: str | None,
) -> User:
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if user is None:
        # Авто-триал новым юзерам (Блок 9): лимит устройств + срок из конфига.
        user = User(
            tg_id=tg_id,
            username=username,
            full_name=full_name,
            is_admin=tg_id in settings.admin_ids,
            sub_max_devices=settings.trial_devices,
            sub_expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.trial_days),
            sub_traffic_limit_bytes=(
                settings.trial_traffic_gb * 1024**3
                if settings.trial_traffic_gb else None
            ),
        )
        session.add(user)
        await session.flush()
    else:
        # Поддерживаем username/full_name в актуальном состоянии.
        changed = False
        if user.username != username:
            user.username = username
            changed = True
        if user.full_name != full_name:
            user.full_name = full_name
            changed = True
        # Админы могут добавляться/убираться через .env, синхронизируем флаг.
        is_admin = tg_id in settings.admin_ids
        if user.is_admin != is_admin:
            user.is_admin = is_admin
            changed = True
        if changed:
            await session.flush()
    return user


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.tg_id == tg_id))
    ).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Получить юзера по внутреннему id (PK), а не tg_id.
    Нужно для admin-панели: пир может принадлежать чужому юзеру (инвайт).
    """
    return await session.get(User, user_id)


async def count_users(session: AsyncSession) -> int:
    from sqlalchemy import func
    return (await session.execute(select(func.count(User.id)))).scalar() or 0


async def list_all_users(
    session: AsyncSession, offset: int = 0, limit: int = 10
) -> list[User]:
    # Сортировка сегментами: активная платная → активный триал → без подписки,
    # заблокированные — в самый низ. Внутри сегмента — по id.
    now = datetime.now(timezone.utc)
    active = (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now)
    paid = (User.is_trial.is_(False)) | (User.sub_expires_at.is_(None))
    tier = case(
        (active & paid, 0),
        (active, 1),
        else_=2,
    )
    result = await session.execute(
        select(User)
        .order_by(User.is_blocked.asc(), tier, User.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars())


async def list_all_users_for_broadcast(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).where(User.is_blocked.is_(False)).order_by(User.id)
    )
    return list(result.scalars())


async def list_users_by_ids(session: AsyncSession, ids: list[int]) -> list[User]:
    """Юзеры по списку внутренних id (для ручной рассылки), кроме заблокированных."""
    if not ids:
        return []
    return list((await session.execute(
        select(User).where(User.id.in_(ids)).where(User.is_blocked.is_(False)).order_by(User.id)
    )).scalars())


async def list_broadcast_targets(session: AsyncSession, target: str) -> list[User]:
    """Аудитория рассылки: all | active (активная подписка) | inactive (истёкшая/нет).
    Заблокированных не берём никогда."""
    now = datetime.now(timezone.utc)
    stmt = select(User).where(User.is_blocked.is_(False))
    if target == "active":
        stmt = stmt.where(
            (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now)
        )
    elif target == "inactive":
        stmt = stmt.where(User.sub_expires_at.isnot(None)).where(
            User.sub_expires_at <= now
        )
    return list((await session.execute(stmt.order_by(User.id))).scalars())


async def set_user_blocked(
    session: AsyncSession, user_id: int, blocked: bool
) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(is_blocked=blocked)
    )
    

# --- Servers ------------------------------------------------------------------

async def create_server(session: AsyncSession, **fields: object) -> Server:
    server = Server(**fields)
    session.add(server)
    await session.flush()
    return server


async def get_server(session: AsyncSession, server_id: int) -> Server | None:
    return await session.get(Server, server_id)


async def list_servers_for_owner(session: AsyncSession, owner_tg_id: int) -> list[Server]:
    result = await session.execute(
        select(Server).where(Server.owner_tg_id == owner_tg_id).order_by(Server.id)
    )
    return list(result.scalars())


async def list_all_servers(session: AsyncSession) -> list[Server]:
    """Все серверы сервиса (Блок 8: общий пул, не «личные»). Любой админ управляет
    всеми — owner_tg_id остаётся лишь пометкой «кем установлен»."""
    result = await session.execute(select(Server).order_by(Server.id))
    return list(result.scalars())


async def list_ready_servers(session: AsyncSession) -> list[Server]:
    result = await session.execute(
        select(Server).where(Server.status == ServerStatus.READY).order_by(Server.id)
    )
    return list(result.scalars())


async def set_server_status(
    session: AsyncSession,
    server_id: int,
    status: ServerStatus,
    last_error: str | None = None,
) -> None:
    await session.execute(
        update(Server)
        .where(Server.id == server_id)
        .values(status=status, last_error=last_error)
    )


# --- Peers --------------------------------------------------------------------

async def list_peers_for_user(session: AsyncSession, user_id: int) -> list[Peer]:
    result = await session.execute(
        select(Peer).where(Peer.user_id == user_id).order_by(Peer.id)
    )
    return list(result.scalars())


async def list_peers_for_server(session: AsyncSession, server_id: int) -> list[Peer]:
    result = await session.execute(
        select(Peer).where(Peer.server_id == server_id).order_by(Peer.id)
    )
    return list(result.scalars())


async def get_peer(session: AsyncSession, peer_id: int) -> Peer | None:
    return await session.get(Peer, peer_id)


async def revoke_peer(session: AsyncSession, peer_id: int) -> None:
    await session.execute(
        update(Peer)
        .where(Peer.id == peer_id)
        .values(status=PeerStatus.REVOKED, revoked_at=datetime.now(timezone.utc))
    )


async def revive_peer(session: AsyncSession, peer_id: int) -> None:
    # Пир заново добавляется на сервер → счётчик awg стартует с нуля; сбрасываем
    # накопленный трафик, чтобы прежний лимит не отозвал пира сразу же.
    await session.execute(
        update(Peer)
        .where(Peer.id == peer_id)
        .values(
            status=PeerStatus.ACTIVE,
            revoked_at=None,
            traffic_used_bytes=0,
            traffic_last_raw_bytes=0,
            expiry_warn_flags=0,
        )
    )


async def delete_peer(session: AsyncSession, peer_id: int) -> None:
    peer = await session.get(Peer, peer_id)
    if peer is not None:
        await session.delete(peer)
        await session.flush()

# --- Invites ------------------------------------------------------------------

async def get_invite(session: AsyncSession, token: str) -> Invite | None:
    return (
        await session.execute(select(Invite).where(Invite.token == token))
    ).scalar_one_or_none()


async def mark_invite_used(session: AsyncSession, invite: Invite, tg_id: int) -> None:
    invite.used_by_tg_id = tg_id
    invite.used_at = datetime.now(timezone.utc)
    await session.flush()


async def list_invites_for_server(
    session: AsyncSession, server_id: int
) -> list[Invite]:
    result = await session.execute(
        select(Invite)
        .where(Invite.server_id == server_id)
        .order_by(Invite.created_at.desc())
    )
    return list(result.scalars())


async def delete_invite(session: AsyncSession, invite_id: int) -> None:
    invite = await session.get(Invite, invite_id)
    if invite is not None:
        await session.delete(invite)
        await session.flush()


# --- WdttAccess (обход белых списков) -----------------------------------------

async def create_wdtt_access(
    session: AsyncSession,
    *,
    server_id: int,
    user_id: int,
    label: str,
    uri_enc: bytes,
    password_enc: bytes,
    expires_at: datetime | None,
    device_id: int | None = None,
    platform: str | None = None,
) -> WdttAccess:
    access = WdttAccess(
        server_id=server_id,
        user_id=user_id,
        device_id=device_id,
        label=label,
        uri_enc=uri_enc,
        password_enc=password_enc,
        status=PeerStatus.ACTIVE,
        expires_at=expires_at,
        platform=platform,
    )
    session.add(access)
    await session.flush()
    return access


async def count_active_wdtt_for_user(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(WdttAccess.id))
            .where(WdttAccess.user_id == user_id)
            .where(WdttAccess.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0


async def get_wdtt_access(session: AsyncSession, access_id: int) -> WdttAccess | None:
    return await session.get(WdttAccess, access_id)


async def list_wdtt_for_user(session: AsyncSession, user_id: int) -> list[WdttAccess]:
    result = await session.execute(
        select(WdttAccess).where(WdttAccess.user_id == user_id).order_by(WdttAccess.id)
    )
    return list(result.scalars())


async def list_wdtt_for_server(
    session: AsyncSession, server_id: int
) -> list[WdttAccess]:
    result = await session.execute(
        select(WdttAccess)
        .where(WdttAccess.server_id == server_id)
        .order_by(WdttAccess.id)
    )
    return list(result.scalars())


async def revoke_wdtt_access(session: AsyncSession, access_id: int) -> None:
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.id == access_id)
        .values(status=PeerStatus.REVOKED, revoked_at=datetime.now(timezone.utc))
    )


async def delete_wdtt_access(session: AsyncSession, access_id: int) -> None:
    access = await session.get(WdttAccess, access_id)
    if access is not None:
        await session.delete(access)
        await session.flush()


# --- Devices / Subscription (Блок 9) ------------------------------------------

async def create_device(
    session: AsyncSession, *, user_id: int, label: str
) -> Device:
    device = Device(user_id=user_id, label=label, status=PeerStatus.ACTIVE)
    session.add(device)
    await session.flush()
    return device


async def get_device(session: AsyncSession, device_id: int) -> Device | None:
    return await session.get(Device, device_id)


async def list_devices_for_user(
    session: AsyncSession, user_id: int, *, active_only: bool = False
) -> list[Device]:
    stmt = select(Device).where(Device.user_id == user_id)
    if active_only:
        stmt = stmt.where(Device.status == PeerStatus.ACTIVE)
    stmt = stmt.order_by(Device.id)
    return list((await session.execute(stmt)).scalars())


async def count_active_devices(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(Device.id))
            .where(Device.user_id == user_id)
            .where(Device.status == PeerStatus.ACTIVE)
        )
    ).scalar() or 0


async def revoke_device(session: AsyncSession, device_id: int) -> None:
    """Отзывает устройство. Пиры и доступы обхода → REVOKED: строки ждут
    возможного ревайва при продлении подписки (пир держит IP, wdtt — пароль,
    который сервер умеет восстановить через ctl add -password). Планировщик
    чистит их через retention."""
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Device).where(Device.id == device_id).values(status=PeerStatus.REVOKED)
    )
    await session.execute(
        update(Peer)
        .where(Peer.device_id == device_id)
        .where(Peer.status == PeerStatus.ACTIVE)
        .values(status=PeerStatus.REVOKED, revoked_at=now)
    )
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.device_id == device_id)
        .where(WdttAccess.status == PeerStatus.ACTIVE)
        .values(status=PeerStatus.REVOKED, revoked_at=now)
    )


async def revive_wdtt_access(session: AsyncSession, access_id: int) -> None:
    # Пароль заново добавлен на сервер → его счётчики Up/Down стартуют с нуля;
    # сбрасываем накопитель, чтобы защита от сброса не насчитала лишнего.
    await session.execute(
        update(WdttAccess)
        .where(WdttAccess.id == access_id)
        .values(
            status=PeerStatus.ACTIVE,
            revoked_at=None,
            traffic_used_bytes=0,
            traffic_last_raw_bytes=0,
            expiry_warn_flags=0,
        )
    )


async def delete_device(session: AsyncSession, device_id: int) -> None:
    """Полностью удаляет устройство из БД: его wdtt-доступы и пиры (освобождает
    их IP) + саму запись устройства. Снятие пиров с сервера по SSH — на вызывающем.
    Отозванный/удалённый девайс не оставляем мусором (в отличие от 30-дн retention
    у одиночных пиров — тут юзер явно удаляет своё устройство)."""
    await session.execute(
        delete(WdttAccess).where(WdttAccess.device_id == device_id)
    )
    await session.execute(delete(Peer).where(Peer.device_id == device_id))
    await session.execute(delete(Device).where(Device.id == device_id))


async def backfill_devices(session: AsyncSession) -> int:
    """Грандфазер (Блок 9): активные пиры без device_id заворачиваем в устройства.
    Идемпотентно — берём только device_id IS NULL. Отозванные не трогаем."""
    peers = list((await session.execute(
        select(Peer)
        .where(Peer.device_id.is_(None))
        .where(Peer.status == PeerStatus.ACTIVE)
    )).scalars())
    for p in peers:
        device = Device(user_id=p.user_id, label=p.label, status=PeerStatus.ACTIVE)
        session.add(device)
        await session.flush()
        p.device_id = device.id
    return len(peers)


async def list_peers_for_device(session: AsyncSession, device_id: int) -> list[Peer]:
    return list(
        (await session.execute(
            select(Peer).where(Peer.device_id == device_id).order_by(Peer.id)
        )).scalars()
    )


async def list_wdtt_for_device(
    session: AsyncSession, device_id: int
) -> list[WdttAccess]:
    return list(
        (await session.execute(
            select(WdttAccess)
            .where(WdttAccess.device_id == device_id)
            .order_by(WdttAccess.id)
        )).scalars()
    )


async def sum_user_traffic(session: AsyncSession, user_id: int) -> int:
    """Суммарный трафик юзера за всё время = WG-пиры + доступы обхода БС.

    Отозванные тоже считаем — трафик уже потрачен. Разница с sub_traffic_base_bytes
    даёт расход за текущий период подписки."""
    peers = (await session.execute(
        select(func.coalesce(func.sum(Peer.traffic_used_bytes), 0))
        .where(Peer.user_id == user_id)
    )).scalar() or 0
    wdtt = (await session.execute(
        select(func.coalesce(func.sum(WdttAccess.traffic_used_bytes), 0))
        .where(WdttAccess.user_id == user_id)
    )).scalar() or 0
    return peers + wdtt


async def sub_traffic_used(session: AsyncSession, user: User) -> int:
    """Расход трафика за текущий период = Σ пиров − base (не меньше нуля)."""
    total = await sum_user_traffic(session, user.id)
    return max(0, total - (user.sub_traffic_base_bytes or 0))


async def set_subscription(
    session: AsyncSession,
    user_id: int,
    *,
    max_devices: int | None = None,
    max_bypass: int | None = None,
    expires_at: datetime | None = None,
    touch_expires: bool = False,
    traffic_limit_bytes: int | None = None,
    touch_traffic_limit: bool = False,
    reset_traffic_base: bool = False,
    mark_paid: bool = False,
) -> None:
    """Обновляет подписку юзера. expires_at/traffic_limit меняются только при
    соответствующем touch_* (иначе None трактовался бы как «снять»). При продлении
    (reset_traffic_base=True) обнуляем расход периода: base := текущая Σ трафика."""
    values: dict = {}
    if max_devices is not None:
        values["sub_max_devices"] = max_devices
    if max_bypass is not None:
        values["sub_max_bypass"] = max_bypass
    if touch_expires:
        values["sub_expires_at"] = expires_at
        values["sub_warn_flags"] = 0  # новый срок → предупреждаем заново
    if touch_traffic_limit:
        values["sub_traffic_limit_bytes"] = traffic_limit_bytes
    if reset_traffic_base:
        values["sub_traffic_base_bytes"] = await sum_user_traffic(session, user_id)
    if mark_paid:
        values["is_trial"] = False
    if values:
        await session.execute(
            update(User).where(User.id == user_id).values(**values)
        )


# ── Блок «Баланс»: баланс, журнал, инвойсы, рефералка ────────────────────────

async def add_balance_tx(
    session: AsyncSession,
    user_id: int,
    amount_kopeks: int,
    kind: str,
    note: str | None = None,
) -> None:
    """ЕДИНСТВЕННАЯ точка изменения баланса: атомарный инкремент User.balance_kopeks
    + строка журнала balance_txs. kind: deposit | charge | ref | admin.
    Коммит — на вызывающем."""
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(balance_kopeks=User.balance_kopeks + amount_kopeks)
    )
    session.add(BalanceTx(
        user_id=user_id, amount_kopeks=amount_kopeks, kind=kind, note=note
    ))
    await session.flush()


async def list_balance_txs(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[BalanceTx]:
    return list((await session.execute(
        select(BalanceTx)
        .where(BalanceTx.user_id == user_id)
        .order_by(BalanceTx.id.desc())
        .limit(limit)
    )).scalars())


async def create_crypto_invoice(
    session: AsyncSession, *, user_id: int, invoice_id: int,
    amount_kopeks: int, url: str,
) -> CryptoInvoice:
    inv = CryptoInvoice(
        user_id=user_id, invoice_id=invoice_id,
        amount_kopeks=amount_kopeks, url=url,
    )
    session.add(inv)
    await session.flush()
    return inv


async def get_crypto_invoice(session: AsyncSession, row_id: int) -> CryptoInvoice | None:
    return await session.get(CryptoInvoice, row_id)


async def list_open_invoices(
    session: AsyncSession, *, max_age_days: int = 3
) -> list[CryptoInvoice]:
    """Активные инвойсы для поллинга планировщиком. Старше max_age_days не трогаем —
    Crypto Pay столько не живёт (наш expires_in час), это страховка от вечного опроса."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return list((await session.execute(
        select(CryptoInvoice)
        .where(CryptoInvoice.status == "active")
        .where(CryptoInvoice.created_at >= cutoff)
    )).scalars())


async def count_referrals(session: AsyncSession, user_id: int) -> int:
    return (await session.execute(
        select(func.count()).select_from(User).where(User.referrer_id == user_id)
    )).scalar_one()


# ── Блок «Сапорт-чат»: маршрутизация вопрос↔ответ ────────────────────────────

async def add_support_route(
    session: AsyncSession, *, user_id: int, user_tg_id: int, user_msg_id: int,
    admin_tg_id: int, admin_msg_id: int,
) -> None:
    """Запоминает пару (сообщение у юзера ↔ сообщение у админа). Пишется при
    копировании вопроса админу И при доставке ответа юзеру — реплай на любую
    сторону продолжает переписку. Коммит — на вызывающем."""
    session.add(SupportMsg(
        user_id=user_id, user_tg_id=user_tg_id, user_msg_id=user_msg_id,
        admin_tg_id=admin_tg_id, admin_msg_id=admin_msg_id,
    ))
    await session.flush()


async def find_support_route_by_admin_msg(
    session: AsyncSession, admin_tg_id: int, admin_msg_id: int
) -> SupportMsg | None:
    return (await session.execute(
        select(SupportMsg)
        .where(SupportMsg.admin_tg_id == admin_tg_id)
        .where(SupportMsg.admin_msg_id == admin_msg_id)
    )).scalars().first()


async def is_support_reply_from_user(
    session: AsyncSession, user_tg_id: int, user_msg_id: int
) -> bool:
    """True, если юзер реплаит на сообщение, доставленное ему сапорт-чатом."""
    return (await session.execute(
        select(SupportMsg.id)
        .where(SupportMsg.user_tg_id == user_tg_id)
        .where(SupportMsg.user_msg_id == user_msg_id)
    )).scalars().first() is not None


async def purge_old_support_routes(session: AsyncSession, days: int = 30) -> int:
    """Удаляет маршруты старше days дней (реплай на них перестанет доставляться,
    но живой переписке 30 дней хватает с запасом). Возвращает число удалённых."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        delete(SupportMsg).where(SupportMsg.created_at < cutoff)
    )
    return result.rowcount or 0


async def sum_ref_earned(session: AsyncSession, user_id: int) -> int:
    """Сколько копеек юзер заработал на рефах за всё время (kind='ref')."""
    return (await session.execute(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.user_id == user_id)
        .where(BalanceTx.kind == "ref")
    )).scalar_one()
