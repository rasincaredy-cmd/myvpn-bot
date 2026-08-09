"""Приведение сервера к эталону безопасности.

Единственное место, которое знает, как доставить сценарий-эталон на
сервер, запустить его команды в нужном порядке и разобрать результат.
Сам сценарий живёт в репозитории (`scripts/hardening/harden.sh`) — копию
его текста в коде держать нельзя, копии разъезжаются.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from bot.services.ssh import SSHClient

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


KEY_PATH = "/root/.ssh/bot_server1"


async def ensure_bot_key(ssh: SSHClient, session, server_id: int) -> bool:
    """Завести боту собственный ключ и записать его в базу.

    Пароль намеренно не трогаем: он остаётся рабочим путём на сервер,
    пока вход по ключу не доказан. Гасит пароль отдельный шаг — и только
    после успеха этой функции.
    """
    from bot.db import repo
    from bot.services.crypto import encrypt

    gen = (
        f"[ -f {KEY_PATH} ] || ssh-keygen -t ed25519 -N '' "
        f"-C 'myvpn-bot' -f {KEY_PATH} >/dev/null 2>&1"
    )
    await ssh.run(gen, check=True)
    await ssh.run(f"chmod 600 {KEY_PATH}", check=True)
    await ssh.run(
        f"grep -qF -- \"$(cat {KEY_PATH}.pub)\" /root/.ssh/authorized_keys "
        f"|| cat {KEY_PATH}.pub >> /root/.ssh/authorized_keys",
        check=True,
    )

    # Доказательство: отдельное подключение строго по ключу, пароль запрещён.
    probe = await ssh.run(
        f"ssh -n -i {KEY_PATH} -o StrictHostKeyChecking=no "
        f"-o PasswordAuthentication=no -o BatchMode=yes -o ConnectTimeout=10 "
        f"root@127.0.0.1 'echo ok'"
    )
    if "ok" not in probe.stdout:
        logger.warning("Вход по ключу не подтверждён на сервере id={}", server_id)
        return False

    private = await ssh.read_file(KEY_PATH)
    server = await repo.get_server(session, server_id)
    if server is None:
        return False
    server.ssh_key_enc = encrypt(private)
    server.ssh_key_passphrase_enc = None
    # Фиксируем немедленно: следующим шагом гасится пароль, и ключ обязан
    # быть в базе ДО этого, а не «когда-нибудь при закрытии сессии».
    await session.commit()
    return True
