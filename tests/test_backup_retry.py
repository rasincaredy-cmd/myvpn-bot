"""Ночной бэкап: провал не должен превращаться в шторм попыток.

Найдено аудитом 20.08.2026. Отметка «сделано» ставится ПОСЛЕ успешной отправки,
и это правильно — иначе сбой съел бы день. Но тик планировщика идёт раз в 5
минут, а `nightly_due` остаётся истинным до полуночи: не ушедший бэкап
пересобирался и переотправлялся ~250 раз за ночь, каждый раз с полной копией
базы и PBKDF2 на 600k итераций.

Реалистичный сценарий — база перерастает лимит Telegram в 50 МБ: тогда отправка
падает у всех админов, и шторм повторяется каждую ночь, молча.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.services import backup as backup_svc


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_svc, "_MARKER_FILE", tmp_path / "last_backup_date.txt")
    monkeypatch.setattr(backup_svc, "_ATTEMPT_FILE", tmp_path / "backup_attempts.txt")
    monkeypatch.setattr(backup_svc.settings, "backup_password", "x")
    monkeypatch.setattr(backup_svc.settings, "backup_hour_utc", 3)
    return tmp_path


NIGHT = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)


class TestRetryBudget:
    def test_due_when_nothing_happened_yet(self, marker_dir) -> None:
        assert backup_svc.nightly_due(NIGHT) is True

    def test_not_due_after_success(self, marker_dir) -> None:
        backup_svc.mark_done(NIGHT)
        assert backup_svc.nightly_due(NIGHT) is False

    def test_still_due_after_one_failure(self, marker_dir) -> None:
        """Разовый сбой (сеть моргнула) обязан ретраиться — иначе потеряем день."""
        backup_svc.mark_attempt(NIGHT)
        assert backup_svc.nightly_due(NIGHT) is True

    def test_gives_up_after_the_budget(self, marker_dir) -> None:
        for _ in range(backup_svc.MAX_ATTEMPTS_PER_DAY):
            backup_svc.mark_attempt(NIGHT)
        assert backup_svc.nightly_due(NIGHT) is False, "шторм попыток не остановлен"

    def test_budget_resets_next_day(self, marker_dir) -> None:
        for _ in range(backup_svc.MAX_ATTEMPTS_PER_DAY):
            backup_svc.mark_attempt(NIGHT)
        tomorrow = NIGHT.replace(day=22)
        assert backup_svc.nightly_due(tomorrow) is True

    def test_success_clears_attempts(self, marker_dir) -> None:
        """После удачи счётчик обнуляется: завтрашний сбой получит полный бюджет."""
        backup_svc.mark_attempt(NIGHT)
        backup_svc.mark_done(NIGHT)
        assert backup_svc.attempts_today(NIGHT) == 0

    def test_first_failure_is_reported(self, marker_dir) -> None:
        """Тревогу шлём один раз за ночь, а не на каждой попытке."""
        assert backup_svc.mark_attempt(NIGHT) is True
        assert backup_svc.mark_attempt(NIGHT) is False
