"""Приведение сервера к эталону безопасности.

Единственное место, которое знает, как доставить сценарий-эталон на
сервер, запустить его команды в нужном порядке и разобрать результат.
Сам сценарий живёт в репозитории (`scripts/hardening/harden.sh`) — копию
его текста в коде держать нельзя, копии разъезжаются.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

import asyncssh
from loguru import logger

from bot.services.ssh import SSHClient, SSHCredentials, SSHError

SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "hardening" / "harden.sh"
REMOTE_PATH = "/root/harden.sh"


@dataclass(frozen=True, slots=True)
class HardeningReport:
    compliant: bool
    ok: list[str]
    failed: list[str]
    raw: str


def parse_check(stdout: str, exit_code: int) -> HardeningReport:
    """Разобрать вывод `harden.sh check`.

    Ненулевой код возврата означает несоответствие даже если строк FAIL
    не видно: сценарий мог упасть раньше, чем начал печатать проверки.
    """
    ok: list[str] = []
    failed: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("OK "):
            ok.append(stripped[3:].strip())
        elif stripped.startswith("FAIL "):
            failed.append(stripped[5:].strip())
    return HardeningReport(
        compliant=exit_code == 0 and not failed,
        ok=ok,
        failed=failed,
        raw=stdout,
    )


async def upload(ssh: SSHClient) -> None:
    """Положить свежую копию сценария на сервер."""
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    await ssh.write_file(REMOTE_PATH, content, mode=0o700)


async def check(ssh: SSHClient) -> HardeningReport:
    """Проверить соответствие эталону. Ничего на сервере не меняет."""
    await upload(ssh)
    res = await ssh.run(f"{REMOTE_PATH} check")
    return parse_check(res.stdout, res.exit_code)


# Путь захардкожен под root — как и REMOTE_PATH выше: весь проект (и сам бот,
# и то, что он ставит на серверах) работает от root, второго пользователя нет.
KEY_PATH = "/root/.ssh/bot_server1"


async def ensure_bot_key(ssh: SSHClient, session, server_id: int) -> bool:
    """Завести боту собственный ключ и записать его в базу.

    Пароль намеренно не трогаем: он остаётся рабочим путём на сервер,
    пока вход по ключу не доказан. Гасит пароль отдельный шаг — и только
    после успеха этой функции.

    Доказательство входа — НЕ проверка "с сервера на самого себя" через
    127.0.0.1 (она ничего не говорит о реальном пути бота: внешний адрес,
    ufw, политики sshd в петлю не попадают), а отдельное подключение
    ровно так, как ходит сам бот — asyncssh, по адресу и порту из базы,
    строго ключом, без пароля в кредах вовсе.
    """
    from bot.db import repo
    from bot.services.crypto import encrypt

    server = await repo.get_server(session, server_id)
    if server is None:
        logger.warning("ensure_bot_key: сервер id={} не найден в базе", server_id)
        return False

    # 1. Ключ на сервере — генерируем, если ещё нет. Ошибку ssh-keygen не
    # глушим: check=True поднимет SSHError с внятным stderr.
    gen = (
        f"[ -f {KEY_PATH} ] || ssh-keygen -t ed25519 -N '' "
        f"-C 'myvpn-bot' -f {KEY_PATH}"
    )
    await ssh.run(gen, check=True)
    await ssh.run(f"chmod 600 {KEY_PATH}", check=True)

    # 2. authorized_keys — устойчиво. Права выставляем явно, а не полагаемся
    # на umask (на боевом сервере sshd работает с StrictModes yes: при
    # umask 0000 файл ляжет 0666 и sshd молча проигнорирует все ключи в
    # нём, включая уже работающие чужие). Сравнение — по всей строке
    # (-qxF), а не по подстроке: иначе ключ-подстрока другого ключа даёт
    # ложное совпадение и не позволяет дописаться. Дозапись — через
    # printf с явными переводами строки по обе стороны: если в файле нет
    # финального \n, "cat >>" склеит ботовый ключ с последней строкой и
    # сломает оба — и ботовый, и тот, что был последним (на боевом сервере
    # это чужой ключ "termux" телефона Влада, ломать его нельзя).
    await ssh.run("mkdir -p /root/.ssh && chmod 700 /root/.ssh", check=True)
    await ssh.run(
        "touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys",
        check=True,
    )
    pub = (await ssh.run(f"cat {KEY_PATH}.pub", check=True)).stdout.strip()
    if not pub:
        logger.warning(
            "ensure_bot_key: пустой публичный ключ на сервере id={}", server_id
        )
        return False
    quoted_pub = shlex.quote(pub)
    await ssh.run(
        f"grep -qxF -- {quoted_pub} /root/.ssh/authorized_keys "
        f"|| printf '\\n%s\\n' {quoted_pub} >> /root/.ssh/authorized_keys",
        check=True,
    )

    # 3. Приватная часть обязана реально парситься как ключ — иначе после
    # выключения пароля бот не сможет зайти вообще никак (и паролем уже
    # нельзя, и битым ключом тоже).
    private = await ssh.read_file(KEY_PATH)
    try:
        asyncssh.import_private_key(private)
    except (asyncssh.KeyImportError, ValueError) as exc:
        logger.warning(
            "ensure_bot_key: приватный ключ не распознан на сервере id={}: {}",
            server_id,
            exc,
        )
        return False

    # 4. Доказательство: отдельное подключение по реальным host/port/user
    # сервера, строго ключом. Пароль в креды не кладём вовсе — если бы
    # положили, asyncssh при отказе ключа молча откатился бы на пароль,
    # и проба соврала бы про то, что доказывает.
    creds = SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        private_key=private,
    )
    try:
        async with SSHClient(creds) as probe:
            result = await probe.run("echo ok")
    except (SSHError, OSError) as exc:
        logger.warning(
            "ensure_bot_key: вход по ключу не подтверждён на сервере id={}: {}",
            server_id,
            exc,
        )
        return False
    if result.exit_code != 0 or result.stdout.strip() != "ok":
        logger.warning(
            "ensure_bot_key: вход по ключу не подтверждён на сервере id={}",
            server_id,
        )
        return False

    # 5. Ключ доказан — фиксируем в базе немедленно: следующим шагом
    # гасится пароль, и ключ обязан быть в базе ДО этого, а не
    # «когда-нибудь при закрытии сессии».
    server.ssh_key_enc = encrypt(private)
    server.ssh_key_passphrase_enc = None
    await session.commit()
    return True
