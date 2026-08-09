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

from bot.services.ssh import SSHClient, SSHError

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
