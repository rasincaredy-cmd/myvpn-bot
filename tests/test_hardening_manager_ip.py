"""Белый список банилки обязан включать адрес УПРАВЛЯЮЩЕГО хоста.

Найдено аудитом 20.08.2026 на живых серверах. В списке был только собственный
адрес ноды. На первой ноде это работало по совпадению — бот живёт на ней же
(31.77.157.162 и там, и там). На второй, немецкой, в списке стоял её
собственный 31.77.148.187, а бот приходил с 31.77.157.162 — и его там не было.

Цена: пять неудачных попыток SSH за 10 минут (смена ключа, кривые креды) — и
нода банит бота на час. На этот час встают проверки живости, выдача конфигов,
отзывы и учёт трафика по этой локации. Мина была невидимой ровно потому, что на
первой ноде не срабатывала.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hardening" / "harden.sh"

# Заглушка is_ipv4: в сценарии она объявлена выше вырезаемого блока.
_IS_IPV4 = (
    'is_ipv4() { case "$1" in '
    '""|*[!0-9.]*) return 1;; '
    '*.*.*.*) return 0;; '
    '*) return 1;; esac; }\n'
)

_ECHO_JAIL = (
    'echo "ignoreip = 127.0.0.1/8 ::1 '
    '${OWN_IP}${MANAGER_IP:+ $MANAGER_IP} 10.8.0.0/24"\n'
)


def _jail_line(tmp_path: Path, *, ssh_connection: str | None, own_ip: str) -> str:
    """Прогоняет кусок сценария, отвечающий за адрес управляющего хоста.

    Целиком harden здесь не запустить — он трогает systemd и fail2ban. Поэтому
    берём ИЗ ФАЙЛА тот самый блок и собираем ту же строку ignoreip: тест на
    подстроке в исходнике остался бы зелёным при сломанной логике.
    """
    env = {**os.environ, "OWN_IP": own_ip}
    if ssh_connection is not None:
        env["SSH_CONNECTION"] = ssh_connection
    else:
        env.pop("SSH_CONNECTION", None)

    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("MANAGER_IP_FILE=")
    end = src.index("# Совпал с собственным адресом", start)
    block = src[start:end].replace(
        "/root/.harden_manager_ip", str(tmp_path / "manager_ip")
    )
    prog = (
        _IS_IPV4
        + block
        + '\n[ "$MANAGER_IP" = "$OWN_IP" ] && MANAGER_IP=""\n'
        + _ECHO_JAIL
    )
    res = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


class TestManagerIpInWhitelist:
    def test_bot_address_is_whitelisted(self, tmp_path: Path) -> None:
        """Ядро бага: бот приходит с ЧУЖОГО адреса — он обязан попасть в список."""
        line = _jail_line(
            tmp_path,
            ssh_connection="31.77.157.162 54321 31.77.148.187 22",
            own_ip="31.77.148.187",
        )
        assert "31.77.157.162" in line, line
        assert "31.77.148.187" in line, "собственный адрес ноды потерялся"

    def test_no_duplicate_when_bot_lives_on_the_node(self, tmp_path: Path) -> None:
        """Первая нода — бот на ней же. Адрес не должен дублироваться."""
        line = _jail_line(
            tmp_path,
            ssh_connection="31.77.157.162 54321 31.77.157.162 22",
            own_ip="31.77.157.162",
        )
        assert line.count("31.77.157.162") == 1, line

    def test_remembers_across_local_runs(self, tmp_path: Path) -> None:
        """Запуск руками или из cron идёт без SSH_CONNECTION. Адрес обязан
        подхватиться из запомненного, иначе локальный прогон вычеркнул бы бота
        из белого списка — и мина взвелась бы обратно."""
        _jail_line(
            tmp_path,
            ssh_connection="31.77.157.162 54321 31.77.148.187 22",
            own_ip="31.77.148.187",
        )
        line = _jail_line(tmp_path, ssh_connection=None, own_ip="31.77.148.187")
        assert "31.77.157.162" in line, line

    def test_garbage_connection_is_ignored(self, tmp_path: Path) -> None:
        line = _jail_line(tmp_path, ssh_connection="ерунда 1 2 3", own_ip="1.2.3.4")
        assert "ерунда" not in line

    def test_survives_without_any_source(self, tmp_path: Path) -> None:
        """Нет ни переменной, ни файла — просто без адреса управляющего хоста,
        а не падение: сценарий обязан доработать до конца."""
        line = _jail_line(tmp_path, ssh_connection=None, own_ip="1.2.3.4")
        assert "1.2.3.4" in line


def test_script_still_valid_bash() -> None:
    res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
