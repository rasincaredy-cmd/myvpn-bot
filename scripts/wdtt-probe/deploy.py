"""Поставить учёт прилёта пакетов на порты обхода на сервер бота.

    python scripts/wdtt-probe/deploy.py <server_id> [...]

Идемпотентно: повторный запуск обновляет скрипт и юниты и оставляет историю на
месте. Ходит теми же кредами, что и бот, — отдельный доступ на ноды не нужен.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.db import repo
from bot.db.base import session_scope
from bot.db.repo import creds_from_server
from bot.services.ssh import SSHClient, SSHError

HERE = Path(__file__).resolve().parent
SCRIPT_LOCAL = HERE / "wdtt-probe.sh"
SCRIPT_REMOTE = "/usr/local/bin/wdtt-probe.sh"

SERVICE_PATH = "/etc/systemd/system/wdtt-probe.service"
TIMER_PATH = "/etc/systemd/system/wdtt-probe.timer"

SERVICE_UNIT = f"""[Unit]
Description=Учёт прилёта пакетов на порты обхода БС
After=network.target

[Service]
Type=oneshot
ExecStart={SCRIPT_REMOTE}
"""

# Ровно по минутам, а не «раз в 60 секунд от старта»: выровненные отсчёты
# читаются глазами, когда надо посмотреть конкретный час задним числом.
TIMER_UNIT = """[Unit]
Description=Учёт прилёта пакетов на порты обхода БС — раз в минуту

[Timer]
OnCalendar=*:*:00
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


async def deploy(server_id: int) -> bool:
    async with session_scope() as session:
        server = await repo.get_server(session, server_id)
        if server is None:
            print(f"сервер {server_id}: не найден")
            return False
        creds = creds_from_server(server)
        name, host = server.name, server.host

    try:
        async with SSHClient(creds) as ssh:
            await ssh.put_file(str(SCRIPT_LOCAL), SCRIPT_REMOTE, mode=0o755)
            await ssh.write_file(SERVICE_PATH, SERVICE_UNIT, mode=0o644)
            await ssh.write_file(TIMER_PATH, TIMER_UNIT, mode=0o644)
            await ssh.run("systemctl daemon-reload")
            await ssh.run("systemctl enable --now wdtt-probe.timer")
            # Первый прогон вручную: ставит правила и заводит историю сразу,
            # не дожидаясь минуты, — и сразу показывает, если что-то не так.
            res = await ssh.run(SCRIPT_REMOTE, check=False)
            if res.exit_code != 0:
                print(f"{name} ({host}): скрипт вернул {res.exit_code}: "
                      f"{res.stderr.strip()[:300]}")
                return False
            state = await ssh.run(
                "systemctl is-active wdtt-probe.timer; "
                "iptables -w 5 -S WDTT_PROBE | grep -c '^-A'; "
                "tail -4 /var/log/wdtt-probe/*.log",
                check=False,
            )
            print(f"--- {name} ({host})\n{state.stdout.strip()}")
            return True
    except SSHError as exc:
        print(f"{name} ({host}): SSH — {exc}")
        return False


async def main(ids: list[int]) -> int:
    ok = True
    for sid in ids:
        ok &= await deploy(sid)
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main([int(a) for a in sys.argv[1:]])))
