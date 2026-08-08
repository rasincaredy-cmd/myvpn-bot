"""Проверка, что бот может зайти на сервер его же кредами.

    python scripts/check_server_ssh.py <server_id>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import repo
from bot.db.base import session_scope
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHCredentials, SSHError


def build_credentials(server) -> SSHCredentials:
    """Ключ приоритетнее пароля — так же, как это делает бот."""
    key = decrypt(server.ssh_key_enc)
    password = decrypt(server.ssh_password_enc)
    return SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        password=None if key else password,
        private_key=key,
        key_passphrase=decrypt(server.ssh_key_passphrase_enc),
    )


async def main(server_id: int) -> int:
    async with session_scope() as session:
        server = await repo.get_server(session, server_id)
    if server is None:
        print(f"сервера с id={server_id} нет в базе")
        return 1

    creds = build_credentials(server)
    how = "ключу" if creds.private_key else "паролю"
    try:
        async with SSHClient(creds) as ssh:
            result = await ssh.run("echo alive")
    except (SSHError, OSError) as exc:
        print(f"ОШИБКА {server.host}: не удалось подключиться по {how}: {exc}")
        return 1
    if not result.ok or "alive" not in result.stdout:
        print(f"ОШИБКА {server.host}: подключились, но команда не отработала")
        return 1
    print(f"OK {server.host}: вход по {how}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("использование: python scripts/check_server_ssh.py <server_id>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(int(sys.argv[1]))))
