"""Фоновый планировщик: автоотзыв по истечению ПОДПИСКИ и лимиту трафика подписки.

Единый таймер сервиса — подписка юзера (`User.sub_expires_at` + `sub_traffic_limit_bytes`).
У отдельных пиров/доступов своего срока больше нет: устройства живут и умирают вместе
с подпиской. Трафик считается суммарно по юзеру за период."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from bot.config import settings
from bot.db import repo
from bot.db.base import session_scope
from bot.db.models import Device, Peer, PeerStatus, User, WdttAccess
from bot.loader import bot
from bot.services import amnezia
from bot.services import wdtt as wdtt_svc
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHError


# Пороги предупреждений о скором истечении подписки (часов до отзыва). Порядок =
# номер бита в User.sub_warn_flags. v1 — фиксированные; позже можно сделать настройку.
WARN_OFFSETS_HOURS = (24, 1)

# Сколько дней отозванные пиры/wdtt-доступы хранятся в БД, прежде чем
# планировщик удалит их. Отзыв не удаляет строки сразу — они «ждут» продления
# подписки (Блок «Ревайв»: пир держит свои ключи+IP, wdtt — пароль, который
# сервер восстанавливает через ctl add -password). По истечении срока чистим,
# чтобы не копить мусор и освободить IP.
REVOKED_RETENTION_DAYS = 30


async def _notify(tg_id: int, text: str) -> None:
    try:
        await bot.send_message(tg_id, text)
    except Exception:
        pass


def _humanize_left(delta: timedelta) -> str:
    """Грубое «сколько осталось» для текста предупреждения."""
    minutes = int(delta.total_seconds() // 60)
    if minutes >= 1440:
        return f"{minutes // 1440} дн"
    if minutes >= 60:
        return f"{minutes // 60} ч"
    return f"{max(minutes, 1)} мин"


def _as_utc(dt: datetime) -> datetime:
    """SQLite отдаёт datetime без таймзоны — считаем такие значения UTC.

    Без этого арифметика `expires_at - now` (aware) падает с TypeError.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _revoke_all_devices_for_user(session, user_id: int) -> bool:
    """Отзывает ВСЕ активные устройства юзера: снимает WG-пиры и wdtt-доступы с
    серверов (best-effort), затем метит устройства/пиры/wdtt-строки REVOKED
    (см. repo.revoke_device) — они ждут ревайва при продлении retention-срок.
    Возвращает True, если что-то отозвали (для уведомления)."""
    devices = await repo.list_devices_for_user(session, user_id, active_only=True)
    if not devices:
        return False
    for device in devices:
        for peer in await repo.list_peers_for_device(session, device.id):
            if peer.status != PeerStatus.ACTIVE:
                continue
            server = await repo.get_server(session, peer.server_id)
            if server:
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        await amnezia.remove_peer_on_server(ssh, public_key=peer.public_key)
                except SSHError as exc:
                    logger.warning("Revoke-all peer remove err {}: {}", peer.id, exc)
        for acc in await repo.list_wdtt_for_device(session, device.id):
            if acc.status != PeerStatus.ACTIVE:
                continue
            server = await repo.get_server(session, acc.server_id)
            if server:
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        await wdtt_svc.remove_access(
                            ssh, password=decrypt(acc.password_enc),
                            binary=settings.wdtt_binary_path,
                        )
                except SSHError as exc:
                    logger.warning("Revoke-all wdtt remove err {}: {}", acc.id, exc)
        await repo.revoke_device(session, device.id)
        logger.info("Auto-revoked device {} (user {})", device.id, user_id)
    return True


async def _poll_crypto_invoices(session) -> None:
    """Сверяет активные инвойсы с Crypto Pay: paid → зачислить (идемпотентно),
    expired → пометить. Ошибки API не валят тик — попробуем на следующем."""
    from bot.services import billing, cryptopay

    if not cryptopay.enabled():
        return
    open_invoices = await repo.list_open_invoices(session)
    if not open_invoices:
        return
    try:
        statuses = await cryptopay.get_invoice_statuses(
            [inv.invoice_id for inv in open_invoices]
        )
    except cryptopay.CryptoPayError as exc:
        logger.warning("CryptoPay poll failed: {}", exc)
        return
    changed = False
    for inv in open_invoices:
        status = statuses.get(inv.invoice_id)
        if status == "paid":
            dep = await billing.apply_paid_invoice(session, inv)
            changed = True
            from bot.handlers.balance import notify_deposit
            await notify_deposit(dep)
        elif status == "expired":
            inv.status = "expired"
            changed = True
    if changed:
        await session.commit()


async def _try_autopay(session, user: User) -> bool:
    """Автопродление при истечении: если включено и баланса хватает — списываем
    1 месяц ТЕКУЩЕГО тарифа вместо отзыва устройств. True — подписка продлена."""
    from bot.services import billing, cryptopay
    from bot.services.pricing import fmt_rub

    if not user.autopay or not cryptopay.enabled():
        return False
    res = await billing.charge_and_extend(session, user, 1)
    if not res.ok:
        return False
    logger.info(
        "Autopay: user {} charged {} kopeks, until {}",
        user.id, res.price_kopeks, res.new_expires_at,
    )
    await _notify(
        user.tg_id,
        f"♻️ Подписка автоматически продлена на месяц за "
        f"{fmt_rub(res.price_kopeks)} с баланса "
        f"(до {res.new_expires_at.strftime('%d.%m.%Y %H:%M')} UTC).\n"
        f"Остаток: {fmt_rub(user.balance_kopeks)}. "
        "Отключить автопродление можно в «🎫 Моя подписка».",
    )
    return True


async def _run_checks() -> None:
    now = datetime.now(timezone.utc)

    async with session_scope() as session:

        # ── 0. Поллинг инвойсов Crypto Pay (Блок «Баланс») ───────────────────
        # Вебхуков нет: добираем оплаты, которые юзер не подтвердил кнопкой
        # «Проверить» (закрыл экран). Зачисление идемпотентно, гонка с кнопкой
        # не задваивает депозит. Делаем ДО истечения — свежие деньги могут
        # спасти подписку автопродлением в секции 1.
        await _poll_crypto_invoices(session)

        # ── 1. Истечение подписки: отзыв ВСЕХ устройств юзера ────────────────
        # Единый гейт сервиса — срок подписки. У кого sub_expires_at <= now, отзываем
        # все активные устройства (WG-пиры + доступы обхода) и уведомляем один раз.
        # У кого устройств уже нет (отозвали на прошлом тике) — helper вернёт False,
        # повторно не уведомляем. Перед отзывом — попытка автопродления с баланса.
        expired_users = list((await session.execute(
            select(User)
            .where(User.sub_expires_at.isnot(None))
            .where(User.sub_expires_at <= now)
        )).scalars())

        touched = False
        for user in expired_users:
            if await _try_autopay(session, user):
                touched = True
                continue
            if await _revoke_all_devices_for_user(session, user.id):
                touched = True
                await _notify(
                    user.tg_id,
                    "⏱ Подписка истекла — устройства и доступы обхода отключены.\n"
                    f"Конфиги сохраняются {REVOKED_RETENTION_DAYS} дней: продлишь "
                    "подписку — всё оживёт само, перенастраивать не придётся.\n"
                    "Продлить: меню → «🎫 Моя подписка» → «🔁 Продлить» "
                    "(пополнить баланс — «💰 Баланс»).",
                )
        if touched:
            await session.commit()

        # ── 1b. Предупреждения о скором истечении подписки ──────────────────
        soon_users = list((await session.execute(
            select(User)
            .where(User.sub_expires_at.isnot(None))
            .where(User.sub_expires_at > now)
        )).scalars())

        warned = False
        for user in soon_users:
            try:
                remaining = _as_utc(user.sub_expires_at) - now
                fireable = [
                    i for i, hours in enumerate(WARN_OFFSETS_HOURS)
                    if not (user.sub_warn_flags & (1 << i))
                    and remaining <= timedelta(hours=hours)
                ]
                if not fireable:
                    continue
                # Помечаем пороги всегда (чтобы не копить «долги» и не слать протухшее),
                # само сообщение — только при включённых предупреждениях и если есть что
                # терять (активные устройства). Одно сообщение за тик.
                for i in fireable:
                    user.sub_warn_flags |= (1 << i)
                warned = True
                if user.expiry_warn_enabled and await repo.count_active_devices(session, user.id):
                    await _notify(
                        user.tg_id,
                        f"⏳ Подписка истекает примерно через {_humanize_left(remaining)}. "
                        "Продли, чтобы устройства и обход БС не отключились.",
                    )
            except Exception:
                logger.exception("Sub expiry-warning failed for user {}", user.id)
        if warned:
            await session.commit()

        # ── 2. Автоудаление давно отозванных пиров ──────────────────────────
        # Чистим строки со status=REVOKED, отозванные более REVOKED_RETENTION_DAYS
        # назад. Освобождает IP (revoked-пир держит его в БД до удаления) и не даёт
        # копиться мусору. Сравнение делаем в SQL, чтобы не спотыкаться о naive
        # datetime из SQLite (см. _as_utc): обе стороны биндятся одинаково.
        cutoff = now - timedelta(days=REVOKED_RETENTION_DAYS)
        stale = list((await session.execute(
            select(Peer)
            .where(Peer.status == PeerStatus.REVOKED)
            .where(Peer.revoked_at.isnot(None))
            .where(Peer.revoked_at < cutoff)
        )).scalars())

        if stale:
            # На случай, если отзыв на сервере когда-то не прошёл по SSH, — best-effort
            # убираем пир с сервера, затем удаляем строку из БД. Группируем по серверу:
            # один SSH-коннект на сервер.
            by_srv: dict[int, list[Peer]] = {}
            for p in stale:
                by_srv.setdefault(p.server_id, []).append(p)
            for server_id, plist in by_srv.items():
                server = await repo.get_server(session, server_id)
                if not server:
                    continue
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        for p in plist:
                            try:
                                await amnezia.remove_peer_on_server(ssh, public_key=p.public_key)
                            except SSHError as exc:
                                logger.warning("Stale-peer remove SSH error peer {}: {}", p.id, exc)
                except SSHError as exc:
                    logger.warning("Stale-peer SSH connect error server {}: {}", server_id, exc)

            for p in stale:
                await repo.delete_peer(session, p.id)
                logger.info("Auto-deleted stale revoked peer {} ({})", p.id, p.label)
            # Фиксируем удаления сразу — как и отзывы выше, чтобы поздний сбой
            # в секции трафика их не откатил.
            await session.commit()

        # ── 2a. Автоудаление давно отозванных wdtt-доступов ─────────────────
        # Симметрично пирам: REVOKED-строки ждут ревайва retention-срок, затем
        # удаляются. Пароль с сервера снят ещё при отзыве; на случай, если тот
        # SSH тогда не прошёл, — best-effort снимаем повторно (идемпотентно).
        stale_wdtt = list((await session.execute(
            select(WdttAccess)
            .where(WdttAccess.status == PeerStatus.REVOKED)
            .where(WdttAccess.revoked_at.isnot(None))
            .where(WdttAccess.revoked_at < cutoff)
        )).scalars())

        if stale_wdtt:
            by_srv_sw: dict[int, list[WdttAccess]] = {}
            for a in stale_wdtt:
                by_srv_sw.setdefault(a.server_id, []).append(a)
            for server_id, alist in by_srv_sw.items():
                server = await repo.get_server(session, server_id)
                if not server:
                    continue
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        for a in alist:
                            try:
                                await wdtt_svc.remove_access(
                                    ssh, password=decrypt(a.password_enc),
                                    binary=settings.wdtt_binary_path,
                                )
                            except SSHError as exc:
                                logger.warning("Stale-wdtt remove SSH error acc {}: {}", a.id, exc)
                except SSHError as exc:
                    logger.warning("Stale-wdtt SSH connect error server {}: {}", server_id, exc)

            for a in stale_wdtt:
                await repo.delete_wdtt_access(session, a.id)
                logger.info("Auto-deleted stale revoked wdtt access {} ({})", a.id, a.label)
            await session.commit()

        # ── 2b. Зомби-устройства ─────────────────────────────────────────────
        # REVOKED-устройство, у которого retention уже удалил все пиры и обходы,
        # восстанавливать нечем — убираем строку, чтобы не висела в списке 🚫.
        zombies = list((await session.execute(
            select(Device)
            .where(Device.status == PeerStatus.REVOKED)
            .where(~select(Peer.id).where(Peer.device_id == Device.id).exists())
            .where(~select(WdttAccess.id).where(WdttAccess.device_id == Device.id).exists())
        )).scalars())
        if zombies:
            for d in zombies:
                await session.delete(d)
                logger.info("Auto-deleted zombie revoked device {} ({})", d.id, d.label)
            await session.commit()

        # ── 3. Учёт трафика (накопление по пирам) ───────────────────────────
        # Копим трафик для ВСЕХ активных пиров, чтобы счётчик пережил ребут сервера.
        # Лимит теперь на ПОДПИСКУ (см. 3b), а не на отдельный пир.
        active = list((await session.execute(
            select(Peer).where(Peer.status == PeerStatus.ACTIVE)
        )).scalars())

        if active:
            by_server: dict[int, list[Peer]] = {}
            for p in active:
                by_server.setdefault(p.server_id, []).append(p)

            for server_id, peers in by_server.items():
                server = await repo.get_server(session, server_id)
                if not server:
                    continue
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        traffic_list = await amnezia.get_peer_traffic(ssh)
                except SSHError as exc:
                    logger.warning("Traffic check SSH error server {}: {}", server_id, exc)
                    continue

                traffic_map = {ti.public_key: ti for ti in traffic_list}
                for peer in peers:
                    ti = traffic_map.get(peer.public_key)
                    if ti is None:
                        continue
                    raw = ti.rx_bytes + ti.tx_bytes
                    peer.traffic_used_bytes, peer.traffic_last_raw_bytes = (
                        amnezia.accumulate_traffic(
                            peer.traffic_used_bytes, peer.traffic_last_raw_bytes, raw
                        )
                    )
            await session.commit()  # зафиксировать обновлённые счётчики

        # ── 3a. Учёт трафика обхода БС (wdtt) ───────────────────────────────
        # wdtt-сервер сам считает Up/DownBytes по паролю; берём их через `ctl list`
        # и копим на WdttAccess (с защитой от сброса, как у пиров). Идёт в лимит
        # подписки. Группируем активные доступы по серверу — один SSH на сервер.
        wdtt_active = list((await session.execute(
            select(WdttAccess).where(WdttAccess.status == PeerStatus.ACTIVE)
        )).scalars())
        if wdtt_active:
            by_srv_w: dict[int, list[WdttAccess]] = {}
            for a in wdtt_active:
                by_srv_w.setdefault(a.server_id, []).append(a)
            for server_id, accs in by_srv_w.items():
                server = await repo.get_server(session, server_id)
                if not server:
                    continue
                try:
                    async with SSHClient(repo.creds_from_server(server)) as ssh:
                        rows = await wdtt_svc.list_accesses(
                            ssh, binary=settings.wdtt_binary_path
                        )
                except SSHError as exc:
                    logger.warning("Wdtt traffic list SSH error server {}: {}", server_id, exc)
                    continue
                by_pw = {r.get("password"): r for r in rows}
                for acc in accs:
                    r = by_pw.get(decrypt(acc.password_enc))
                    if r is None:
                        continue
                    raw = int(r.get("down_bytes", 0)) + int(r.get("up_bytes", 0))
                    acc.traffic_used_bytes, acc.traffic_last_raw_bytes = (
                        amnezia.accumulate_traffic(
                            acc.traffic_used_bytes, acc.traffic_last_raw_bytes, raw
                        )
                    )
            await session.commit()

        # ── 3b. Лимит трафика на ПОДПИСКУ ───────────────────────────────────
        # Расход считается суммарно по юзеру за период (Σ пиров − base). Превысил
        # заданный лимит → отзываем все устройства (как истечение срока) + уведомляем.
        capped_users = list((await session.execute(
            select(User).where(User.sub_traffic_limit_bytes.isnot(None))
        )).scalars())

        limit_touched = False
        for user in capped_users:
            used = await repo.sub_traffic_used(session, user)
            if used < (user.sub_traffic_limit_bytes or 0):
                continue
            if await _revoke_all_devices_for_user(session, user.id):
                limit_touched = True
                logger.info("Auto-revoked user {} devices (traffic limit)", user.id)
                await _notify(
                    user.tg_id,
                    f"📊 Достигнут лимит трафика подписки "
                    f"({amnezia.fmt_bytes(used)} из "
                    f"{amnezia.fmt_bytes(user.sub_traffic_limit_bytes)}). "
                    "Устройства отозваны — для сброса напиши админу.",
                )
        if limit_touched:
            await session.commit()


async def run() -> None:
    """Запускать как asyncio.create_task() при старте бота."""
    logger.info("Peer limit scheduler started (interval: 5 min)")
    while True:
        await asyncio.sleep(300)
        try:
            await _run_checks()
        except Exception:
            logger.exception("Scheduler _run_checks crashed")
