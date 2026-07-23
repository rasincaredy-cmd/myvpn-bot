"""Проверка живости внешних ссылок на обход-приложения (wdtt._PLATFORMS).

Ссылки на GitHub-релизы и TestFlight-инвайт живут не у нас: репо переезжают,
бета заполняется (лимит 10k тестеров) — юзер утыкается в тупик, а мы не знаем.
Раз в LINKCHECK_INTERVAL_DAYS дней дёргаем каждую ссылку и алертим админов,
если что-то отвалилось. Всё живо — молчим.

TestFlight-страницы отвечают 200 даже когда бета закрыта, поэтому для них
дополнительно ищем маркеры «beta is full / isn't accepting» в теле страницы.

Маркер-файл с датой последней проверки переживает рестарты (как у бэкапа).
"""
from __future__ import annotations

import re
from datetime import datetime

from loguru import logger

from bot.config import settings

_MARKER_FILE = settings.data_dir / "last_linkcheck_date.txt"

# Браузерный UA: GitHub и Apple отвечают и дефолтному aiohttp-клиенту, но
# нейтральный UA снижает шанс словить бот-фильтр вместо честного статуса.
_UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"

# Маркеры «инвайт живой, но вступить нельзя» на страницах testflight.apple.com.
_TESTFLIGHT_DEAD_MARKERS = (
    "beta is full",
    "isn't accepting any new testers",
    "not accepting any new testers",
    "no longer available",
)


def _collect_urls() -> list[tuple[str, str]]:
    """(платформа, URL) из wdtt._PLATFORMS. Третий элемент кортежа — либо URL,
    либо HTML-инструкция со ссылками внутри (iOS) — выдёргиваем все http(s)."""
    from bot.handlers.wdtt import _PLATFORMS

    pairs: list[tuple[str, str]] = []
    for key, (label, _app, target) in _PLATFORMS.items():
        if not target:
            continue
        for url in re.findall(r"https?://[^\s\"<>]+", target):
            pairs.append((label, url))
    return pairs


async def _check_url(session, url: str) -> str | None:
    """None — ссылка живая; строка — что с ней не так."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                return f"HTTP {resp.status}"
            if "testflight.apple.com" in url:
                body = (await resp.text(errors="ignore")).lower()
                for marker in _TESTFLIGHT_DEAD_MARKERS:
                    if marker in body:
                        return f"страница отвечает, но: «{marker}»"
    except Exception as exc:  # DNS, таймаут, TLS — тоже повод посмотреть руками
        return f"недоступна ({type(exc).__name__}: {exc})"
    return None


async def run_check() -> list[str]:
    """Проверяет все ссылки, возвращает список проблем (пусто = всё живо)."""
    import aiohttp  # транзитивная зависимость aiogram

    problems: list[str] = []
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": _UA}
    ) as http:
        for label, url in _collect_urls():
            issue = await _check_url(http, url)
            if issue:
                problems.append(f"• <b>{label}</b> — {url}\n  {issue}")
                logger.warning("Linkcheck: {} {} -> {}", label, url, issue)
    return problems


async def notify_admins(problems: list[str]) -> None:
    from bot.loader import bot

    text = (
        "🔗 <b>Проверка ссылок на обход-приложения</b>\n\n"
        "Часть ссылок из раздела «Обход БС» не отвечает — юзеры упрутся "
        "в тупик при установке:\n\n" + "\n".join(problems) +
        "\n\nПоправить: <code>_PLATFORMS</code> в <code>bot/handlers/wdtt.py</code>."
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("Linkcheck alert to admin {} failed: {}", admin_id, exc)


# ── Триггер для планировщика (по образцу ночного бэкапа) ─────────────────────

def due(now: datetime) -> bool:
    """Пора ли проверять: прошло ≥ интервала с последней проверки. 0 = выключено.

    Дата в маркере, а не в памяти — рестарт бота не вызывает лишнюю проверку."""
    interval = settings.linkcheck_interval_days
    if interval <= 0:
        return False
    try:
        last = datetime.strptime(_MARKER_FILE.read_text().strip(), "%Y-%m-%d").date()
    except (FileNotFoundError, ValueError):
        return True
    return (now.date() - last).days >= interval


def mark_done(now: datetime) -> None:
    _MARKER_FILE.write_text(now.strftime("%Y-%m-%d"))
