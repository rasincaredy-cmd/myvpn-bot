"""Проверка, что бот может зайти на сервер его же кредами.

    python scripts/check_server_ssh.py <server_id>
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import repo
from bot.db.base import session_scope
from bot.db.repo import creds_from_server
from bot.services.ssh import SSHClient, SSHCredentials, SSHError


def build_credentials(server) -> SSHCredentials:
    """Тонкая обёртка над боевой сборкой кредов (`repo.creds_from_server`,
    которой пользуется бот на каждом SSH-подключении) — чтобы будущие правки
    боевой сборки (новые поля, другой источник) подхватывались тут сами, а
    не расходились с этим скриптом молча.

    Единственное сознательное отличие от боевой сборки — ниже: боевая кладёт
    в SSHCredentials И ключ, И пароль одновременно, и asyncssh при отказе
    ключа молча откатывается на пароль — бот всё равно подключится. А у
    ЭТОГО скрипта задача ровно одна: ответить «можно ли уже выключать
    пароль». Поэтому здесь сценарий «ключ битый, пароль ещё рабочий» обязан
    дать ошибку, а не OK — иначе можно выключить пароль и потерять сервер.
    """
    creds = creds_from_server(server)
    if creds.private_key:
        creds = replace(creds, password=None)  # см. докстринг: строгая проверка ключа
    return creds


async def main(server_id: int) -> int:
    async with session_scope() as session:
        server = await repo.get_server(session, server_id)
    if server is None:
        print(f"сервера с id={server_id} нет в базе")
        return 1

    try:
        creds = build_credentials(server)
    except RuntimeError as exc:
        # decrypt() кидает RuntimeError на битом шифртексте/чужом ENCRYPTION_KEY —
        # это ровно тот случай, ради которого скрипт запускают перед опасным
        # шагом, поэтому голый трейсбек тут не годится.
        print(f"ОШИБКА {server.host}: не удалось расшифровать креды: {exc}")
        return 1

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
