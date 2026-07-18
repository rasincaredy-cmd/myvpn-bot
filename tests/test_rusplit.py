"""Тесты Блока «RU напрямую»: инверсия RU-подсетей для AllowedIPs.

Ключевые инварианты: направление ошибки — только «лишнее в туннель» (крупные
RU-блоки напрямую, мелкие и весь остальной мир через VPN); приватные сети
всегда мимо туннеля (LAN юзера работает); инверсия без дыр и без пересечений.
"""
from __future__ import annotations

import ipaddress

from bot.config import settings
from bot.services import rusplit


def _allowed_nets() -> list[ipaddress.IPv4Network]:
    rusplit.allowed_ips_no_ru.cache_clear()
    return [
        ipaddress.IPv4Network(p.strip())
        for p in rusplit.allowed_ips_no_ru().split(",")
    ]


def _covers(nets: list[ipaddress.IPv4Network], ip: str) -> bool:
    addr = ipaddress.IPv4Address(ip)
    return any(addr in n for n in nets)


class TestInversion:
    def test_partition_is_exact(self) -> None:
        """Туннель + прямое = всё IPv4-пространство, без пересечений."""
        allowed = _allowed_nets()
        ru_big = [
            n for n in rusplit._load_ru_networks()
            if n.prefixlen <= settings.ru_direct_max_prefixlen
        ]
        direct = ru_big + [ipaddress.IPv4Network(p) for p in rusplit._PRIVATE]
        total = sum(n.num_addresses for n in allowed) + sum(
            n.num_addresses for n in ipaddress.collapse_addresses(direct)
        )
        assert total == 2**32

    def test_big_ru_block_direct(self) -> None:
        """IP из крупного RU-блока НЕ попадает в туннель."""
        allowed = _allowed_nets()
        big = next(
            n for n in rusplit._load_ru_networks()
            if n.prefixlen <= settings.ru_direct_max_prefixlen
        )
        assert not _covers(allowed, str(big.network_address))

    def test_small_ru_block_tunneled(self) -> None:
        """Мелкий RU-блок (> порога) идёт в туннель — безопасное направление
        ошибки: заблокированный сайт не окажется «напрямую»."""
        allowed = _allowed_nets()
        small = next(
            n for n in rusplit._load_ru_networks()
            if n.prefixlen > settings.ru_direct_max_prefixlen
            # сам не внутри крупного RU-блока и не в приватных
            and not any(
                n.subnet_of(b) for b in rusplit._load_ru_networks()
                if b.prefixlen <= settings.ru_direct_max_prefixlen
            )
        )
        assert _covers(allowed, str(small.network_address))

    def test_world_tunneled_lan_direct(self) -> None:
        allowed = _allowed_nets()
        assert _covers(allowed, "8.8.8.8")          # мир — через VPN
        assert _covers(allowed, "1.1.1.1")          # DNS — через VPN
        assert not _covers(allowed, "192.168.1.1")  # LAN — напрямую
        assert not _covers(allowed, "10.8.0.1")     # и наша WG-подсеть

    def test_reasonable_size(self) -> None:
        """Конфиг остаётся подъёмным для мобильных клиентов."""
        line = rusplit.allowed_ips_no_ru()
        assert 500 < line.count(",") + 1 < 5000
        assert len(line) < 100_000
