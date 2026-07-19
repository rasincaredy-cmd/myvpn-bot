"""Блок «Бэкап»: шифрованный офсайт-бэкап БД и .env в Telegram админам.

Зачем: в SQLite лежат ДЕНЬГИ (балансы, журнал balance_txs), все юзеры и
приватники пиров; в .env — Fernet-ключ, без которого приватники не расшифровать.
Смерть VPS без бэкапа = потеря денежного учёта перед платящими юзерами.

Схема: горячая копия SQLite штатным `Connection.backup()` (без остановки бота,
консистентно даже под записью) + .env → tar в памяти → шифрование паролем
(PBKDF2-SHA256 600k итераций → Fernet) → документ в чат каждому админу.
Telegram хранит файлы неограниченно — это и есть офсайт-хранилище, бесплатно.

Пароль (BACKUP_PASSWORD в .env) должен храниться и ВНЕ VPS (менеджер паролей):
без него бэкап — мусор, с ним — полный доступ ко всему сервису. Пусто =
фича выключена (warning в лог при старте планировщика).

Расшифровка: scripts/restore_backup.py (автономный, без запуска бота).
ФОРМАТ ФАЙЛА ДОЛЖЕН СОВПАДАТЬ с restore-скриптом: MAGIC + salt(16) + Fernet-токен;
синхронность форматов сторожит tests/test_backup.py (сервис шифрует —
скрипт расшифровывает).
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger

from bot.config import settings

# Версия формата в магии: поменяли KDF/структуру — новая магия, restore-скрипт
# учит обе. 16 байт соли, дальше — Fernet-токен целиком.
MAGIC = b"MYVPNBK1"
_SALT_LEN = 16
_KDF_ITERATIONS = 600_000

_ENV_FILE = Path(".env")
_MARKER_FILE = settings.data_dir / "last_backup_date.txt"


def enabled() -> bool:
    return bool(settings.backup_password)


def _db_path() -> Path:
    """Путь к файлу SQLite из db_url ('sqlite+aiosqlite:///./data/x.sqlite3')."""
    return Path(settings.db_url.rsplit("///", 1)[-1])


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_blob(data: bytes, password: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    token = Fernet(_derive_key(password, salt)).encrypt(data)
    return MAGIC + salt + token


def _make_tar(db_path: Path, env_path: Path) -> bytes:
    """Tar в памяти: консистентная копия БД (Connection.backup) + .env."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # Горячая копия БД во временный файл рядом (одна ФС, потом удаляем).
        tmp = db_path.with_suffix(db_path.suffix + ".bktmp")
        try:
            src = sqlite3.connect(db_path)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            tar.add(tmp, arcname=db_path.name)
        finally:
            tmp.unlink(missing_ok=True)
        if env_path.exists():
            tar.add(env_path, arcname=env_path.name)
        else:  # бэкап без .env почти бесполезен (Fernet-ключ!) — но лучше, чем ничего
            logger.warning("Backup: {} не найден, архив только с БД", env_path)
    return buf.getvalue()


def make_backup_bytes(
    *,
    db_path: Path | None = None,
    env_path: Path | None = None,
    password: str | None = None,
) -> tuple[str, bytes]:
    """(имя файла, зашифрованный архив). Синхронная — звать через to_thread."""
    raw = _make_tar(db_path or _db_path(), env_path or _ENV_FILE)
    blob = encrypt_blob(raw, password or settings.backup_password)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"myvpn-backup-{stamp}.tar.enc", blob


async def send_backup_to_admins() -> str:
    """Собирает бэкап и шлёт документом всем ADMIN_IDS. Возвращает имя файла."""
    from aiogram.types import BufferedInputFile

    from bot.loader import bot

    filename, blob = await asyncio.to_thread(make_backup_bytes)
    caption = (
        f"📦 <code>{filename}</code>\n"
        "БД + .env, зашифровано BACKUP_PASSWORD.\n"
        "Восстановление: <code>python scripts/restore_backup.py</code> "
        "(см. README)."
    )
    sent = False
    for admin_id in settings.admin_ids:
        try:
            await bot.send_document(
                admin_id,
                document=BufferedInputFile(blob, filename=filename),
                caption=caption,
            )
            sent = True
        except Exception as exc:  # один недоступный админ не отменяет остальных
            logger.warning("Backup send to admin {} failed: {}", admin_id, exc)
    if not sent:
        raise RuntimeError("бэкап не доставлен ни одному админу")
    return filename


# ── Ночной триггер (для планировщика) ────────────────────────────────────────
# Маркер-файл с датой последнего бэкапа переживает рестарты бота: после ребута
# не шлём дубль и не пропускаем день. Часовой пояс — UTC, как весь планировщик.


def nightly_due(now: datetime) -> bool:
    if not enabled() or now.hour < settings.backup_hour_utc:
        return False
    try:
        return _MARKER_FILE.read_text().strip() != now.strftime("%Y-%m-%d")
    except FileNotFoundError:
        return True


def mark_done(now: datetime) -> None:
    _MARKER_FILE.write_text(now.strftime("%Y-%m-%d"))
