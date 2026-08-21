"""Слежение за новыми версиями приложения резервного подключения.

Серверная часть у нас теперь чужая (qWDTT) плюс один наш файл сверху, и
обновляется она НЕ сама: надо пересобрать и разослать по нодам. Пока никто не
следит за их релизами, «не отставать» держится на том, что кто-то вспомнил
зайти на страницу проекта — то есть не держится вообще.

Поэтому бот раз в цикл здоровья спрашивает у GitHub последний релиз и, если
номер сменился с прошлого раза, пишет об этом админу. Один раз на версию, не
каждый час: тревога, которая повторяется, перестаёт читаться.

Любая ошибка сети или разбора — молчание. Выдуманная новость хуже пропущенной,
а проверка эта не про аварию.
"""
from __future__ import annotations

import aiohttp
from loguru import logger

# Живой форк оригинального (архивного) проекта: и приложение, и серверная часть.
REPO = "SpaceNeuroX/proxy-turn-vk-android"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"

_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def latest_release() -> str | None:
    """Номер последнего релиза (например «v1.4.2») или None, если не вышло.

    None означает «не знаю», а не «нового нет»: вызывающий обязан промолчать,
    а не делать выводов.
    """
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.get(
                RELEASES_API, headers={"Accept": "application/vnd.github+json"}
            ) as resp:
                if resp.status != 200:
                    logger.debug("upstream: релизы отдали {}", resp.status)
                    return None
                data = await resp.json()
    except Exception as exc:  # сеть, таймаут, мусор в ответе
        logger.debug("upstream: не спросил про релизы: {}", exc)
        return None

    tag = (data or {}).get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None
    return tag.strip()


def release_message(tag: str) -> str:
    """Текст админу. Без указаний «что делать» списком: делать всё равно мне,
    админу нужен факт и ссылка."""
    return (
        f"🆕 <b>Вышла новая версия приложения резервного подключения</b>\n\n"
        f"Версия: <b>{tag}</b>\n"
        f"{RELEASES_URL}\n\n"
        "<i>Серверную часть, скорее всего, тоже нужно пересобрать и разослать "
        "по нодам — напиши мне, соберу и обновлю.</i>"
    )
