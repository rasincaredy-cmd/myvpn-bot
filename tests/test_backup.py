"""Тесты Блока «Бэкап».

Ключевой инвариант: то, что зашифровал сервис (bot/services/backup.py),
расшифровывает автономный restore-скрипт (scripts/restore_backup.py) — форматы
продублированы в двух файлах намеренно (restore обязан работать без запуска
бота), и этот тест — единственный сторож их синхронности.
"""
from __future__ import annotations

import importlib.util
import io
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.config import settings
from bot.services import backup


def _load_restore_module():
    """scripts/ — не пакет; грузим скрипт как модуль по пути."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "restore_backup.py"
    spec = importlib.util.spec_from_file_location("restore_backup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_state(tmp_path: Path) -> tuple[Path, Path]:
    """Мини-БД SQLite и .env во временной папке."""
    db = tmp_path / "vpn_bot.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('деньги')")
    conn.commit()
    conn.close()
    env = tmp_path / ".env"
    env.write_text("ENCRYPTION_KEY=super-secret\n")
    return db, env


class TestBackup:
    def test_roundtrip_service_encrypts_script_decrypts(self, fake_state) -> None:
        db, env = fake_state
        filename, blob = backup.make_backup_bytes(
            db_path=db, env_path=env, password="пароль-123"
        )
        assert filename.startswith("myvpn-backup-") and filename.endswith(".tar.enc")

        restore = _load_restore_module()
        raw = restore.decrypt_blob(blob, "пароль-123")
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            names = set(tar.getnames())
            assert names == {"vpn_bot.sqlite3", ".env"}
            env_body = tar.extractfile(".env").read().decode()
            assert "super-secret" in env_body
            # БД в архиве — валидный sqlite с данными (копия горячим .backup)
            db_body = tar.extractfile("vpn_bot.sqlite3").read()
            assert db_body[:16] == b"SQLite format 3\x00"

    def test_wrong_password_rejected(self, fake_state) -> None:
        db, env = fake_state
        _, blob = backup.make_backup_bytes(db_path=db, env_path=env, password="a")
        restore = _load_restore_module()
        with pytest.raises(SystemExit):
            restore.decrypt_blob(blob, "b")

    def test_not_a_backup_rejected(self) -> None:
        restore = _load_restore_module()
        with pytest.raises(SystemExit):
            restore.decrypt_blob(b"garbage-not-magic", "x")

    def test_missing_env_still_backs_up_db(self, fake_state, tmp_path) -> None:
        db, _ = fake_state
        _, blob = backup.make_backup_bytes(
            db_path=db, env_path=tmp_path / "no-such.env", password="p"
        )
        raw = _load_restore_module().decrypt_blob(blob, "p")
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            assert tar.getnames() == ["vpn_bot.sqlite3"]


class TestNightlyTrigger:
    @pytest.fixture(autouse=True)
    def _marker_in_tmp(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(backup, "_MARKER_FILE", tmp_path / "last_backup_date.txt")
        monkeypatch.setattr(settings, "backup_password", "pw")

    def test_due_after_hour_once_a_day(self) -> None:
        early = datetime(2026, 7, 19, settings.backup_hour_utc - 1, 30,
                         tzinfo=timezone.utc)
        late = datetime(2026, 7, 19, settings.backup_hour_utc, 5,
                        tzinfo=timezone.utc)
        assert not backup.nightly_due(early)   # до часа Х — рано
        assert backup.nightly_due(late)        # после — пора
        backup.mark_done(late)
        assert not backup.nightly_due(late)    # сегодня уже слали
        tomorrow = datetime(2026, 7, 20, settings.backup_hour_utc, 5,
                            tzinfo=timezone.utc)
        assert backup.nightly_due(tomorrow)    # завтра — снова пора

    def test_disabled_without_password(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "backup_password", "")
        now = datetime(2026, 7, 19, 23, 0, tzinfo=timezone.utc)
        assert not backup.nightly_due(now)
