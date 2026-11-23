"""API эндпоинты для веб-панели администратора."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web
from loguru import logger
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.db import repo
from bot.db.base import session_scope
from bot.db.models import (
    AuditAction,
    AuditLog,
    Device,
    Peer,
    PeerStatus,
    PlategaPayment,
    Server,
    ServerStatus,
    User,
    WdttAccess,
)
from bot.services import amnezia, pricing
from bot.services import revive as revive_svc
from bot.services.pricing import fmt_rub
from bot.utils.timefmt import as_utc, fmt_msk


async def get_overview_data(session: AsyncSession) -> dict[str, Any]:
    """Формирует данные общей сводки сервиса."""
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    # Статистика пользователей
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    active_paid = (await session.execute(
        select(func.count(User.id)).where(
            User.is_blocked.is_(False),
            User.is_staff.is_(False),
            User.is_trial.is_(False),
            (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now),
        )
    )).scalar() or 0
    active_trial = (await session.execute(
        select(func.count(User.id)).where(
            User.is_blocked.is_(False),
            User.is_staff.is_(False),
            User.is_trial.is_(True),
            User.sub_expires_at > now,
        )
    )).scalar() or 0
    expired_users = (await session.execute(
        select(func.count(User.id)).where(
            User.is_blocked.is_(False),
            User.is_staff.is_(False),
            User.sub_expires_at.isnot(None),
            User.sub_expires_at <= now,
        )
    )).scalar() or 0
    blocked_users = (await session.execute(
        select(func.count(User.id)).where(User.is_blocked.is_(True))
    )).scalar() or 0
    vip_users = (await session.execute(
        select(func.count(User.id)).where(User.is_vip.is_(True))
    )).scalar() or 0

    # Серверы и пиры
    total_servers = (await session.execute(select(func.count(Server.id)))).scalar() or 0
    ready_servers = (await session.execute(
        select(func.count(Server.id)).where(Server.status == ServerStatus.READY)
    )).scalar() or 0
    active_peers = (await session.execute(
        select(func.count(Peer.id)).where(Peer.status == PeerStatus.ACTIVE)
    )).scalar() or 0
    active_bypass = (await session.execute(
        select(func.count(WdttAccess.id)).where(WdttAccess.status == PeerStatus.ACTIVE)
    )).scalar() or 0

    # Выручка за 30 дней (успешные платежи Platega)
    revenue_kopeks = (await session.execute(
        select(func.coalesce(func.sum(PlategaPayment.amount_kopeks), 0)).where(
            PlategaPayment.status == "CONFIRMED",
            PlategaPayment.created_at >= thirty_days_ago,
        )
    )).scalar() or 0

    # Суммарный трафик сервиса
    peer_traffic = (await session.execute(
        select(func.coalesce(func.sum(Peer.traffic_used_bytes), 0))
    )).scalar() or 0
    wdtt_traffic = (await session.execute(
        select(func.coalesce(func.sum(WdttAccess.traffic_used_bytes), 0))
    )).scalar() or 0
    total_traffic_gb = round((peer_traffic + wdtt_traffic) / (1024**3), 2)

    return {
        "users": {
            "total": total_users,
            "active_paid": active_paid,
            "active_trial": active_trial,
            "expired": expired_users,
            "blocked": blocked_users,
            "vip": vip_users,
        },
        "finance": {
            "revenue_30d_rub": round(revenue_kopeks / 100, 2),
            "formatted_revenue": fmt_rub(revenue_kopeks),
        },
        "infrastructure": {
            "total_servers": total_servers,
            "ready_servers": ready_servers,
            "active_peers": active_peers,
            "active_bypass": active_bypass,
            "total_traffic_gb": total_traffic_gb,
        },
    }


async def api_overview(request: web.Request) -> web.Response:
    """Общая сводка: пользователи, финансы, серверы, нагрузка."""
    async with session_scope() as session:
        data = await get_overview_data(session)
    return web.json_response(data)



async def api_servers_list(request: web.Request) -> web.Response:
    """Список серверов со статусом и текущей загрузкой."""
    async with session_scope() as session:
        servers = list((await session.execute(select(Server).order_by(Server.id))).scalars())
        peer_counts = await repo.count_active_peers_by_server(session)
        wdtt_counts = await repo.count_active_wdtt_by_server(session)

        items = []
        for s in servers:
            p_count = peer_counts.get(s.id, 0)
            w_count = wdtt_counts.get(s.id, 0)
            items.append({
                "id": s.id,
                "name": s.name,
                "host": s.host,
                "ssh_port": s.ssh_port,
                "location": s.location or "",
                "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                "wg_port": s.wg_port,
                "wg_interface": s.wg_interface,
                "wg_subnet": s.wg_subnet,
                "dns": s.dns or "1.1.1.1, 1.0.0.1",
                "max_peers": s.max_peers,
                "active_peers": p_count,
                "wdtt_enabled": s.wdtt_enabled,
                "wdtt_ports": s.wdtt_ports or "",
                "wdtt_max_accesses": s.wdtt_max_accesses,
                "active_bypass": w_count,
                "is_private": s.is_private,
                "last_error": s.last_error or "",
            })

    return web.json_response({"servers": items})


async def api_server_update(request: web.Request) -> web.Response:
    """Обновление настроек сервера (название, локация, лимиты, тумблеры)."""
    srv_id = int(request.match_info["id"])
    body = await request.json()

    async with session_scope() as session:
        srv = await session.get(Server, srv_id)
        if srv is None:
            raise web.HTTPNotFound(text="Server not found")

        if "name" in body and body["name"]:
            srv.name = str(body["name"]).strip()
        if "location" in body:
            srv.location = str(body["location"]).strip() or None
        if "dns" in body:
            srv.dns = str(body["dns"]).strip() or None
        if "max_peers" in body:
            val = body["max_peers"]
            srv.max_peers = int(val) if val is not None and val != "" else None
        if "wdtt_max_accesses" in body:
            val = body["wdtt_max_accesses"]
            srv.wdtt_max_accesses = int(val) if val is not None and val != "" else None
        if "wdtt_enabled" in body:
            srv.wdtt_enabled = bool(body["wdtt_enabled"])
        if "is_private" in body:
            srv.is_private = bool(body["is_private"])

        await session.commit()

    return web.json_response({"ok": True})


async def get_users_list_data(
    session: AsyncSession,
    q: str = "",
    status_filter: str = "all",
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """Формирует список пользователей с фильтрацией, поиском и пагинацией."""
    now = datetime.now(timezone.utc)
    stmt = select(User)

    if q:
        if q.isdigit():
            stmt = stmt.where((User.id == int(q)) | (User.tg_id == int(q)))
        else:
            like_str = f"%{q.lstrip('@')}%"
            stmt = stmt.where(
                (User.username.ilike(like_str))
                | (User.full_name.ilike(like_str))
                | (User.ref_code.ilike(like_str))
            )

    if status_filter == "active":
        stmt = stmt.where(
            User.is_blocked.is_(False),
            (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now),
        )
    elif status_filter == "paid":
        stmt = stmt.where(
            User.is_blocked.is_(False),
            User.is_trial.is_(False),
            (User.sub_expires_at.is_(None)) | (User.sub_expires_at > now),
        )
    elif status_filter == "trial":
        stmt = stmt.where(
            User.is_blocked.is_(False),
            User.is_trial.is_(True),
            User.sub_expires_at > now,
        )
    elif status_filter == "expired":
        stmt = stmt.where(
            User.is_blocked.is_(False),
            User.sub_expires_at.isnot(None),
            User.sub_expires_at <= now,
        )
    elif status_filter == "blocked":
        stmt = stmt.where(User.is_blocked.is_(True))
    elif status_filter == "vip":
        stmt = stmt.where(User.is_vip.is_(True))

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(total_stmt)).scalar() or 0

    users = list((
        await session.execute(
            stmt.order_by(User.id.desc()).offset(offset).limit(limit)
        )
    ).scalars())

    items = []
    for u in users:
        traffic_used = await repo.sub_traffic_used(session, u)
        is_expired = bool(u.sub_expires_at and as_utc(u.sub_expires_at) <= now)
        items.append({
            "id": u.id,
            "tg_id": u.tg_id,
            "username": u.username or "",
            "full_name": u.full_name or "",
            "balance_rub": round(u.balance_kopeks / 100, 2),
            "is_trial": u.is_trial,
            "is_vip": u.is_vip,
            "is_staff": u.is_staff,
            "is_blocked": u.is_blocked,
            "is_expired": is_expired,
            "sub_expires_at": u.sub_expires_at.isoformat() if u.sub_expires_at else None,
            "sub_expires_msk": fmt_msk(u.sub_expires_at) if u.sub_expires_at else "Бессрочно",
            "sub_max_devices": u.sub_max_devices,
            "sub_max_bypass": u.sub_max_bypass,
            "traffic_used_mb": round(traffic_used / (1024 * 1024), 2),
            "traffic_limit_mb": (
                round(u.sub_traffic_limit_bytes / (1024 * 1024), 2)
                if u.sub_traffic_limit_bytes else 0
            ),
            "ref_code": u.ref_code or "",
            "sub_token": u.sub_token or "",
            "created_at_msk": fmt_msk(u.created_at),
        })

    return {"total": total, "items": items, "offset": offset, "limit": limit}


async def api_users_list(request: web.Request) -> web.Response:
    """Список пользователей с фильтрацией, поиском и пагинацией."""
    q = request.query.get("q", "").strip()
    status_filter = request.query.get("filter", "all").lower()
    offset = int(request.query.get("offset", 0))
    limit = min(int(request.query.get("limit", 25)), 100)

    async with session_scope() as session:
        data = await get_users_list_data(
            session,
            q=q,
            status_filter=status_filter,
            offset=offset,
            limit=limit,
        )

    return web.json_response(data)


async def api_user_detail(request: web.Request) -> web.Response:
    """Подробная карточка пользователя со всеми его устройствами, пирами и платежами."""
    user_id = int(request.match_info["id"])
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise web.HTTPNotFound(text="User not found")

        devices = await repo.list_devices_for_user(session, user.id)
        peers = await repo.list_peers_for_user(session, user.id)
        wdtt_list = await repo.list_wdtt_for_user(session, user.id)
        traffic_used = await repo.sub_traffic_used(session, user)

        devices_json = [
            {
                "id": d.id,
                "label": d.label,
                "status": d.status.value if hasattr(d.status, "value") else str(d.status),
                "created_at": fmt_msk(d.created_at),
            }
            for d in devices
        ]
        peers_json = [
            {
                "id": p.id,
                "server_id": p.server_id,
                "device_id": p.device_id,
                "label": p.label,
                "ip": p.ip,
                "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                "traffic_used_mb": round((p.traffic_used_bytes or 0) / (1024 * 1024), 2),
                "created_at": fmt_msk(p.created_at),
            }
            for p in peers
        ]
        wdtt_json = [
            {
                "id": w.id,
                "server_id": w.server_id,
                "device_id": w.device_id,
                "label": w.label,
                "platform": w.platform or "",
                "status": w.status.value if hasattr(w.status, "value") else str(w.status),
                "traffic_used_mb": round((w.traffic_used_bytes or 0) / (1024 * 1024), 2),
                "created_at": fmt_msk(w.created_at),
            }
            for w in wdtt_list
        ]

        sub_token = user.sub_token or await repo.get_or_create_sub_token(session, user.id)

    return web.json_response({
        "id": user.id,
        "tg_id": user.tg_id,
        "username": user.username or "",
        "full_name": user.full_name or "",
        "balance_rub": round(user.balance_kopeks / 100, 2),
        "is_trial": user.is_trial,
        "is_vip": user.is_vip,
        "is_staff": user.is_staff,
        "is_blocked": user.is_blocked,
        "autopay": user.autopay,
        "sub_expires_at": user.sub_expires_at.isoformat() if user.sub_expires_at else None,
        "sub_expires_msk": fmt_msk(user.sub_expires_at) if user.sub_expires_at else "Бессрочно",
        "sub_max_devices": user.sub_max_devices,
        "sub_max_bypass": user.sub_max_bypass,
        "traffic_used_mb": round(traffic_used / (1024 * 1024), 2),
        "traffic_limit_mb": (
            round(user.sub_traffic_limit_bytes / (1024 * 1024), 2)
            if user.sub_traffic_limit_bytes else 0
        ),
        "sub_token": sub_token,
        "sub_url": f"/sub/{sub_token}",
        "devices": devices_json,
        "peers": peers_json,
        "bypass": wdtt_json,
    })


async def api_user_subscription(request: web.Request) -> web.Response:
    """Управление подпиской юзера: изменение срока, лимитов, сброс расхода."""
    user_id = int(request.match_info["id"])
    admin_id = request.get("admin_id", 0)
    body = await request.json()

    now = datetime.now(timezone.utc)

    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise web.HTTPNotFound(text="User not found")

        max_dev = int(body["max_devices"]) if "max_devices" in body else None
        max_byp = int(body["max_bypass"]) if "max_bypass" in body else None

        touch_expires = False
        expires_at = None
        if "add_days" in body:
            touch_expires = True
            days = int(body["add_days"])
            current_exp = as_utc(user.sub_expires_at) if user.sub_expires_at else now
            base_time = max(now, current_exp)
            expires_at = base_time + timedelta(days=days)
        elif "expires_at" in body:
            touch_expires = True
            raw_exp = body["expires_at"]
            if raw_exp:
                # ISO формат
                expires_at = datetime.fromisoformat(raw_exp.replace("Z", "+00:00"))

        reset_traffic = bool(body.get("reset_traffic", False))
        mark_paid = bool(body.get("mark_paid", False))
        mark_trial = bool(body.get("mark_trial", False))

        await repo.set_subscription(
            session,
            user_id,
            max_devices=max_dev,
            max_bypass=max_byp,
            expires_at=expires_at,
            touch_expires=touch_expires,
            reset_traffic_base=reset_traffic,
            mark_paid=mark_paid,
            mark_trial=mark_trial,
        )

        turning_off = touch_expires and expires_at is not None and as_utc(expires_at) <= now
        turning_on = touch_expires and (expires_at is None or as_utc(expires_at) > now)

        revive_result = None
        if turning_off:
            await revive_svc.revoke_devices_for_user(
                session,
                user.id,
                actor_tg_id=admin_id if admin_id else None,
                actor_is_admin=True,
                reason="подписка отключена через веб-панель",
            )
            await repo.log_action(
                session,
                AuditAction.SUB_REVOKED,
                actor_tg_id=admin_id if admin_id else None,
                actor_is_admin=True,
                target_user_id=user.id,
                details=f"Админ через веб-панель отключил подписку (срок {fmt_msk(expires_at)} МСК)",
            )
        else:
            if turning_on or max_dev is not None or max_byp is not None:
                revive_result = await revive_svc.revive_devices_for_user(session, user)

            if touch_expires:
                await repo.log_action(
                    session,
                    AuditAction.SUB_GRANTED,
                    actor_tg_id=admin_id if admin_id else None,
                    actor_is_admin=True,
                    target_user_id=user.id,
                    details=(
                        f"Админ через веб-панель задал срок подписки: до {fmt_msk(expires_at)} МСК"
                        if expires_at else "Админ через веб-панель сделал подписку бессрочной"
                    ),
                )

        if max_dev is not None or max_byp is not None:
            await repo.log_action(
                session,
                AuditAction.TARIFF_CHANGED,
                actor_tg_id=admin_id if admin_id else None,
                actor_is_admin=True,
                target_user_id=user.id,
                details=f"Тариф изменён через веб-панель: устройств {user.sub_max_devices}, обходов {user.sub_max_bypass}",
            )

        await session.commit()

        if turning_on:
            sub_line = f"до {fmt_msk(expires_at)} МСК" if expires_at else "бессрочная"
            notify = f"🎉 Подписка продлена ({sub_line})."
            if revive_result and (revive_result.devices_restored or revive_result.bypass_restored):
                notify += (
                    "\n♻️ Твои устройства восстановлены — прежние конфиги и ссылки "
                    "снова работают, ничего перенастраивать не нужно."
                )
            try:
                from bot.loader import bot as tg_bot
                await tg_bot.send_message(user.tg_id, notify)
            except Exception:
                pass

        res_data: dict[str, Any] = {"ok": True}
        if expires_at:
            res_data["expires_at"] = expires_at.isoformat()
            res_data["expires_msk"] = fmt_msk(expires_at)
        if revive_result and revive_result.touched:
            res_data["revived"] = {
                "devices": revive_result.devices_restored,
                "bypass": revive_result.bypass_restored,
                "errors": revive_result.errors,
            }

    return web.json_response(res_data)


async def api_user_action(request: web.Request) -> web.Response:
    """Быстрые действия над пользователем (VIP, блокировка, сброс токена, баланс)."""
    user_id = int(request.match_info["id"])
    body = await request.json()
    action = body.get("action")

    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise web.HTTPNotFound(text="User not found")

        if action == "toggle_vip":
            user.is_vip = not user.is_vip
            await session.commit()
            return web.json_response({"ok": True, "is_vip": user.is_vip})

        if action == "toggle_staff":
            user.is_staff = not user.is_staff
            await session.commit()
            return web.json_response({"ok": True, "is_staff": user.is_staff})

        if action == "toggle_block":
            user.is_blocked = not user.is_blocked
            await session.commit()
            return web.json_response({"ok": True, "is_blocked": user.is_blocked})

        if action == "rotate_sub_token":
            new_token = await repo.rotate_sub_token(session, user.id)
            return web.json_response({"ok": True, "sub_token": new_token, "sub_url": f"/sub/{new_token}"})

        if action == "adjust_balance":
            amount_rub = float(body.get("amount_rub", 0))
            kopeks = int(round(amount_rub * 100))
            admin_id = request.get("admin_id", 0)
            reason = str(body.get("reason", "Коррекция через веб-панель"))
            await repo.add_balance_tx(
                session,
                user_id=user.id,
                amount_kopeks=kopeks,
                tx_type="ADMIN_ADJUST",
                description=f"Админ {admin_id}: {reason}",
            )
            await session.commit()
            return web.json_response({"ok": True, "new_balance_rub": round(user.balance_kopeks / 100, 2)})

    return web.json_response({"error": "Unknown action"}, status=400)


async def api_user_wipe(request: web.Request) -> web.Response:
    """Полное стирание пользователя из базы (с отзывом конфигов на нодах)."""
    user_id = int(request.match_info["id"])
    admin_id = request.get("admin_id", 0)

    from bot.services.user_wipe import wipe_user

    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise web.HTTPNotFound(text="User not found")
        if user.is_admin:
            return web.json_response({"error": "Нельзя удалить администратора"}, status=400)

        # Вызываем wipe_user
        res = await wipe_user(session, user_id=user.id, actor_tg_id=admin_id)
        await session.commit()

    return web.json_response({"ok": True, "result": res})


async def api_audit_list(request: web.Request) -> web.Response:
    """Журнал аудита действий."""
    offset = int(request.query.get("offset", 0))
    limit = min(int(request.query.get("limit", 50)), 100)

    async with session_scope() as session:
        logs = await repo.list_audit(session, offset=offset, limit=limit)
        total = await repo.count_audit(session)

        items = [
            {
                "id": log.id,
                "actor_tg_id": log.actor_tg_id,
                "action": log.action,
                "target_type": log.target_type,
                "target_id": log.target_id,
                "details": log.details or "",
                "created_at_msk": fmt_msk(log.created_at),
            }
            for log in logs
        ]

    return web.json_response({"total": total, "items": items, "offset": offset, "limit": limit})


async def api_tariffs_get(request: web.Request) -> web.Response:
    """Текущие тарифные ставки и параметры."""
    return web.json_response({
        "price_first_rub": settings.price_first_rub,
        "price_extra_device_rub": settings.price_extra_device_rub,
        "price_extra_bypass_rub": settings.price_extra_bypass_rub,
        "trial_devices": settings.trial_devices,
        "trial_bypass": settings.trial_bypass,
        "trial_days": settings.trial_days,
        "trial_traffic_gb": settings.trial_traffic_gb,
        "referral_percent": settings.referral_percent,
    })


async def api_broadcast_send(request: web.Request) -> web.Response:
    """Отправка рассылки в Telegram."""
    body = await request.json()
    text = body.get("text", "").strip()
    target = body.get("target", "all")
    if not text:
        return web.json_response({"error": "Текст рассылки пуст"}, status=400)

    from bot.loader import bot

    async with session_scope() as session:
        users = await repo.list_broadcast_targets(session, target=target)

    async def _send_all():
        success = 0
        failed = 0
        for u in users:
            try:
                await bot.send_message(u.tg_id, text)
                success += 1
                await asyncio.sleep(0.05)  # Telegram rate limit safety
            except Exception:
                failed += 1
        logger.info("Рассылка завершена: {} доставлено, {} сбоев", success, failed)

    asyncio.create_task(_send_all())
    return web.json_response({"ok": True, "target_count": len(users)})
