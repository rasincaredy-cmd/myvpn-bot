"""Генерация нативной ссылки AmneziaVPN `vpn://` для one-tap импорта с именем.

Голый WireGuard/AmneziaWG `.conf` не несёт имени → приложение показывает «Сервер 1».
Нативный формат AmneziaVPN держит поле `description` (имя) + сам конфиг, и импортится
одним тапом. Формат: JSON → qCompress (Qt: 4 байта BE длины + zlib) → base64 urlsafe →
префикс `vpn://`.

ВАЖНО (выяснено тестом на телефоне): при подключении клиент строит туннель НЕ из
текста конфига, а из структурных полей внутри `last_config` (client_priv_key,
client_ip, server_pub_key, port, Jc..H4). Кладём и текст, и все поля — их парсим
из самого `.conf` (единственный источник правды), client_pub_key выводим из
приватного ключа (X25519)."""
from __future__ import annotations

import base64
import json
import struct
import zlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# Параметры обфускации AmneziaWG — дублируются на всех уровнях нативного формата.
_AWG_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4")


def _qcompress(data: bytes, level: int = 8) -> bytes:
    """Повторяет Qt qCompress: 4 байта big-endian несжатой длины + zlib."""
    return struct.pack(">I", len(data)) + zlib.compress(data, level)


def _quncompress(blob: bytes) -> bytes:
    """Обратное к _qcompress (для тестов/декода): пропустить 4 байта длины + zlib."""
    return zlib.decompress(blob[4:])


def _parse_conf(conf: str) -> dict[str, str]:
    """Плоский парс `key = value` из .conf (ключи Interface/Peer у нас не пересекаются)."""
    out: dict[str, str] = {}
    for line in conf.splitlines():
        line = line.strip()
        if not line or line.startswith(("[", "#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _derive_pub_key(priv_b64: str) -> str:
    """Публичный ключ WireGuard из приватного (X25519, base64)."""
    try:
        priv = X25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
        raw = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode("ascii")
    except Exception:  # noqa: BLE001 — некритично: клиент этим полем не подключается
        return ""


def build_vpn_link(
    *,
    conf: str,
    name: str,
    host: str | None = None,
    port: int | None = None,
    dns1: str | None = None,
    dns2: str | None = None,
) -> str:
    """Собирает `vpn://`-ссылку для AmneziaWG-конфига `conf` с именем `name`.

    host/port/dns — фолбэки; первичный источник — сам `.conf` (Endpoint/DNS)."""
    fields = _parse_conf(conf)

    endpoint = fields.get("Endpoint", "")
    ep_host, _, ep_port = endpoint.rpartition(":")
    host = ep_host or host or ""
    port = int(ep_port) if ep_port.isdigit() else (port or 0)

    dns_parts = [p.strip() for p in fields.get("DNS", "").split(",") if p.strip()]
    dns1 = dns1 or (dns_parts[0] if dns_parts else "1.1.1.1")
    dns2 = dns2 or (dns_parts[1] if len(dns_parts) > 1 else "1.0.0.1")

    client_priv = fields.get("PrivateKey", "")
    client_ip = fields.get("Address", "").split("/")[0]
    awg_params = {k: str(fields[k]) for k in _AWG_KEYS if k in fields}

    last_config: dict = {
        "config": conf,
        "hostName": host,
        "port": port,
        "client_ip": client_ip,
        "client_priv_key": client_priv,
        "client_pub_key": _derive_pub_key(client_priv),
        "server_pub_key": fields.get("PublicKey", ""),
        "persistent_keep_alive": fields.get("PersistentKeepalive", "25"),
        "allowed_ips": [
            p.strip() for p in fields.get("AllowedIPs", "0.0.0.0/0").split(",") if p.strip()
        ],
        "mtu": fields.get("MTU", "1280"),
        **awg_params,
    }
    if fields.get("PresharedKey"):
        last_config["psk_key"] = fields["PresharedKey"]

    payload = {
        "containers": [
            {
                "container": "amnezia-awg",
                "awg": {
                    "isThirdPartyConfig": True,
                    "last_config": json.dumps(last_config, ensure_ascii=False),
                    "port": str(port),
                    "transport_proto": "udp",
                    **awg_params,
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
