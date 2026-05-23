from __future__ import annotations

import ipaddress
import re

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")


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
