"""Страж: из сценария-эталона не должны исчезнуть критичные защиты.

Каждая проверка здесь стоит за конкретной аварией, которая случается,
если соответствующий кусок сценария потерять.
"""
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hardening" / "harden.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists() -> None:
    assert SCRIPT.is_file(), "сценарий-эталон пропал"


def test_script_is_valid_bash() -> None:
    # Minor-2: ни один тест раньше не прогонял сценарий даже на синтаксис —
    # синтаксическая ошибка уехала бы незамеченной до первого боевого запуска.
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


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


def _closes_pipe_early(segment: str) -> bool:
    """Команда-приёмник, которая может закрыть трубу до того, как источник
    дочитает весь свой вывод (grep -q/-l и т.п., grep -m1, head -N)."""
    segment = segment.strip()
    return bool(
        # Границу слова после [qQlL] ставить НЕЛЬЗЯ: она ломает ловлю
        # комбинированных флагов `-qE`, `-qF`, `-qx`, `-qi` — а именно ими
        # написан весь сценарий. Проверено: с `\b` страж пропускал их все,
        # то есть был слабее, чем до расширения.
        re.match(r"grep\s+(-\w*[qQlL]\w*(\s|$)|--quiet\b|--silent\b)", segment)
        or re.match(r"grep\s+.*(-m\s*1\b|--max-count(=|\s+)1\b)", segment)
        or re.match(r"head\s+(-1\b|-n\s*1\b)", segment)
    )


def test_no_pipe_into_grep_q(text: str) -> None:
    """Под `set -o pipefail` конструкция `команда | grep -q ...` врёт наоборот.

    `grep -q` закрывает трубу на первом совпадении, источник получает
    SIGPIPE и завершается с ошибкой, pipefail делает «неуспешным» весь
    конвейер — и НАЙДЕННАЯ строка читается как ненайденная. На этом уже
    попались: проверка рапортовала «вход по паролю РАЗРЕШЁН» на сервере,
    где пароль был выключен. Забирай вывод в переменную или используй
    here-string.

    Minor-3: ловим не только короткую `-q`, но и `--quiet`/`--silent`,
    `grep -m1` (совпадение того же класса, читает ровно N строк и рвёт
    трубу) и `head -1`/`head -n1` — тоже рано закрывающие приёмники.
    """
    offenders = []
    for i, ln in enumerate(text.splitlines(), 1):
        # комментарии пропускаем: они не исполняются, а как раз в них и
        # написано предупреждение про этот капкан
        if ln.lstrip().startswith("#"):
            continue
        parts = ln.split("|")
        if len(parts) < 2:
            continue
        if any(_closes_pipe_early(seg) for seg in parts[1:]):
            offenders.append(f"строка {i}: {ln.strip()}")
    assert not offenders, "конвейер в приёмник, закрывающий трубу рано, под pipefail:\n" + "\n".join(offenders)


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


def test_rollback_survives_either_ssh_unit_name(text: str) -> None:
    # I8: юнит ssh называется по-разному (ssh на Debian/Ubuntu, sshd на
    # прочих). Автооткат, прибитый к одному имени, на "чужой" системе
    # удалит файл настроек, но не перечитает конфиг — доступ не вернётся.
    rollback_line = next(
        (ln for ln in text.splitlines() if "rollback-sshd" in ln and "arm_rollback" in ln),
        "",
    )
    assert "restart ssh" in rollback_line and "restart sshd" in rollback_line, (
        "автооткат sshd пробует только один вариант имени юнита"
    )


def test_dropin_uses_00_prefix(text: str) -> None:
    # C2: 99- проигрывает типовому 50-cloud-init.conf по алфавиту — sshd
    # берёт первое встреченное значение. 00- гарантированно первый.
    assert "SSHD_DROPIN=/etc/ssh/sshd_config.d/00-hardening.conf" in text


def test_include_checked_before_disabling_password(text: str) -> None:
    # C2: без Include в sshd_config весь каталог sshd_config.d молча
    # игнорируется — drop-in никогда не прочитается, а скрипт до этой
    # проверки рапортовал бы "пароль выключен" на живом пароле.
    assert "sshd_dropin_included" in text


def test_password_off_is_verified_after_restart(text: str) -> None:
    # C2: зелёный рестарт ещё не значит, что пароль реально выключен —
    # его мог перебить другой drop-in-файл. Нужна проверка факта через
    # sshd -T, а не намерения.
    assert "password_actually_off" in text


def test_dangerous_steps_self_cancel_rollback(text: str) -> None:
    # C1: повторный прогон на уже настроенном сервере (штатно для бота,
    # без человека) не должен полагаться на то, что кто-то руками позовёт
    # rollback-cancel — иначе автооткат снимет защиту через 10 минут.
    assert "rollback_cancel_unit" in text
    assert text.count("rollback_cancel_unit rollback-sshd") >= 1
    assert text.count("rollback_cancel_unit rollback-ufw") >= 1


def test_firewall_requires_ssh_port_before_enabling(text: str) -> None:
    # C3: мгновенный снимок слушающих портов может не увидеть порт,
    # который ещё не поднялся (например VPN сразу после установки).
    # Обязательные порты (ssh — всегда) должны проверяться ДО включения.
    assert "current_ssh_port" in text
    assert "port_is_listening" in text


def test_private_bound_ports_not_opened_to_everyone(text: str) -> None:
    # I1: служба, слушающая только на внутреннем (VPN/приватном) адресе,
    # не должна получить правило "разрешить всем" — иначе, например,
    # резолвер на 10.8.0.1:53 превращается в открытый резолвер для интернета.
    assert "is_private_ipv4" in text


def test_firewall_check_looks_at_default_policy(text: str) -> None:
    # I2: включённый ufw с политикой "allow incoming" пропускает всё —
    # без этой проверки такой сервер получал бы "соответствует эталону".
    assert "Default: deny" in text


def test_fail2ban_check_polls_the_jail(text: str) -> None:
    # I3: служба fail2ban может быть active, а конкретный джейл sshd —
    # не подняться. Проверяем сам джейл, а не только факт, что демон жив.
    assert "fail2ban-client status sshd" in text


def test_apply_stats_command_exists(text: str) -> None:
    # I6: check требует sysstat-collect.timer, но раньше ни одна команда
    # применения его не устанавливала — новый сервер никогда бы не стал
    # "соответствующим эталону".
    assert "apply-stats" in text
    assert "cmd_apply_stats" in text


def test_firewall_verifies_restricted_rules(text: str) -> None:
    """N2: сверка после включения не должна отвергать собственные
    ограниченные правила.

    Порт, слушающий на приватном адресе, получает правило вида
    `ufw allow from 10.8.0.0/24 to 10.8.0.1 port 53` — в выводе `ufw status`
    такая строка начинается с адреса назначения, а не с `53/udp`. Сверка,
    привязанная к началу строки, не найдёт правило, решит что порт не
    открыт, вернёт ошибку и ОСТАВИТ автооткат вооружённым — через 10 минут
    фаервол выключится на корректно настроенном сервере.
    """
    assert "rule_present" in text


def test_fail2ban_installs_systemd_backend_dependency(text: str) -> None:
    # N3: джейл с backend=systemd не стартует без python3-systemd, и
    # apply-fail2ban честно возвращает ошибку — то есть автоматика встанет
    # на ровном месте. Зависимость надо ставить вместе с fail2ban.
    assert "python3-systemd" in text


def test_fail2ban_dependency_installed_unconditionally(text: str) -> None:
    """Important-1: установка python3-systemd не должна зависеть от того,
    пришлось ли ставить fail2ban «с нуля».

    backend=systemd пишется в jail.local на КАЖДОМ прогоне apply-fail2ban.
    Если зависимость ставится только внутри
    `if ! systemctl is-active --quiet fail2ban; then ... fi`, то на
    образе, где fail2ban уже установлен и активен (например собран с
    --no-install-recommends), а python3-systemd не хватает, эта ветка не
    выполнится вовсе — джейл не поднимется, и это будет повторяться на
    каждом прогоне бота без самолечения.
    """
    start = text.index("if ! systemctl is-active --quiet fail2ban; then")
    end = text.index("\n  fi\n", start)
    inside_if = text[start:end]
    after_if = text[end : text.index("cat > /etc/fail2ban/jail.local", end)]
    assert "python3-systemd" not in inside_if, (
        "зависимость systemd-бэкенда ставится только внутри условия "
        "«fail2ban ещё не активен» — не установится, если fail2ban уже стоит"
    )
    assert "python3-systemd" in after_if, (
        "нет безусловной установки python3-systemd между установкой "
        "fail2ban и записью jail.local"
    )


def test_fail2ban_waits_for_jail(text: str) -> None:
    # N3: опрос джейла сразу после старта на нагруженной машине даёт
    # ложный отказ. Нужен повтор, а не единственная попытка после sleep.
    assert "jail_ready" in text


def test_plan_marks_private_bound_ports(text: str) -> None:
    # Команда plan существует ради одного — показать, что изменится.
    # Приватно-привязанные порты apply-firewall наружу НЕ откроет, значит
    # показывать их в списке «останутся открытыми наружу» — враньё.
    #
    # ВНИМАНИЕ: не проверять просто "только изнутри" — эта подстрока уже
    # была в файле ДО правки (комментарий про панель "только изнутри VPN и
    # обхода"), и тест был бы зелёным на невыполненной работе. Проверяем
    # фразы, которых в старом cmd_plan не было и быть не могло.
    assert "наружу открыт не будет" in text
    assert "слушают только на приватном адресе" in text


def test_rule_present_requires_allow_or_limit_action(text: str) -> None:
    """Important-2: rule_present обязана ловить обе формы записи правила,
    но НЕ должна принимать голое совпадение номера порта где угодно в
    строке — иначе `22/tcp DENY Anywhere` (запрещающее правило, оставшееся
    от неудавшегося `ufw --force reset`) читалась бы как «правило есть»,
    пост-сверка рапортовала бы успех, и автооткат снимался бы на сервере,
    где нужный порт на самом деле закрыт первым же совпадением DENY.

    Тест реально выполняет функцию rule_present из сценария на образце
    настоящего вывода `ufw status` с боевого сервера (обычная форма,
    форма "(v6)" и форма, ограниченная по адресу назначения), а не просто
    ищет имя функции по тексту.
    """
    match = re.search(r"^rule_present\(\) \{.*?\n\}\n", text, re.MULTILINE | re.DOTALL)
    assert match, "функция rule_present не найдена в сценарии"
    func_src = match.group(0)

    allow_sample = (
        "To                         Action      From\n"
        "--                         ------      ----\n"
        "22/tcp                     ALLOW       Anywhere\n"
        "585/udp                    ALLOW       Anywhere\n"
        "6769/tcp                   ALLOW       10.8.0.0/24\n"
        "6769/tcp                   ALLOW       10.66.66.0/24\n"
        "22/tcp (v6)                ALLOW       Anywhere (v6)\n"
        "10.8.0.1 53/udp            ALLOW       10.8.0.0/24\n"
    )
    deny_only_sample = "22/tcp                     DENY        Anywhere\n"

    script = f"""
set -uo pipefail
{func_src}

allow_status=$(cat <<'SAMPLE_EOF'
{allow_sample}SAMPLE_EOF
)
rule_present "$allow_status" 22 tcp || exit 11
rule_present "$allow_status" 6769 tcp || exit 12
rule_present "$allow_status" 53 udp || exit 13

deny_status=$(cat <<'SAMPLE_EOF'
{deny_only_sample}SAMPLE_EOF
)
if rule_present "$deny_status" 22 tcp; then
  exit 14
fi
exit 0
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True
    )
    codes = {
        11: "обычная форма '22/tcp ALLOW Anywhere' не распознана как присутствующая",
        12: "правило, ограниченное по адресу источника (6769/tcp ALLOW <subnet>), не распознано",
        13: "правило, ограниченное по адресу назначения (10.8.0.1 53/udp ALLOW ...), не распознано",
        14: "строка '22/tcp DENY Anywhere' без ALLOW/LIMIT ошибочно считается присутствующим правилом",
    }
    assert result.returncode == 0, (
        codes.get(result.returncode, f"код {result.returncode}")
        + f"\nstdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_firewall_checks_required_port_reachability_before_rollback(text: str) -> None:
    """Important-4: обязательный порт, слушающий ТОЛЬКО на приватном адресе
    вне VPN_SUBNET/BYPASS_SUBNET, никогда не получит от cmd_apply_firewall
    ни одного ufw-правила (её основной цикл просто делает `continue` для
    такого адреса) — пост-сверка после включения никогда не найдёт для
    него ALLOW. Проверять это нужно ДО arm_rollback и ДО единой правки
    фаервола, а не постфактум, когда автооткат уже вооружён, а фаервол уже
    включён.
    """
    assert "required_port_reachable" in text
    check_idx = text.index('required_port_reachable "$ports" "$rp"')
    rollback_idx = text.index('arm_rollback rollback-ufw "ufw --force disable"')
    assert check_idx < rollback_idx, (
        "проверка достижимости обязательного порта стоит после "
        "arm_rollback, а не до него"
    )
