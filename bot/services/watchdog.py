"""Сторож за машиной бота: ноды присматривают за ней снаружи.

Бот живёт на одной из нод и о собственной беде сказать не может — ни когда у
машины пропадает интернет (27.08.2026: шесть минут без связи, и тревога о
неотвечающей ноде не ушла ровно потому, что связи не было), ни когда падает
сам бот. Значит, сказать должен кто-то снаружи.

Сторож — маленький сценарий на каждой ЧУЖОЙ ноде: раз в две минуты дёргает
страницу мини-приложения и, если та молчит дважды подряд, пишет админу прямо
в Telegram, минуя бота. Проверяем именно страницу, а не «жива ли машина»:
живая машина с повисшим ботом для юзера ничем не лучше мёртвой, а страницу
отдаёт тот же процесс, что и бота, — через тот же nginx и тот же сертификат.

Правила те же, что у тревог бота: одно сообщение на проблему, повтор не чаще
раза в час, обязательный отбой, когда отпустило. И четвёртое, выученное
27.08: недоставленное сообщение не считается отправленным — иначе сторож
промолчит навсегда.

Токен здесь основной, бота (решение Влада 27.08.2026). Поэтому файл с ним
кладётся правами 600 и не появляется ни в одной командной строке: аргументы
команд видны в списке процессов, файлы — нет.
"""
from __future__ import annotations

from loguru import logger

from bot.config import settings
from bot.services.ssh import SSHError

ENV_PATH = "/etc/myvpn-watchdog.env"
SCRIPT_PATH = "/usr/local/sbin/myvpn-watchdog"
UNIT_PATH = "/etc/systemd/system/myvpn-watchdog.service"
TIMER_PATH = "/etc/systemd/system/myvpn-watchdog.timer"
STATE_DIR = "/var/lib/myvpn-watchdog"
TIMER_NAME = "myvpn-watchdog.timer"

# Раз в две минуты, тревога после двух промахов подряд: обрыв 27.08 длился
# шесть минут и под такой порог попадает, а икание на полминуты — нет.
EVERY_MINUTES = 2
MISSES = 2


class WatchdogError(Exception):
    """Сторожа поставить не удалось; в тексте — причина для админа."""


def alive_url(miniapp_url: str) -> str:
    """Адрес страницы-отклика из адреса мини-приложения.

    Мини-приложение живёт по /app (в .env адрес бывает и со слешом на конце,
    и без), а отклик — соседним путём внутри того же места, которое nginx уже
    проксирует боту. Отдельный путь наверху потребовал бы правки nginx на
    каждой установке.
    """
    base = (miniapp_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/app"):
        return f"{base}/alive"
    return f"{base}/app/alive"


def render_env(*, url: str, token: str, admins: list[int], node: str) -> str:
    """Настройки сторожа. Кавычки вокруг значений обязательны: имя ноды
    задаёт админ, и пробел в нём иначе развалил бы файл."""
    return (
        "# Сторож за машиной бота. Ставится ботом, править руками не нужно.\n"
        f'URL="{url}"\n'
        f'TOKEN="{token}"\n'
        f'ADMINS="{" ".join(str(a) for a in admins)}"\n'
        f'NODE="{node}"\n'
        f'EVERY="{EVERY_MINUTES}"\n'
        f'MISSES="{MISSES}"\n'
    )


_SCRIPT = """#!/bin/sh
# Сторож за машиной бота. Поставлен ботом, правки затрутся при переустановке.
#
# Раз в EVERY минут дёргает URL. MISSES промахов подряд — пишем админам прямо
# в Telegram: бот об этом сказать не может, он и есть то, что молчит.
set -u

# Пути переопределяются переменными окружения ровно затем, чтобы этот
# сценарий можно было прогнать тестами: логика сторожа работает в тот
# единственный момент, когда всё остальное уже лежит, и проверять её на
# боевой ноде поздно.
ENV_FILE=${WATCHDOG_ENV:-/etc/myvpn-watchdog.env}
[ -r "$ENV_FILE" ] || exit 0
. "$ENV_FILE"

STATE=${WATCHDOG_STATE:-/var/lib/myvpn-watchdog}
mkdir -p "$STATE"

num() {
    v=$(cat "$1" 2>/dev/null)
    case "$v" in
        ''|*[!0-9]*) echo 0 ;;
        *) echo "$v" ;;
    esac
}

# 0 — дошло хотя бы до одного админа. Недоставленное сообщение не считается
# отправленным: иначе сторож замолчит навсегда после первой же осечки.
send() {
    rc=1
    for chat in $ADMINS; do
        if curl -fsS --max-time 20 -o /dev/null \\
                --data-urlencode "chat_id=$chat" \\
                --data-urlencode "parse_mode=HTML" \\
                --data-urlencode "text=$1" \\
                "https://api.telegram.org/bot$TOKEN/sendMessage"; then
            rc=0
        fi
    done
    return $rc
}

now=$(date +%s)

if curl -fsS --max-time 10 -o /dev/null "$URL"; then
    if [ "$(num $STATE/alerted)" -eq 1 ]; then
        if send "✅ <b>Отпустило</b>
🖥 сторожит $NODE

«Бот не отвечает» — больше не наблюдается."; then
            echo 0 > $STATE/alerted
        fi
    fi
    echo 0 > $STATE/fails
    exit 0
fi

fails=$(( $(num $STATE/fails) + 1 ))
echo $fails > $STATE/fails
[ "$fails" -ge "$MISSES" ] || exit 0

# Повтор не чаще раза в час: авария бывает долгой, а тревога, приходящая
# каждые две минуты, перестаёт читаться на третий раз.
if [ "$(num $STATE/alerted)" -eq 1 ] && [ $(( now - $(num $STATE/last_sent) )) -lt 3600 ]; then
    exit 0
fi

if send "🔴 <b>Бот не отвечает</b>
🖥 сторожит $NODE

Страница мини-приложения молчит $(( fails * EVERY )) мин: $URL

Сам бот сказать об этом не может — он и есть то, что молчит. Проверь машину
бота и её сеть."; then
    echo 1 > $STATE/alerted
    echo $now > $STATE/last_sent
fi
"""


def render_script() -> str:
    return _SCRIPT


_UNIT = """[Unit]
Description=Watchdog for the myvpn bot host
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={script}
"""

_TIMER = """[Unit]
Description=Watchdog for the myvpn bot host (every {every} min)

[Timer]
OnBootSec={every}min
OnUnitActiveSec={every}min
AccuracySec=10s
Unit=myvpn-watchdog.service

[Install]
WantedBy=timers.target
"""


def render_unit() -> str:
    return _UNIT.format(script=SCRIPT_PATH)


def render_timer() -> str:
    return _TIMER.format(every=EVERY_MINUTES)


async def is_running(ssh) -> bool:
    """Стоит ли сторож и тикает ли таймер. Без файла настроек «active» врёт:
    сценарий с пустыми настройками молча выходит на первой строке."""
    res = await ssh.run(
        f"systemctl is-active {TIMER_NAME} 2>/dev/null; "
        f"test -s {ENV_PATH} && echo env-ok"
    )
    out = res.stdout or ""
    return "active" in out.split() and "env-ok" in out


async def is_bot_host(ssh) -> bool:
    """Мы на той самой машине, где живёт бот?

    Сторожить себя бессмысленно: машина без интернета не расскажет о том, что
    у неё нет интернета. Сравниваем адрес ноды с адресом, с которого пришёл
    бот, — тем же способом, каким тревога о самобане отличает бота от ноды.
    """
    res = await ssh.run(
        "ip route get 1.1.1.1 2>/dev/null "
        "| awk '{for (i = 1; i <= NF; i++) if ($i == \"src\") "
        "{ print $(i + 1); exit }}'; "
        'echo "${SSH_CONNECTION%% *}"'
    )
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return len(lines) == 2 and lines[0] == lines[1]


async def install(ssh, *, node_name: str) -> bool:
    """Поставить (или переставить) сторожа на ноду. Идемпотентна.

    True — поставлен, False — не нужен (это сама машина бота). Настоящая
    беда — WatchdogError с причиной для админа.

    Переустановка безопасна: сторож ничего не обслуживает и никого не рвёт,
    а свежий файл настроек нужен всегда — адрес машины бота и список админов
    меняются, и разъехавшись, сторож замолчит именно тогда, когда понадобится.
    """
    if await is_bot_host(ssh):
        return False

    url = alive_url(settings.miniapp_url)
    if not url:
        raise WatchdogError(
            "не задан адрес мини-приложения (MINIAPP_URL) — сторожу нечего "
            "проверять"
        )
    if not settings.admin_ids:
        raise WatchdogError("некому писать: в настройках нет ни одного админа")

    res = await ssh.run("command -v curl >/dev/null 2>&1 && echo yes || echo no")
    if "yes" not in (res.stdout or ""):
        raise WatchdogError("на ноде нет curl — сторожу нечем ходить в сеть")

    try:
        await ssh.write_file(
            ENV_PATH,
            render_env(
                url=url,
                token=settings.bot_token,
                admins=settings.admin_ids,
                node=node_name,
            ),
            mode=0o600,
        )
        await ssh.write_file(SCRIPT_PATH, render_script(), mode=0o700)
        await ssh.write_file(UNIT_PATH, render_unit(), mode=0o644)
        await ssh.write_file(TIMER_PATH, render_timer(), mode=0o644)
    except SSHError as exc:
        raise WatchdogError(f"не удалось записать файлы: {exc}") from exc

    await ssh.run(f"mkdir -p {STATE_DIR}")
    await ssh.run("systemctl daemon-reload")
    await ssh.run(f"systemctl enable --now {TIMER_NAME}")

    if not await is_running(ssh):
        raise WatchdogError("таймер не запустился")
    logger.info("Сторож поставлен на ноду {}", node_name)
    return True
