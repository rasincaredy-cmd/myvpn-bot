"""Валидаторы пользовательского ввода."""
from __future__ import annotations

import pytest

from bot.utils.validators import (
    is_valid_host,
    is_valid_label,
    is_valid_port,
    is_valid_server_name,
    is_valid_ssh_user,
)


class TestIsValidHost:
    @pytest.mark.parametrize("v", [
        "1.2.3.4",
        "192.168.0.1",
        "8.8.8.8",
        "::1",
        "2001:db8::1",
        "example.com",
        "my-vps.example.org",
        "a.b.c.d.e",
    ])
    def test_valid(self, v: str) -> None:
        assert is_valid_host(v) is True

    @pytest.mark.parametrize("v", [
        "",
        "   ",
        "-invalid.com",
        "host..name",
        "no_underscores_allowed.com",
        "x" * 300,
    ])
    def test_invalid(self, v: str) -> None:
        assert is_valid_host(v) is False


class TestIsValidPort:
    @pytest.mark.parametrize("v,expected", [
        ("22", 22),
        ("65535", 65535),
        ("1", 1),
        (" 8080 ", 8080),
    ])
    def test_valid(self, v: str, expected: int) -> None:
        assert is_valid_port(v) == expected

    @pytest.mark.parametrize("v", ["0", "65536", "-1", "abc", "", "1.5"])
    def test_invalid(self, v: str) -> None:
        assert is_valid_port(v) is None


class TestIsValidServerName:
    @pytest.mark.parametrize("v", ["de-fra-1", "srv01", "my_vpn", "A1"])
    def test_valid(self, v: str) -> None:
        assert is_valid_server_name(v) is True

    @pytest.mark.parametrize("v", [
        "",            # пусто
        "x",           # слишком коротко (минимум 2)
        "-bad",        # дефис вначале
        "x" * 33,      # слишком длинно
        "with space",  # пробел запрещён
        "with.dot",    # точка запрещена
    ])
    def test_invalid(self, v: str) -> None:
        assert is_valid_server_name(v) is False


class TestIsValidLabel:
    @pytest.mark.parametrize("v", ["my phone", "vasya", "iPhone_13", "p1"])
    def test_valid(self, v: str) -> None:
        assert is_valid_label(v) is True

    @pytest.mark.parametrize("v", ["", " leading-space", "with/slash", "x" * 33])
    def test_invalid(self, v: str) -> None:
        assert is_valid_label(v) is False


class TestIsValidSshUser:
    @pytest.mark.parametrize("v", ["root", "ubuntu", "deploy", "_local", "user-1"])
    def test_valid(self, v: str) -> None:
        assert is_valid_ssh_user(v) is True

    @pytest.mark.parametrize("v", [
        "",
        "Root",          # с большой не пускаем
        "1user",         # начинается с цифры
        "very-long-user-name-much-longer-than-thirtytwo-chars",
        "with space",
        "пользователь",
    ])
    def test_invalid(self, v: str) -> None:
        assert is_valid_ssh_user(v) is False
