#!/usr/bin/env python3
"""Восстановление из бэкапа myvpn-bot (см. bot/services/backup.py).

Автономный: НЕ импортирует bot.* (на свежем VPS бот без .env даже не стартует —
а .env как раз внутри бэкапа). Нужен только пакет cryptography:
    pip install cryptography

Использование (на новом VPS, из корня склонированного репо):
    python3 scripts/restore_backup.py myvpn-backup-XXXX.tar.enc
Пароль спросит интерактивно (или возьмёт из env BACKUP_PASSWORD).
Распакует vpn_bot.sqlite3 → ./data/ и .env → ./ (существующие файлы не
перезаписывает без --force). Дальше обычный запуск бота.

ФОРМАТ ФАЙЛА (должен совпадать с bot/services/backup.py, сторожит
tests/test_backup.py): MAGIC(8) + salt(16) + Fernet-токен;
ключ = PBKDF2-SHA256(пароль, salt, 600000 итераций).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import io
import os
import sys
import tarfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"MYVPNBK1"
_SALT_LEN = 16
_KDF_ITERATIONS = 600_000


def decrypt_blob(blob: bytes, password: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise SystemExit("Это не бэкап myvpn-bot (нет магии MYVPNBK1)")
    salt = blob[len(MAGIC):len(MAGIC) + _SALT_LEN]
    token = blob[len(MAGIC) + _SALT_LEN:]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    try:
        return Fernet(key).decrypt(token)
    except InvalidToken:
        raise SystemExit("Неверный пароль (или файл повреждён)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Восстановление бэкапа myvpn-bot")
    ap.add_argument("backup_file", type=Path)
    ap.add_argument("--force", action="store_true",
                    help="перезаписывать существующие файлы")
    args = ap.parse_args()

    password = os.environ.get("BACKUP_PASSWORD") or getpass.getpass("Пароль бэкапа: ")
    raw = decrypt_blob(args.backup_file.read_bytes(), password)

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or "/" in member.name or member.name.startswith("."):
                # В наших бэкапах только плоские имена; .env обрабатываем ниже
                # отдельно от этого фильтра.
                if member.name != ".env":
                    print(f"пропущен неожиданный член архива: {member.name}")
                    continue
            target = Path(".") / member.name if member.name == ".env" \
                else data_dir / member.name
            if target.exists() and not args.force:
                print(f"ЕСТЬ УЖЕ, пропускаю (--force для перезаписи): {target}")
                continue
            src = tar.extractfile(member)
            assert src is not None
            target.write_bytes(src.read())
            print(f"восстановлен: {target}")

    print("Готово. Проверь .env (endpoint'ы, токены) и запускай бота.")


if __name__ == "__main__":
    main()
