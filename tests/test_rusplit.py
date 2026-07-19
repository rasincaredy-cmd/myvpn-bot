"""Тесты Блока «RU напрямую»: инверсия RU-подсетей для AllowedIPs.

Ключевые инварианты: крупные RU-блоки (vk, яндекс, госуслуги) напрямую; мелкие
(mail.ru/сбер/ozon) — через VPN (осознанно: безопасное направление ошибки);
весь остальной мир — через туннель; приватные сети всегда мимо туннеля (LAN
юзера работает); инверсия без дыр и без пересечений; конфиг НЕ раздувается
за проверенный потолок клиента Amnezia (эмпирика 19.07.2026: 8496 маршрутов —
туннель не стартует, 21643 — краш при импорте; работает 2141, см. rusplit.py).
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

    def test_key_ru_services_direct(self) -> None:
        """Крупные RU-сервисы (блоки <= /18) идут напрямую, не в туннель.
        IP приколочены (снапшот DNS 19.07.2026) — тесты без сети."""
        assert settings.ru_direct_max_prefixlen == 18
        allowed = _allowed_nets()
        anchors = {
            "vk.com": "87.240.132.72",       # 87.240.128.0/18
            "yandex.ru": "5.255.255.77",     # 5.255.192.0/18
            "gosuslugi.ru": "213.59.253.7",  # 213.59.128.0/17
        }
        for host, ip in anchors.items():
            assert not _covers(allowed, ip), f"{host} ({ip}) уехал в туннель"

    def test_small_service_blocks_tunneled(self) -> None:
        """mail.ru/сбер (/21) и ozon (/22) при пороге /18 едут ЧЕРЕЗ VPN —
        осознанная жертва ради стабильности клиента (см. rusplit.py: /21 =
        8496 маршрутов уже не стартует на Android-Amnezia). Если этот тест
        упал из-за поднятия порога — сначала проверь подключение на телефоне."""
        allowed = _allowed_nets()
        tunneled = {
            "mail.ru": "89.221.239.1",        # 89.221.232.0/21
            "sberbank.ru": "84.252.149.206",  # 84.252.144.0/21
            "ozon.ru": "185.73.193.68",       # 185.73.192.0/22
        }
        for host, ip in tunneled.items():
            assert _covers(allowed, ip), f"{host} ({ip}) не в туннеле"

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

    def test_size_amnezia_can_swallow(self) -> None:
        """Регресс на потолок клиента Amnezia (Android, 19.07.2026): 21643
        маршрута — краш при импорте; 8496 — туннель не стартует; 2141 —
        работает. Держимся в проверенном диапазоне."""
        line = rusplit.allowed_ips_no_ru()
        routes = line.count(",") + 1
        assert 1_500 < routes < 4_000
        assert len(line) < 60_000
