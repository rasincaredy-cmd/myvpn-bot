"""Страж: из сценария-эталона не должны исчезнуть критичные защиты.

Каждая проверка здесь стоит за конкретной аварией, которая случается,
если соответствующий кусок сценария потерять.
"""
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hardening" / "harden.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists() -> None:
    assert SCRIPT.is_file(), "сценарий-эталон пропал"


def test_forward_policy_accept(text: str) -> None:
    # Без этого ufw дропает форвард: VPN подключается, интернета у клиента нет.
    assert 'DEFAULT_FORWARD_POLICY="ACCEPT"' in text


def test_whitelist_has_vpn_subnets(text: str) -> None:
    # Без белого списка банилка забанит самого бота (он ходит по SSH сам на себя).
    assert "10.8.0.0/24" in text
    assert "10.66.66.0/24" in text


def test_whitelist_has_own_external_ip(text: str) -> None:
    # Собственный адрес сервера вычисляется, а не хардкодится.
    assert "OWN_IP" in text
