"""Версии программы резервного подключения на нодах и обновление одной кнопкой.

Раньше обновление обхода было ручной операцией на каждой ноде: залить файл,
перезапустить службу, проверить сокет. Пока нод две — терпимо, но разъезд
версий ниоткуда не виден: «служба active» одинаково выглядит и у свежей
программы, и у полугодовалой. Экран показывает отпечаток на каждой ноде рядом
с эталоном с ноды бота — расхождение видно сразу.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import CB_PANEL, wdtt_nodes_kb
from bot.services import wdtt_update
from bot.services.ssh import SSHClient, SSHError
from bot.texts import ui
from bot.utils.tgtext import TG_TEXT_LIMIT, fit_to_message

router = Router(name="admin_wdtt_nodes")


async def _wdtt_servers(session: AsyncSession):
    """Ноды, на которых резервное подключение включено. Не READY тоже берём:
    именно на них чаще всего и стоит старьё."""
    servers = await repo.list_all_servers(session)
    return [s for s in servers if s.wdtt_enabled]


def _node_line(server, probe: wdtt_update.Probe, ref: str | None) -> str:
    if not probe.installed:
        return f"❌ <b>{ui.safe(server.name)}</b> — программы нет"
    if not probe.socket_ok:
        state = "служба стоит" if not probe.active else "сокет молчит"
        return f"⚠️ <b>{ui.safe(server.name)}</b> — {state} · <code>{probe.short}</code>"
    mark = "✅" if ref and probe.sha256 == ref else "🔸"
    tail = "" if ref and probe.sha256 == ref else " · <i>отличается от эталона</i>"
    accesses = "" if probe.accesses is None else f" · доступов: {probe.accesses}"
    return (
        f"{mark} <b>{ui.safe(server.name)}</b> — <code>{probe.short}</code>"
        f"{accesses}{tail}"
    )


@router.callback_query(F.data == f"{CB_PANEL}:wdtt")
async def cb_wdtt_nodes(call: CallbackQuery, session: AsyncSession) -> None:
    servers = await _wdtt_servers(session)
    if not servers:
        await call.answer("Ни на одной ноде обход не включён", show_alert=True)
        return

    await call.answer("⏳ Опрашиваю ноды...")
    ref = wdtt_update.reference_sha256()
    lines = [
        "⚡ <b>Резервное подключение: версии</b>\n",
        f"Эталон (нода бота): <code>{(ref or '—')[:8]}</code>\n",
    ]
    stale: list[int] = []
    for server in servers:
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                probe = await wdtt_update.probe(ssh)
        except SSHError as exc:
            lines.append(f"🚫 <b>{ui.safe(server.name)}</b> — не отвечает: {ui.safe(exc)}")
            continue
        lines.append(_node_line(server, probe, ref))
        if probe.installed and (ref is None or probe.sha256 != ref or not probe.socket_ok):
            stale.append(server.id)

    help_block = (
        "\n<blockquote expandable>ℹ️ <b>Что делает обновление</b>\n"
        "Заливает на ноду эталонную программу с ноды бота, перезапускает службу "
        "и проверяет, что управляющий сокет отвечает, а доступы на месте. Если "
        "нет — возвращает прежнюю версию из бэкапа.\n"
        "Перезапуск обрывает тех, кто прямо сейчас сидит через резервное "
        "подключение: пароли сохраняются, людям надо переподключиться.\n"
        "Эталон обновляется отдельно — новую программу кладут на ноду бота "
        "в <code>/usr/local/bin/wdtt-server</code>.</blockquote>"
    )
    # Список нод растёт с каждой купленной страной, а сообщение длиннее 4096
    # символов Telegram не обрезает — он его не принимает вовсе. Справку
    # оставляем всегда, режется список (и говорит, что порезан).
    text = fit_to_message(lines, limit=TG_TEXT_LIMIT - len(help_block) - 2) + help_block
    await call.message.edit_text(
        text,
        reply_markup=wdtt_nodes_kb([(s.id, s.name) for s in servers], stale),
    )


@router.callback_query(F.data.startswith(f"{CB_PANEL}:wdttup:"))
async def cb_wdtt_update(call: CallbackQuery, session: AsyncSession) -> None:
    """Обновление одной ноды или всех сразу. Ноды идут по очереди: параллельно
    рвать резервное подключение сразу везде — плохая идея."""
    target = call.data.rsplit(":", 1)[-1]
    servers = await _wdtt_servers(session)
    if target != "all":
        servers = [s for s in servers if s.id == int(target)]
    if not servers:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.answer("⏳ Обновляю...")
    report = ["⚡ <b>Обновление резервного подключения</b>\n"]
    for server in servers:
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                result = await wdtt_update.update(ssh)
        except SSHError as exc:
            report.append(f"🚫 <b>{ui.safe(server.name)}</b> — {ui.safe(exc)}")
            continue
        icon = "✅" if result.ok else ("↩️" if result.rolled_back else "❌")
        report.append(f"{icon} <b>{ui.safe(server.name)}</b> — {ui.safe(result.detail)}")

    report.append("\n<i>Проверь список версий — он покажет, что стоит сейчас.</i>")
    await call.message.edit_text(
        fit_to_message(report),
        reply_markup=wdtt_nodes_kb([(s.id, s.name) for s in await _wdtt_servers(session)], []),
    )
