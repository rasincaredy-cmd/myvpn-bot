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

# Порты новых режимов qWDTT. Значения — ЗАВОДСКИЕ для приложения: если взять
# свои, каждому юзеру придётся лезть в настройки и вбивать их руками.
#   direct — тот же замаскированный канал, но без DTLS: легче для телефона;
#   raw    — вообще без WireGuard, сервер поднимает свой интерфейс и NAT.
DIRECT_PORT = 56002
RAW_PORT = 56003

_UNIT_TEMPLATE = """[Unit]
Description=WDTT VPN Server
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/env bash -c "ip link show wdtt0 >/dev/null 2>&1 && ip link del wdtt0 2>/dev/null || true"
ExecStartPre=-/usr/bin/env bash -c "if command -v iptables >/dev/null 2>&1; then for P in {dtls} {direct} {raw}; do iptables -C INPUT -p udp --dport $P -m comment --comment WDTT_MANAGED -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport $P -m comment --comment WDTT_MANAGED -j ACCEPT; done; iptables -C INPUT -p tcp --dport {dtls} -m comment --comment WDTT_MANAGED -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport {dtls} -m comment --comment WDTT_MANAGED -j ACCEPT; iptables -C INPUT -p tcp --dport 22 -m comment --comment WDTT_MANAGED -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 22 -m comment --comment WDTT_MANAGED -j ACCEPT; fi"
ExecStart={binary} -listen 0.0.0.0:{dtls} -wg-port {wg} -listen-direct 0.0.0.0:{direct} -listen-raw 0.0.0.0:{raw} -config-dir {config_dir} -password {password} -dns {dns}
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
"""


def render_unit(*, binary: str, dtls: str, wg: str, password: str, dns: str) -> str:
    """Файл службы под все три режима. Одно место на установку и на обновление:
    разъехавшись, они начнут ставить разное, и «почему на новой стране нет raw»
    выяснится через месяц."""
    return _UNIT_TEMPLATE.format(
        binary=binary, dtls=dtls, wg=wg, direct=DIRECT_PORT, raw=RAW_PORT,
        config_dir=CONFIG_DIR, password=password, dns=dns,
    )


def unit_has_modes(unit: str) -> bool:
    """Есть ли в файле службы новые режимы. Нода со старым файлом работает, но
    raw и прямой режим у неё просто выключены — снаружи не видно никак."""
    return "-listen-raw" in unit and "-listen-direct" in unit


def parse_unit(unit: str) -> dict | None:
    """Достаёт из файла службы то, что нельзя потерять при перезаписи: пароль
    владельца, DNS и порты.

    Пароль владельца существует ради одного — без активного пароля демон не
    стартует; сгенерировать новый нельзя, он уже лежит в базе паролей ноды.
    Поэтому нет пароля — нет и перезаписи: возвращаем None, вызывающий обязан
    оставить ноду в покое. По этой же причине значение НИКУДА не логируется.
    """
    values = {"password": "", "dns": DEFAULT_DNS, "wg": "56001", "dtls": "56000"}
    for token, key in (
        ("-password ", "password"),
        ("-dns ", "dns"),
        ("-wg-port ", "wg"),
        ("-listen 0.0.0.0:", "dtls"),
    ):
        if token in unit:
            values[key] = unit.split(token, 1)[1].split()[0]
    if not values["password"]:
        return None
    return values


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
    unit = render_unit(
        binary=binary, dtls=dtls, wg=wg,
        password=secrets.token_urlsafe(15), dns=dns or DEFAULT_DNS,
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
