"""Раздельное туннелирование «RU напрямую» (Блок «RU напрямую»).

Идея: в туннель заворачивается «весь мир минус российские подсети» — RU-трафик
идёт напрямую (свой IP, локальная скорость, RU-сервисы не видят иностранца),
остальное через VPN. Реализовано на уровне WireGuard AllowedIPs, поэтому
работает в любом клиенте, не только в Amnezia.

Компромисс размера: полная инверсия RU — ~21 000 маршрутов (~350 КБ конфиг,
iOS подключается вечность). Поэтому напрямую пускаем только КРУПНЫЕ RU-блоки
(prefixlen <= RU_DIRECT_MAX_PREFIXLEN, по умолчанию /18 — все крупные
провайдеры и сервисы, ~66% адресного пространства RU). Мелкие RU-подсети
уходят в туннель — это безопасное направление ошибки: заблокированный сайт
никогда не окажется «напрямую» и не отвалится, просто чуть больше трафика
поедет через VPN.

Такой конфиг раздаётся ТОЛЬКО .conf-файлом: ~2 100 маршрутов (~30 КБ) не
влезают ни в QR (~3 КБ максимум), ни в vpn://-ссылку (лимит сообщения 4096).

Список RU-подсетей — снапшот ipdeny.com (agregated) в bot/assets/ru_networks.txt;
обновляется редеплоем (curl https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone).
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

from loguru import logger

from bot.config import settings

_RU_FILE = Path(__file__).resolve().parent.parent / "assets" / "ru_networks.txt"

# Приватные/служебные сети — тоже мимо туннеля: LAN юзера (роутер, принтер,
# телевизор) продолжает работать при подключённом VPN.
_PRIVATE = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "127.0.0.0/8", "100.64.0.0/10", "224.0.0.0/4", "240.0.0.0/4",
)


def _load_ru_networks() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    for line in _RU_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            nets.append(ipaddress.IPv4Network(line))
        except ValueError:
            logger.warning("rusplit: пропущена кривая строка {!r}", line)
    return nets


def _invert(direct: list[ipaddress.IPv4Network]) -> list[ipaddress.IPv4Network]:
    """Дополнение списка сетей до всего IPv4-пространства (то, что идёт в туннель)."""
    allowed: list[ipaddress.IPv4Network] = []
    prev_end = 0
    for net in sorted(
        ipaddress.collapse_addresses(direct), key=lambda n: int(n.network_address)
    ):
        start = int(net.network_address)
        if start > prev_end:
            allowed.extend(ipaddress.summarize_address_range(
                ipaddress.IPv4Address(prev_end), ipaddress.IPv4Address(start - 1)
            ))
        prev_end = max(prev_end, int(net.broadcast_address) + 1)
    if prev_end <= 0xFFFFFFFF:
        allowed.extend(ipaddress.summarize_address_range(
            ipaddress.IPv4Address(prev_end), ipaddress.IPv4Address(0xFFFFFFFF)
        ))
    return allowed


@lru_cache(maxsize=1)
def allowed_ips_no_ru() -> str:
    """Строка AllowedIPs «всё, кроме крупных RU-блоков и приватных сетей».

    Считается один раз за процесс (lru_cache): список статичен до рестарта."""
    ru = [
        n for n in _load_ru_networks()
        if n.prefixlen <= settings.ru_direct_max_prefixlen
    ]
    direct = ru + [ipaddress.IPv4Network(p) for p in _PRIVATE]
    allowed = _invert(direct)
    logger.info(
        "rusplit: {} RU-блоков (<=/{}) напрямую, {} маршрутов в туннель",
        len(ru), settings.ru_direct_max_prefixlen, len(allowed),
    )
    return ", ".join(map(str, allowed))
