from __future__ import annotations

import ipaddress
import re

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
# Метка (устройство/пир/инвайт): разрешаем кириллицу и латиницу — юзер пишет
# «Ноутбук» или «my-phone». Буквы/цифры/пробел/дефис/подчёркивание, до 32.
_LABEL_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9 _-]{0,31}$")


def is_valid_host(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(value))


def is_valid_port(value: str) -> int | None:
    try:
        port = int(value.strip())
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def is_valid_server_name(value: str) -> bool:
    return bool(_NAME_RE.match(value.strip()))


def is_valid_label(value: str) -> bool:
    # Не глотаем пробелы: хендлер делает .strip() сам перед валидацией.
    return bool(_LABEL_RE.match(value))


def is_valid_ssh_user(value: str) -> bool:
    v = value.strip()
    return bool(v) and v.isascii() and re.match(r"^[a-z_][a-z0-9_-]{0,31}$", v) is not None


import re
from datetime import datetime, timedelta, timezone


def parse_expiry(text: str) -> datetime | None | str:
    """
    Возвращает datetime (UTC), None (сброс, если '-'), или 'invalid'.

    Всё, что вводит админ, читается по МОСКВЕ — так же, как показывается ему и
    юзеру; в базу уходит UTC. Иначе «до 23:00» на экране и в уведомлении юзеру
    разъезжались бы на три часа.

    Форматы (время ЧЧ:ММ — необязательное, в конце):
      Nд | Nd                 → сейчас + N дней, текущее время
      Nд ЧЧ:ММ                → сейчас + N дней, в указанное время МСК
      ДД.ММ.ГГГГ              → на 23:59 МСК
      ДД.ММ.ГГГГ ЧЧ:ММ        → на указанное время МСК
    """
    from bot.utils.timefmt import MSK, msk_to_utc
    text = text.strip()
    if text == "-":
        return None

    # Необязательное время ЧЧ:ММ в конце строки.
    hh_mm: tuple[int, int] | None = None
    m_time = re.search(r"\s+(\d{1,2}):(\d{2})$", text)
    if m_time:
        hh, mm = int(m_time.group(1)), int(m_time.group(2))
        if hh > 23 or mm > 59:
            return "invalid"
        hh_mm = (hh, mm)
        text = text[: m_time.start()].strip()

    # Период: Nд / Nd
    m = re.match(r"^(\d+)[dдDД]$", text, re.IGNORECASE)
    if m:
        dt = datetime.now(timezone.utc) + timedelta(days=int(m.group(1)))
        if hh_mm:
            # Час ставим по Москве: «30д 18:00» — это 18:00 МСК, а не UTC.
            local = dt.astimezone(MSK).replace(
                hour=hh_mm[0], minute=hh_mm[1], second=0, microsecond=0
            )
            dt = local.astimezone(timezone.utc)
        return dt

    # Дата: ДД.ММ.ГГГГ
    try:
        dt = datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        return "invalid"
    if hh_mm:
        return msk_to_utc(dt.replace(hour=hh_mm[0], minute=hh_mm[1], second=0))
    return msk_to_utc(dt.replace(hour=23, minute=59, second=59))


def parse_traffic_limit(text: str) -> int | None | str:
    """
    Возвращает байты, None (сброс, если '-'), или 'invalid'.
    Форматы: 10GB | 500MB | 1TB (и кириллические ГБ/МБ/ТБ)
    """
    text = text.strip()
    if text == "-":
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(GB|MB|TB|ГБ|МБ|ТБ)$", text, re.IGNORECASE)
    if not m:
        return "invalid"
    value = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"MB": 1024**2, "МБ": 1024**2,
            "GB": 1024**3, "ГБ": 1024**3,
            "TB": 1024**4, "ТБ": 1024**4}.get(unit, 1024**3)
    return int(value * mult)

def clean_location(value: str) -> str | None:
    """Название локации из ввода админа или None, если оно не годится.

    Локация — единственный свободный текст, который админ вводит, а видят ВСЕ
    юзеры: витрина «🌍 Локации», подписи к конфигам, блоки со ссылками. Символы
    разметки в ней ломают эти экраны целиком — Telegram просто не принимает
    такое сообщение (аудит 20.08.2026).

    Экранирование на выводе тоже стоит, но это второй рубеж: в базу кривое
    значение попадать не должно вовсе, иначе оно всплывёт в новом экране, где
    экранировать забудут. Эмодзи и флаги разрешены — из них локация и состоит.
    """
    cleaned = " ".join(value.strip().split())
    if not cleaned or len(cleaned) > 64:
        return None
    if any(ch in cleaned for ch in "<>&"):
        return None
    return cleaned
