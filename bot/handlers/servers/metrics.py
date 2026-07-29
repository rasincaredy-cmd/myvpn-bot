"""Что происходит на сервере: трафик пиров, состояние железа, чистка лишних пиров."""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus
from bot.keyboards.inline import CB_SERVERS, server_card, stats_nav, traffic_nav
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError

router = Router(name="servers_metrics")


# --- Трафик пиров -----------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:traffic:"))
async def cb_server_traffic(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Читаю счётчики...")

    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            traffic_list = await amnezia.get_peer_traffic(ssh)
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        return

    traffic_map = {ti.public_key: ti for ti in traffic_list}
    peers = await repo.list_peers_for_server(session, server_id)
    now_ts = datetime.now(timezone.utc).timestamp()

    lines: list[str] = [f"📊 <b>Трафик — {server.name}</b>\n"]

    if not peers:
        lines.append("Пиров нет.")
    else:
        for peer in peers:
            icon = "✅" if peer.status == PeerStatus.ACTIVE else "🚫"
            ti = traffic_map.get(peer.public_key)

            if ti is None:
                # peer добавлен в БД, но awg его не видит (маловероятно)
                detail = "  нет данных от awg"
            elif ti.last_handshake_ts == 0:
                detail = "  никогда не подключался"
            else:
                delta = int(now_ts - ti.last_handshake_ts)
                if delta < 60:
                    ago = f"{delta} сек"
                elif delta < 3600:
                    ago = f"{delta // 60} мин"
                elif delta < 86400:
                    ago = f"{delta // 3600} ч"
                else:
                    ago = f"{delta // 86400} д"
                # rx сервера = upload пира; tx сервера = download пира
                detail = (
                    f"  ↓ {amnezia.fmt_bytes(ti.tx_bytes)}"
                    f"  ↑ {amnezia.fmt_bytes(ti.rx_bytes)}"
                    f"  🕐 {ago} назад"
                )

            # Накопленный трафик (persisted планировщиком) + ещё не зачтённая
            # текущая дельта — переживает сброс счётчика awg при ребуте.
            acc = peer.traffic_used_bytes
            if ti is not None:
                extra = (ti.rx_bytes + ti.tx_bytes) - peer.traffic_last_raw_bytes
                if extra > 0:
                    acc += extra
            sigma = f"\n  Σ {amnezia.fmt_bytes(acc)}"
            if peer.traffic_limit_bytes:
                sigma += f" / {amnezia.fmt_bytes(peer.traffic_limit_bytes)}"

            lines.append(
                f"{icon} <b>{peer.label}</b> • <code>{peer.ip}</code>\n{detail}{sigma}"
            )

    # Пиры на сервере, о которых БД ничего не знает (ручное добавление и т.п.)
    known_keys = {p.public_key for p in peers}
    orphans = [ti for ti in traffic_list if ti.public_key not in known_keys]
    if orphans:
        lines.append("\n⚠️ <i>Пиры вне БД:</i>")
        for ti in orphans:
            lines.append(f"  <code>{ti.public_key[:24]}…</code>")

    await call.message.edit_text(
        "\n".join(lines), reply_markup=traffic_nav(server_id, has_orphans=bool(orphans))
    )

# --- Состояние сервера -------------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:stats:"))
async def cb_server_stats(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Собираю метрики...")

    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            stats = await amnezia.get_server_stats(ssh)
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        return

    ram_pct  = round(stats.ram_used_mb  / stats.ram_total_mb  * 100) if stats.ram_total_mb  else 0
    disk_pct = round(stats.disk_used_gb / stats.disk_total_gb * 100) if stats.disk_total_gb else 0

    text = (
        f"🖥 <b>Состояние — {server.name}</b>\n\n"
        f"⏱ <b>Uptime:</b> {stats.uptime}\n"
        f"📈 <b>Load avg:</b> {stats.load_1:.2f} / {stats.load_5:.2f} / {stats.load_15:.2f}"
        f"  ({stats.cpu_count} CPU)\n"
        f"🧠 <b>RAM:</b> {stats.ram_used_mb} / {stats.ram_total_mb} MB  ({ram_pct}%)\n"
        f"💾 <b>Диск (/):</b> {stats.disk_used_gb:.1f} / {stats.disk_total_gb:.1f} GB  ({disk_pct}%)"
    )
    await call.message.edit_text(text, reply_markup=stats_nav(server_id))


# --- Очистка лишних WG-пиров -------------------------------------------------

@router.callback_query(F.data.startswith(f"{CB_SERVERS}:cleanup:"))
async def cb_server_cleanup(call: CallbackQuery, session: AsyncSession) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Чищу...")
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            traffic_list = await amnezia.get_peer_traffic(ssh)
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=server_card(server.id, server.wdtt_enabled, server.is_private),
        )
        return

    peers = await repo.list_peers_for_server(session, server_id)
    known_keys = {p.public_key for p in peers}
    orphans = [ti for ti in traffic_list if ti.public_key not in known_keys]

    if not orphans:
        await call.message.edit_text(
            "✅ Лишних пиров нет.", reply_markup=traffic_nav(server_id)
        )
        return

    removed, failed = 0, 0
    try:
        async with SSHClient(repo.creds_from_server(server)) as ssh:
            for ti in orphans:
                try:
                    await amnezia.remove_peer_on_server(ssh, public_key=ti.public_key)
                    removed += 1
                except SSHError:
                    failed += 1
    except SSHError as exc:
        await call.message.edit_text(
            f"❌ SSH-ошибка: <code>{exc}</code>",
            reply_markup=traffic_nav(server_id),
        )
        return

    result = f"🧹 Удалено лишних пиров: <b>{removed}</b>"
    if failed:
        result += f"\n⚠️ Не удалось: {failed}"
    await call.message.edit_text(result, reply_markup=traffic_nav(server_id))
