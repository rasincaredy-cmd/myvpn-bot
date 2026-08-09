"""Положить приватный ssh-ключ сервера в базу бота (зашифрованным).

    python scripts/set_server_key.py <server_id> <путь_к_приватному_ключу>

Пароль намеренно НЕ трогается: он остаётся рабочим, пока вход по ключу
не доказан. Гасит пароль отдельный шаг плана — после того, как
`check_server_ssh.py` подтвердит, что бот заходит именно ключом.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import repo
from bot.db.base import session_scope
from bot.services.crypto import encrypt


def prepare_update(private_key: str) -> dict:
    """Поля, которые надо записать серверу. Только ключ — и ничего больше."""
    if not private_key or not private_key.strip():
        raise ValueError("пустой ключ")
    return {"ssh_key_enc": encrypt(private_key)}


async def main(server_id: int, key_path: Path) -> int:
    try:
        private_key = key_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"не удалось прочитать файл ключа {key_path}: {exc}")
        return 1

    try:
        values = prepare_update(private_key)
    except ValueError as exc:
        print(f"ключ не годится: {exc}")
        return 1

    async with session_scope() as session:
        server = await repo.get_server(session, server_id)
        if server is None:
            print(f"сервера с id={server_id} нет в базе")
            return 1
        for field, value in values.items():
            setattr(server, field, value)
        host = server.host

    print(f"ключ записан для сервера id={server_id} ({host}); пароль оставлен как был")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("использование: python scripts/set_server_key.py <server_id> <путь_к_ключу>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2]))))
