"""Страж: из сценария-эталона не должны исчезнуть критичные защиты.

Каждая проверка здесь стоит за конкретной аварией, которая случается,
если соответствующий кусок сценария потерять.
"""
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hardening" / "harden.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists() -> None:
    assert SCRIPT.is_file(), "сценарий-эталон пропал"


def test_forward_policy_accept(text: str) -> None:
    # Без этого ufw дропает форвард: VPN подключается, интернета у клиента нет.
    assert 'DEFAULT_FORWARD_POLICY="ACCEPT"' in text


def test_whitelist_has_vpn_subnets(text: str) -> None:
    # Без белого списка банилка забанит самого бота (он ходит по SSH сам на себя).
    assert "10.8.0.0/24" in text
    assert "10.66.66.0/24" in text


def test_whitelist_has_own_external_ip(text: str) -> None:
    # Собственный адрес сервера вычисляется, а не хардкодится.
    assert "OWN_IP" in text


def test_dangerous_steps_have_rollback(text: str) -> None:
    # Автооткат ставится ДО изменения, иначе потеря доступа необратима.
    assert "systemd-run" in text
    assert "--on-active=10min" in text


def test_password_disabled_only_after_key_check(text: str) -> None:
    # Порядок из спеки: сначала доказать вход по ключу, потом гасить пароль.
    key_check = text.index("verify_key_login")
    disable = text.index("PasswordAuthentication no")
    assert key_check < disable, "выключение пароля стоит раньше проверки ключа"


def test_no_pipe_into_grep_q(text: str) -> None:
    """Под `set -o pipefail` конструкция `команда | grep -q ...` врёт наоборот.

    `grep -q` закрывает трубу на первом совпадении, источник получает
    SIGPIPE и завершается с ошибкой, pipefail делает «неуспешным» весь
    конвейер — и НАЙДЕННАЯ строка читается как ненайденная. На этом уже
    попались: проверка рапортовала «вход по паролю РАЗРЕШЁН» на сервере,
    где пароль был выключен. Забирай вывод в переменную или используй
    here-string.
    """
    offenders = [
        f"строка {i}: {ln.strip()}"
        for i, ln in enumerate(text.splitlines(), 1)
        # комментарии пропускаем: они не исполняются, а как раз в них и
        # написано предупреждение про этот капкан
        if not ln.lstrip().startswith("#")
        and re.search(r"\|\s*grep\s+(-\w*q)", ln)
    ]
    assert not offenders, "конвейер в grep -q под pipefail:\n" + "\n".join(offenders)


def test_panel_restricted_to_vpn(text: str) -> None:
    # Панель x-ui открывается только из VPN, а не всему интернету.
    assert '"$VPN_SUBNET" to any port "$PANEL_PORT"' in text
    assert '"$BYPASS_SUBNET" to any port "$PANEL_PORT"' in text


def test_firewall_built_from_listening_ports(text: str) -> None:
    # Правила строятся от того, что реально слушает наружу. Список портов
    # из головы — верный способ забыть нужный и обрезать живой сервис.
    assert "listening_ports" in text


def test_rollback_removes_the_file_it_created(text: str) -> None:
    # Пароль выключается отдельным файлом настроек, значит откат обязан
    # именно УДАЛИТЬ этот файл. Восстановление старого sshd_config его не
    # тронет — и «страховка» окажется фиктивной: доступ не вернётся.
    rollback_line = next(
        (ln for ln in text.splitlines() if "rollback-sshd" in ln and "arm_rollback" in ln),
        "",
    )
    assert "rm -f" in rollback_line and "SSHD_DROPIN" in rollback_line, (
        "автооткат sshd не удаляет созданный им файл настроек"
    )
