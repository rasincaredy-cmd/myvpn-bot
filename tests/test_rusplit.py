"""Тесты Блока «RU напрямую»: инверсия RU-подсетей для AllowedIPs.

Ключевые инварианты: при дефолтном пороге /24 ВЕСЬ RU-реестр идёт напрямую
(включая mail.ru/reg.ru в мелких блоках — иначе полрунета шло бы через VPN);
весь остальной мир — через туннель; приватные сети всегда мимо туннеля (LAN
юзера работает); инверсия без дыр и без пересечений.
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

    def test_every_ru_block_direct(self) -> None:
        """При дефолтном /24 КАЖДАЯ сеть из RU-реестра идёт напрямую, не в туннель.
        Регресс на баг: с порогом /18 mail.ru/reg.ru (мелкие блоки) шли через VPN."""
        assert settings.ru_direct_max_prefixlen == 32
        allowed = _allowed_nets()
        for net in rusplit._load_ru_networks():
            assert not _covers(allowed, str(net.network_address)), net

    def test_world_tunneled_lan_direct(self) -> None:
        allowed = _allowed_nets()
        assert _covers(allowed, "8.8.8.8")          # мир — через VPN
        assert _covers(allowed, "1.1.1.1")          # DNS — через VPN
        assert not _covers(allowed, "192.168.1.1")  # LAN — напрямую
        assert not _covers(allowed, "10.8.0.1")     # и наша WG-подсеть

    def test_size_within_telegram_file(self) -> None:
        """Полное покрытие — тяжёлый конфиг, но раздаём файлом: должен влезать
        в разумный .conf (Telegram-файл до 2ГБ, WireGuard тянет ~20k маршрутов)."""
        line = rusplit.allowed_ips_no_ru()
        routes = line.count(",") + 1
        assert 15_000 < routes < 30_000
        assert len(line) < 500_000
