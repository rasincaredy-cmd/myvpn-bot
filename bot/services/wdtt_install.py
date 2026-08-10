"""Установка обхода БС (wdtt) на сервер проекта.

Мастер установки ставил только AmneziaWG, а обход админ доносил руками. 10.08
это выстрелило: на германской ноде тумблер «Обход БС» был включён, программы
не было, и юзер вместо доступа получал «на сервере заминка».

Порядок шагов повторяет ручную установку, которая сработала: залить программу
→ написать службу → запустить → убедиться, что управляющий сокет отвечает.
Последний шаг обязателен: демон падает при старте, если у него нет ни одного
активного пароля (`[WRAP] нет активных паролей для WRAP` в main.go), поэтому
«служба active» сама по себе ничего не доказывает — а выдаёт доступы бот
именно через сокет.

Программу берём с сервера, где живёт бот: она там уже лежит по тому же пути,
что настроен для вызовов `ctl`. Отдельного места хранения не заводим —
лишний источник, который начнёт расходиться с рабочим.
"""
from __future__ import annotations

import asyncio
import secrets

from loguru import logger

from bot.config import settings
from bot.services.ssh import SSHError

UNIT_PATH = "/etc/systemd/system/wdtt.service"
CONFIG_DIR = "/etc/wdtt"
DEFAULT_DNS = "1.1.1.1,1.0.0.1"
DEFAULT_PORTS = "56000,56001,9000"

# Ожидание готовности демона после запуска: WireGuard-устройство, ключи и
# управляющий сокет поднимаются не мгновенно.
_READY_RETRIES = 6
_READY_DELAY = 2

_UNIT_TEMPLATE = """[Unit]
Description=WDTT VPN Server
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/env bash -c "ip link show wdtt0 >/dev/null 2>&1 && ip link del wdtt0 2>/dev/null || true"
ExecStart={binary} -listen 0.0.0.0:{dtls} -wg-port {wg} -config-dir {config_dir} -password {password} -dns {dns}
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
"""


def _split_ports(ports: str | None) -> tuple[str, str]:
    """Порты обхода из строки «dtls,wg,tun» — как они хранятся у сервера."""
    parts = (ports or DEFAULT_PORTS).split(",")
    dtls = parts[0].strip() if parts else "56000"
    wg = parts[1].strip() if len(parts) > 1 else "56001"
    return dtls, wg


async def _is_working(ssh, binary: str) -> bool:
    """Обход считается рабочим, только если и служба жива, и сокет отвечает."""
    active = await ssh.run("systemctl is-active wdtt")
    if active.stdout.strip() != "active":
        return False
    ctl = await ssh.run(f"{binary} ctl -op list")
    return ctl.exit_code == 0 and '"ok":true' in ctl.stdout.replace(" ", "")


async def install(ssh, *, ports: str | None, dns: str | None, progress) -> bool:
    """Поставить обход БС на сервер. True — обход поднят и отвечает.

    Идемпотентна: на сервере с работающим обходом не трогает ничего. Перезапись
    службы сменила бы пароль владельца и перезапустила демон, оборвав всех,
    кто сидит через обход прямо сейчас.
    """
    binary = settings.wdtt_binary_path

    if await _is_working(ssh, binary):
        return True

    dtls, wg = _split_ports(ports)

    await progress("Ставлю резервное подключение...")
    try:
        await ssh.put_file(binary, binary, mode=0o755)
    except SSHError as exc:
        logger.warning("wdtt: не удалось залить программу: {}", exc)
        return False

    # Пароль владельца существует ради одного: без активного пароля демон
    # не стартует. В команду шелла он не попадает — только в файл службы.
    unit = _UNIT_TEMPLATE.format(
        binary=binary,
        dtls=dtls,
        wg=wg,
        config_dir=CONFIG_DIR,
        password=secrets.token_urlsafe(15),
        dns=dns or DEFAULT_DNS,
    )
    try:
        await ssh.write_file(UNIT_PATH, unit, mode=0o600)
    except SSHError as exc:
        logger.warning("wdtt: не удалось записать службу: {}", exc)
        return False

    await ssh.run("systemctl daemon-reload")
    await ssh.run("systemctl enable --now wdtt")

    for attempt in range(_READY_RETRIES):
        if await _is_working(ssh, binary):
            return True
        if attempt < _READY_RETRIES - 1:
            await asyncio.sleep(_READY_DELAY)

    logger.warning("wdtt: служба не поднялась или управляющий сокет молчит")
    return False
