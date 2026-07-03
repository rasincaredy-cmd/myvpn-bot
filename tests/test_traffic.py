"""Тесты накопления трафика с защитой от сброса счётчика awg."""
from __future__ import annotations

from bot.services.amnezia import accumulate_traffic, fmt_bytes


class TestAccumulateTraffic:
    def test_first_reading_counts_full(self) -> None:
        # Первый замер: prev_used=0, prev_raw=0 → накоплено = текущее сырое.
        used, raw = accumulate_traffic(0, 0, 500)
        assert used == 500
        assert raw == 500

    def test_normal_increment_adds_delta(self) -> None:
        # Счётчик вырос с 500 до 800 → +300 к накопленному.
        used, raw = accumulate_traffic(500, 500, 800)
        assert used == 800
        assert raw == 800

    def test_counter_reset_counts_from_zero(self) -> None:
        # После ребута awg сбросил счётчик: было 1000, стало 200 (< prev_raw).
        # Дельта отрицательная → считаем 200 как новую порцию поверх накопленного.
        used, raw = accumulate_traffic(1000, 1000, 200)
        assert used == 1200
        assert raw == 200

    def test_no_change_keeps_used(self) -> None:
        used, raw = accumulate_traffic(1000, 1000, 1000)
        assert used == 1000
        assert raw == 1000

    def test_accumulates_across_multiple_resets(self) -> None:
        # Симуляция: рост → сброс → рост.
        used, raw = accumulate_traffic(0, 0, 100)      # +100
        used, raw = accumulate_traffic(used, raw, 300) # +200
        used, raw = accumulate_traffic(used, raw, 50)  # сброс, +50
        used, raw = accumulate_traffic(used, raw, 120) # +70
        assert used == 100 + 200 + 50 + 70
        assert raw == 120


class TestFmtBytes:
    def test_units(self) -> None:
        assert fmt_bytes(0) == "0.0 B"
        assert fmt_bytes(1536) == "1.5 KB"
        assert fmt_bytes(1024 * 1024) == "1.0 MB"
