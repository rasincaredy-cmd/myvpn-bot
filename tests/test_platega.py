"""Тесты Platega: конвертация сумм, выключенность без ключей, счета и зачисление.

Сеть не трогаем: сам HTTP-клиент проверяется живыми запросами руками (см.
спеку), а здесь — денежная логика и то, что бот не сломается от чужих ответов.
"""
from __future__ import annotations

import pytest

from bot.services import platega


class TestAmountConversion:
    def test_whole_rubles(self) -> None:
        assert platega.amount_to_rub(300_00) == 300.0

    def test_kopeks_survive(self) -> None:
        """90.50 ₽ обязаны уехать как 90.5, а не как 90 или 9050."""
        assert platega.amount_to_rub(90_50) == 90.5

    def test_no_float_drift(self) -> None:
        """Копейки считаем целыми и делим один раз — накопленной ошибки быть не может."""
        assert platega.amount_to_rub(10_01) == 10.01
        assert platega.amount_to_rub(1_000_000_00) == 1_000_000.0


class TestEnabled:
    def test_disabled_without_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "")
        monkeypatch.setattr(platega.settings, "platega_secret", "")
        assert platega.enabled() is False

    def test_needs_both_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Один ключ без второго — это не настроенная платёжка, а опечатка в .env."""
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "mid")
        monkeypatch.setattr(platega.settings, "platega_secret", "")
        assert platega.enabled() is False

    def test_enabled_with_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "mid")
        monkeypatch.setattr(platega.settings, "platega_secret", "sec")
        assert platega.enabled() is True
