"""Генерация нативной ссылки AmneziaVPN `vpn://` для one-tap импорта с именем.

Голый WireGuard/AmneziaWG `.conf` не несёт имени → приложение показывает «Сервер 1».
Нативный формат AmneziaVPN держит поле `description` (имя) + сам конфиг, и импортится
одним тапом. Формат: JSON → qCompress (Qt: 4 байта BE длины + zlib) → base64 urlsafe →
префикс `vpn://`.

⚠️ Точную схему `amnezia-awg` знает только приложение — проверять на телефоне.
Кодек (qCompress/base64) детерминирован и покрыт round-trip тестом; если импорт не
взлетит — правим только формат JSON, не обёртку."""
from __future__ import annotations

import base64
import json
import struct
import zlib


def _qcompress(data: bytes, level: int = 8) -> bytes:
    """Повторяет Qt qCompress: 4 байта big-endian несжатой длины + zlib."""
    return struct.pack(">I", len(data)) + zlib.compress(data, level)


def _quncompress(blob: bytes) -> bytes:
    """Обратное к _qcompress (для тестов/декода): пропустить 4 байта длины + zlib."""
    return zlib.decompress(blob[4:])


def build_vpn_link(
    *,
    conf: str,
    name: str,
    host: str,
    port: int,
    dns1: str = "1.1.1.1",
    dns2: str = "1.0.0.1",
) -> str:
    """Собирает `vpn://`-ссылку для AmneziaWG-конфига `conf` с именем `name`."""
    last_config = json.dumps({"config": conf}, ensure_ascii=False)
    payload = {
        "containers": [
            {
                "container": "amnezia-awg",
                "awg": {
                    "last_config": last_config,
                    "isThirdPartyConfig": True,
                    "port": str(port),
                    "transport_proto": "udp",
                },
            }
        ],
        "defaultContainer": "amnezia-awg",
        "description": name,
        "hostName": host,
        "dns1": dns1,
        "dns2": dns2,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    packed = _qcompress(raw)
    b64 = base64.urlsafe_b64encode(packed).decode("ascii")
    return "vpn://" + b64


def decode_vpn_link(link: str) -> dict:
    """Декод обратно в JSON (для тестов/отладки)."""
    b64 = link[len("vpn://"):]
    b64 += "=" * (-len(b64) % 4)  # добить паддинг
    packed = base64.urlsafe_b64decode(b64)
    return json.loads(_quncompress(packed).decode("utf-8"))
