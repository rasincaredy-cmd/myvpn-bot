"""Провижнинг доступов обхода белых списков (wdtt) через управляющий сокет.

Бот по SSH зовёт `wdtt-server ctl -op ...` на сервере — форк wdtt-сервера
(control.go) добавляет/удаляет/листит пароли в ЖИВОМ демоне без рестарта и без
лимита 10. Бинарь печатает одну строку JSON в stdout; секреты (пароль/ссылку)
здесь НЕ логируем.
"""
from __future__ import annotations

import json
import shlex

from loguru import logger

from bot.services.ssh import SSHClient, SSHError

_DEFAULT_PORTS = "56000,56001,9000"


async def _run_ctl(ssh: SSHClient, binary: str, args: list[str]) -> dict:
    cmd = shlex.quote(binary) + " ctl " + " ".join(shlex.quote(a) for a in args)
    res = await ssh.run(cmd, check=False, timeout=40)
    out = res.stdout.strip()
    if not out:
        raise SSHError(
            f"wdtt ctl: пустой ответ (код {res.exit_code}): "
            f"{res.stderr.strip()[:200]}"
        )
    try:
        data = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SSHError(f"wdtt ctl: некорректный ответ: {out[:200]}") from exc
    if not data.get("ok"):
        raise SSHError(f"wdtt ctl: {data.get('error', 'неизвестная ошибка')}")
    return data


async def create_access(
    ssh: SSHClient,
    *,
    days: int,
    label: str,
    vk_hashes: str,
    ports: str | None,
    binary: str,
    password: str | None = None,
) -> dict:
    """Создаёт доступ на wdtt-сервере. Возвращает {'password', 'link', 'expires_at'}.

    password — режим restore (ревайв после продления): сервер добавляет ИМЕННО
    этот пароль, и прежняя wdtt://-ссылка клиента снова работает. Вызывающий
    обязан сверить, что вернулся тот же пароль (старый бинарь сервера молча
    сгенерил бы новый)."""
    args = [
        "-op", "add",
        "-days", str(days),
        "-label", label,
        "-hash", vk_hashes,
        "-ports", ports or _DEFAULT_PORTS,
    ]
    if password:
        args += ["-password", password]
    data = await _run_ctl(ssh, binary, args)
    logger.info(
        "wdtt access {} (label={}, days={})",
        "restored" if password else "created", label, days,
    )  # без секретов
    return {
        "password": data["password"],
        "link": data["link"],
        "expires_at": data.get("expires_at") or 0,
    }


async def remove_access(ssh: SSHClient, *, password: str, binary: str) -> bool:
    """Удаляет доступ. Идемпотентно (False, если пароля уже нет)."""
    data = await _run_ctl(ssh, binary, ["-op", "remove", "-password", password])
    return bool(data.get("removed"))


async def list_accesses(ssh: SSHClient, *, binary: str) -> list[dict]:
    data = await _run_ctl(ssh, binary, ["-op", "list"])
    return list(data.get("passwords", []))
