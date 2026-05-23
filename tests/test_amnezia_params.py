"""Тесты параметров обфускации AmneziaWG и хелперов peer-конфига."""
from __future__ import annotations

import json
import re

import pytest

from bot.services.amnezia import (
    AmneziaParams,
    build_peer_conf,
    generate_amnezia_params,
    next_free_ip,
)
from bot.services.amnezia import _FORBIDDEN_H


class TestGenerateAmneziaParams:
    def test_basic_ranges(self) -> None:
        p = generate_amnezia_params()
        assert 3 <= p.Jc <= 10
        assert 40 <= p.Jmin <= 70
        assert p.Jmax > p.Jmin
        assert p.Jmax - p.Jmin >= 10
        assert 15 <= p.S1 <= 150
        assert 15 <= p.S2 <= 150

    def test_h_values_unique_and_not_forbidden(self) -> None:
        """H1..H4 не могут совпадать с дефолтными WG magic 1..4 и друг с другом —
        иначе обфускация ломается."""
        for _ in range(50):  # генерация рандомная, проверяем многократно
            p = generate_amnezia_params()
            hs = {p.H1, p.H2, p.H3, p.H4}
            assert len(hs) == 4, "H1..H4 должны быть различны"
            assert hs.isdisjoint(_FORBIDDEN_H), "H1..H4 не должны быть из {1,2,3,4}"
            for h in hs:
                assert h >= 5

    def test_serializable(self) -> None:
        p = generate_amnezia_params()
        raw = p.to_json()
        # json.loads должен работать — то есть это валидный JSON, не repr().
        data = json.loads(raw)
        assert set(data.keys()) == {"Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"}
        restored = AmneziaParams.from_json(raw)
        assert restored == p

    def test_interface_block_format(self) -> None:
        p = AmneziaParams(Jc=5, Jmin=50, Jmax=100, S1=50, S2=80, H1=10, H2=20, H3=30, H4=40)
        block = p.to_interface_block()
        assert "Jc = 5" in block
        assert "Jmin = 50" in block
        assert "H1 = 10" in block
        assert "H4 = 40" in block
        # каждая строка — ключ-значение, заканчивается переводом строки
        assert block.endswith("\n")


class TestNextFreeIp:
    def test_first_free_after_gateway(self) -> None:
        # .1 — занят сервером, ожидаем .2 как первый свободный
        assert next_free_ip("10.8.0.0/24", used={"10.8.0.1"}) == "10.8.0.2"

    def test_skips_used(self) -> None:
        used = {"10.8.0.1", "10.8.0.2", "10.8.0.3"}
        assert next_free_ip("10.8.0.0/24", used) == "10.8.0.4"

    def test_finds_hole(self) -> None:
        """Если .2 свободен, а .3 занят — вернуть .2."""
        used = {"10.8.0.1", "10.8.0.3", "10.8.0.4"}
        assert next_free_ip("10.8.0.0/24", used) == "10.8.0.2"

    def test_exhausted_subnet_raises(self) -> None:
        from bot.services.ssh import SSHError

        used = {f"10.8.0.{i}" for i in range(1, 255)}
        with pytest.raises(SSHError):
            next_free_ip("10.8.0.0/24", used)


class TestBuildPeerConf:
    def test_contains_required_fields(self) -> None:
        params = AmneziaParams(
            Jc=4, Jmin=50, Jmax=80, S1=40, S2=60, H1=10, H2=20, H3=30, H4=40
        )
        conf = build_peer_conf(
            peer_private_key="PRIVKEY",
            peer_ip="10.8.0.5",
            server_public_key="SERVERPUB",
            endpoint="1.2.3.4:585",
            params=params,
        )
        # Структура двухсекционная.
        assert "[Interface]" in conf
        assert "[Peer]" in conf
        # Поля интерфейса.
        assert "PrivateKey = PRIVKEY" in conf
        assert "Address = 10.8.0.5/32" in conf
        assert "DNS =" in conf
        # Параметры обфускации.
        assert "Jc = 4" in conf
        assert "H1 = 10" in conf
        # Секция peer.
        assert "PublicKey = SERVERPUB" in conf
        assert "Endpoint = 1.2.3.4:585" in conf
        assert "AllowedIPs = 0.0.0.0/0" in conf
        assert "PersistentKeepalive = 25" in conf

    def test_interface_comes_before_peer(self) -> None:
        """Парсеры wg-quick требуют [Interface] перед [Peer]."""
        params = generate_amnezia_params()
        conf = build_peer_conf(
            peer_private_key="x",
            peer_ip="10.8.0.5",
            server_public_key="y",
            endpoint="1.2.3.4:585",
            params=params,
        )
        assert conf.index("[Interface]") < conf.index("[Peer]")

    def test_no_forbidden_default_dns(self) -> None:
        """DNS-по-умолчанию явный, не пустой — иначе клиент получит DNS-leak."""
        params = generate_amnezia_params()
        conf = build_peer_conf(
            peer_private_key="x",
            peer_ip="10.8.0.5",
            server_public_key="y",
            endpoint="1.2.3.4:585",
            params=params,
        )
        m = re.search(r"DNS\s*=\s*(.+)", conf)
        assert m is not None
        assert m.group(1).strip()
