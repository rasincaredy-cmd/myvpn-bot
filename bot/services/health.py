"""Тревоги о состоянии серверов (этап 2B спеки защиты).

Бот раз в HEALTH_INTERVAL_MINUTES обходит серверы по SSH, снимает показания
и шлёт админам сообщение, только когда что-то реально сломалось. Порог у
каждой тревоги подобран так, чтобы сообщение означало «иди чини», а не
«прими к сведению»: тревоги, которые приходят зря, перестают читать, и это
хуже, чем не иметь их вовсе.

Порог соседа (steal) — 10%, а не 20%, как было записано при проектировании:
замер истории на боевой ноде 10.08.2026 дал средний steal 2.17→3.20% при
пике 14.31%, то есть порог 20% не сработал бы никогда.

Против спама три правила: одна проблема — одно сообщение; повтор той же
проблемы не чаще раза в час; когда отпустило — обязательное сообщение об
этом, иначе админ не знает, чинилось оно само или до сих пор висит.

Состояние тревог лежит в файле рядом с БД, а не в памяти: рестарт бота не
должен приводить к повторной пачке сообщений о том, что и так известно.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from loguru import logger

from bot.config import settings
from bot.texts import ui

_STATE_FILE = settings.data_dir / "health_state.json"

# ── Пороги ───────────────────────────────────────────────────────────────────

DISK_FREE_MIN_PCT = 15      # спека: меньше 15% свободного — тревога
RAM_FREE_MIN_PCT = 10       # памяти на исходе
STEAL_ALERT_PCT = 10.0      # доля процессора, отъедаемая соседом
UDP_ERR_ALERT = 500         # прирост потерь UDP между замерами

# Сертификат приёма оплат живёт 160 часов, продление начинается, когда
# осталась треть, — в здоровом цикле остаток не падает ниже ~53 часов. Порог
# 36 стоит заметно ниже этого дна (тревога не придёт зря) и при этом даёт
# полтора дня на починку. Взят с крови: сертификат от 11.08.2026 протух 18.08
# и три дня никто не знал, потому что следить было нечем.
CERT_WARN_HOURS = 36

REPEAT_AFTER = timedelta(hours=1)

# Уровни нужны только для значка: чинить всё равно надо всё.
LEVEL_ICON = {"crit": "🔴", "warn": "🟠"}


# ── Снимок показаний ─────────────────────────────────────────────────────────

@dataclass(slots=True)
class Snapshot:
    server_id: int
    server_name: str
    services: dict[str, str] = field(default_factory=dict)  # юнит → is-active
    disk_free_pct: int = 100
    ram_free_pct: int = 100
    oom_kills: int = 0
    steal_recent: float = 0.0    # среднее за последние ~10 минут
    steal_today: float = 0.0     # среднее за сегодня
    steal_yesterday: float = 0.0
    udp_errors: int = 0          # InErrors + RcvbufErrors, счётчик с загрузки
    banned: tuple[str, ...] = ()
    own_ip: str = ""
    manager_ip: str = ""   # адрес, с которого пришёл бот
    wdtt_sha: str = ""     # отпечаток программы резервного подключения


_SECTION_RE = re.compile(r"^---([A-Z0-9]+)---$")


def parse_sections(out: str) -> dict[str, list[str]]:
    """Разбирает составной вывод в секции. Пустые секции сохраняются — их
    отсутствие и пустота это разные вещи: «fail2ban не установлен» против
    «забаненных нет»."""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in out.splitlines():
        stripped = line.strip()
        match = _SECTION_RE.match(stripped)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
        elif current and stripped:
            sections[current].append(stripped)
    return sections


def _steal_from_sar(lines: list[str]) -> float:
    """Среднее %steal по строкам `sar -u`.

    Колонку берём предпоследней с конца, а не по номеру: при 12-часовом
    формате времени строка начинается двумя полями («07:42:12 AM»), и любой
    отсчёт слева съезжает. С конца порядок фиксирован: … %steal %idle.
    """
    values: list[float] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            values.append(float(parts[-2].replace(",", ".")))
        except ValueError:
            continue  # заголовок «CPU %user …» и прочий текст
    return round(sum(values) / len(values), 2) if values else 0.0


def _udp_errors_from_snmp(lines: list[str]) -> int:
    """InErrors + RcvbufErrors из /proc/net/snmp.

    Позиции полей берём из строки-заголовка, а не хардкодом: набор счётчиков
    у Udp менялся между версиями ядра (IgnoredMulti, MemErrors появились
    позже), и жёсткие индексы однажды начнут считать не то.
    """
    header: list[str] = []
    for line in lines:
        parts = line.split()
        if not parts or parts[0] != "Udp:":
            continue
        if header and parts[1].lstrip("-").isdigit():
            total = 0
            for name in ("InErrors", "RcvbufErrors"):
                if name in header:
                    try:
                        total += int(parts[header.index(name)])
                    except (IndexError, ValueError):
                        pass
            return total
        header = parts
    return 0


def _pct_int(raw: str) -> int:
    try:
        return int(float(raw.strip().rstrip("%")))
    except ValueError:
        return -1


def build_snapshot(
    server_id: int, server_name: str, out: str, *, units: list[str]
) -> Snapshot:
    """Собирает снимок из вывода `probe_command`.

    Всё, что не разобралось, остаётся в безопасном значении по умолчанию:
    сломанный парсер обязан молчать, а не будить админа ночью выдуманной
    аварией.
    """
    sec = parse_sections(out)
    snap = Snapshot(server_id=server_id, server_name=server_name)

    for line in sec.get("SERVICES", []):
        parts = line.split()
        if len(parts) == 2:
            snap.services[parts[0]] = parts[1]
    for unit in units:                      # юнит не ответил вовсе — неизвестно
        snap.services.setdefault(unit, "unknown")

    disk = sec.get("DISK", [])
    if disk:
        used = _pct_int(disk[0])
        if used >= 0:
            snap.disk_free_pct = 100 - used

    ram = sec.get("RAM", [])
    if ram:
        parts = ram[0].split()
        try:
            total, available = int(parts[0]), int(parts[1])
            if total > 0:
                snap.ram_free_pct = round(available / total * 100)
        except (IndexError, ValueError):
            pass

    oom = sec.get("OOM", [])
    if oom:
        try:
            snap.oom_kills = int(oom[0])
        except ValueError:
            pass

    snap.steal_recent = _steal_from_sar(sec.get("STEAL", []))
    snap.steal_today = _steal_from_sar(sec.get("STEALTODAY", []))
    snap.steal_yesterday = _steal_from_sar(sec.get("STEALPREV", []))
    snap.udp_errors = _udp_errors_from_snmp(sec.get("SNMP", []))

    banned: list[str] = []
    for line in sec.get("BAN", []):
        if "Banned IP list" in line:
            banned = re.findall(r"\d+\.\d+\.\d+\.\d+", line)
    snap.banned = tuple(banned)

    own = sec.get("OWNIP", [])
    snap.own_ip = own[0] if own else ""
    mine = [ln.strip() for ln in sec.get("MYIP", []) if ln.strip()]
    snap.manager_ip = mine[0] if mine else ""
    binsha = [ln.strip() for ln in sec.get("WDTTBIN", []) if ln.strip()]
    snap.wdtt_sha = binsha[0] if binsha else ""
    return snap


# Средние за сегодня и за вчера — общий кусок для тревог и для экрана
# сервера. Имя вчерашнего файла sysstat зависит от версии пакета: свежие
# пишут sa20260810, старые — sa10 (день месяца). Пробуем оба: на боевой ноде
# это sa20260810, и жёсткий выбор одного формата ломает динамику молча —
# секция просто приходит пустой, и никто об этом не узнаёт.
_STEAL_HISTORY_PROBE = (
    "echo '---STEALTODAY---'; "
    "LC_ALL=C sar -u 2>/dev/null | grep '^Average' || true; "
    "echo '---STEALPREV---'; "
    "PREV=/var/log/sysstat/sa$(date -d yesterday +%Y%m%d); "
    "[ -f \"$PREV\" ] || PREV=/var/log/sysstat/sa$(date -d yesterday +%d); "
    "LC_ALL=C sar -u -f \"$PREV\" 2>/dev/null | grep '^Average' || true"
)


def probe_command(units: list[str]) -> str:
    """Одна составная команда на весь снимок: каждое лишнее SSH-подключение —
    это ещё одна попытка входа в журнале и ещё один повод банилке нервничать.

    Всё завёрнуто в `|| true`: сервер без sysstat или без банилки обязан
    отдать остальные секции, а не оборваться на первой отсутствующей команде.
    """
    unit_probe = "; ".join(
        f'echo "{u} $(systemctl is-active {u} 2>/dev/null)"' for u in units
    )
    return (
        "echo '---SERVICES---'; " + (unit_probe + "; " if unit_probe else "") +
        "echo '---DISK---'; df -P / | awk 'NR==2{print $5}'; "
        "echo '---RAM---'; free -m | awk 'NR==2{print $2, $7}'; "
        "echo '---OOM---'; "
        "journalctl -k --since '-10 min' 2>/dev/null | grep -ci 'out of memory' || true; "
        # Шаг сбора sysstat — 2 минуты, поэтому шесть последних отсчётов это и
        # есть «дольше 10 минут» из спеки. Строку Average отбрасываем: она про
        # сутки, а не про последние минуты.
        "echo '---STEAL---'; "
        "LC_ALL=C sar -u 2>/dev/null | grep -v Average | tail -6 || true; "
        + _STEAL_HISTORY_PROBE + "; "
        "echo '---SNMP---'; grep -A1 '^Udp:' /proc/net/snmp || true; "
        "echo '---BAN---'; fail2ban-client status sshd 2>/dev/null "
        "| grep -E 'Currently banned|Banned IP list' || true; "
        "echo '---OWNIP---'; ip route get 1.1.1.1 2>/dev/null "
        "| awk '{for (i = 1; i <= NF; i++) if ($i == \"src\") { print $(i + 1); exit }}'; "
        # Адрес, с которого пришёл САМ БОТ. Он и есть тот, чей бан отрезает нас
        # от сервера: бот живёт на отдельной машине, а не на ноде. Раньше
        # тревога искала в бане адрес самой ноды — на первом сервере они
        # совпадали, и слепота была незаметна (аудит 20.08.2026).
        "echo '---MYIP---'; echo \"${SSH_CONNECTION%% *}\"; "
        # Отпечаток программы резервного подключения — в тот же снимок, чтобы
        # заметить отставшую ноду без отдельного захода по SSH. Сравнивать не
        # с чем прямо здесь: эталон лежит на машине бота, поэтому сравнение —
        # в run(), а сюда попадает только факт.
        f"echo '---WDTTBIN---'; sha256sum {settings.wdtt_binary_path} "
        "2>/dev/null | cut -d' ' -f1 || true"
    )


def wdtt_version_alert(server, snap: Snapshot) -> "Alert | None":
    """Нода отстала по версии программы резервного подключения.

    Отдельно от `evaluate`, потому что сравнивать надо с тем, чего в снимке
    нет: эталон лежит на машине бота. Тревога мягкая — «warn»: отстающая нода
    работает, просто не тем, чем остальные, и разъезд версий иначе не видно
    ниоткуда. Отпустит сама после обновления: ключ стабильный, а `decide`
    шлёт отбой, когда проблема исчезла.
    """
    if not getattr(server, "wdtt_enabled", False) or not snap.wdtt_sha:
        return None
    from bot.services.wdtt_update import reference_sha256

    ref = reference_sha256()
    if not ref or snap.wdtt_sha == ref:
        return None
    return Alert(
        key=f"{server.id}:wdttver",
        level="warn",
        title="Резервное подключение: версия отстаёт",
        detail=(
            f"На ноде <code>{snap.wdtt_sha[:8]}</code>, эталон "
            f"<code>{ref[:8]}</code>. Обновить: «👮 Админ-панель» → "
            "«⚡ Версии обхода»."  # wording: ok — экран админа
        ),
    )


# ── Оценка: что из этого авария ──────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class Alert:
    key: str      # стабильный ключ проблемы: по нему считается повтор и отбой
    level: str    # crit | warn
    title: str
    detail: str


# Человеческие имена служб: админ читает «VPN упал», а не имя юнита systemd.
def _service_title(unit: str) -> str:
    if unit.startswith("awg-quick@"):
        return "VPN"
    if unit == "wdtt":
        return "резервное подключение"
    if unit == "fail2ban":
        return "защита от перебора"
    return unit


def evaluate(snap: Snapshot, prev_udp_errors: int | None) -> list[Alert]:
    """Список проблем на сервере. Пусто — всё в порядке.

    Порядок важен: первым идёт то, из-за чего надо вставать ночью.
    """
    alerts: list[Alert] = []
    sid = snap.server_id

    # 1. Бот забанен собственной банилкой — самая важная тревога из спеки:
    # доступа к серверу нет ни у бота, ни (тем же путём) у админа.
    # Проверяем ОБА адреса: свой у ноды и тот, с которого приходит бот. Это
    # разные машины — бот живёт отдельно. Раньше сверялся только адрес ноды, и
    # на первом сервере они совпадали, поэтому слепота не проявлялась: на любой
    # второй ноде бан бота эта тревога поймать не могла в принципе.
    for ip, who in ((snap.manager_ip, "бота"), (snap.own_ip, "самого сервера")):
        if not ip or ip not in snap.banned:
            continue
        alerts.append(Alert(
            key=f"{sid}:selfban:{ip}", level="crit",
            title="Банилка забанила своих",
            detail=(f"Адрес {ip} ({who}) попал в бан — подключения с него "
                    "больше не проходят.\n"
                    f"Снять: <code>fail2ban-client set sshd unbanip {ip}</code>"),
        ))
        break   # хватит одной тревоги: лечение одинаковое

    # 2. Упавшие службы. «unknown» — тоже отказ: сервер не ответил на вопрос.
    for unit, state in sorted(snap.services.items()):
        if state != "active":
            alerts.append(Alert(
                key=f"{sid}:svc:{unit}", level="crit",
                title=f"Не работает: {_service_title(unit)}",
                detail=(f"<code>{unit}</code> — состояние «{state}».\n"
                        f"Поднять: <code>systemctl start {unit}</code>"),
            ))

    # 3. Диск. Кончившийся диск кладёт сервер целиком, поэтому crit.
    if snap.disk_free_pct < DISK_FREE_MIN_PCT:
        alerts.append(Alert(
            key=f"{sid}:disk", level="crit",
            title="Заканчивается место на диске",
            detail=f"Свободно {snap.disk_free_pct}% (порог {DISK_FREE_MIN_PCT}%).",
        ))

    # 4. Память и убийства процессов.
    if snap.oom_kills > 0:
        alerts.append(Alert(
            key=f"{sid}:oom", level="crit",
            title="Система убивает процессы из-за нехватки памяти",
            detail=f"За последние 10 минут: {snap.oom_kills}.",
        ))
    elif snap.ram_free_pct < RAM_FREE_MIN_PCT:
        alerts.append(Alert(
            key=f"{sid}:ram", level="warn",
            title="Память на исходе",
            detail=f"Свободно {snap.ram_free_pct}% (порог {RAM_FREE_MIN_PCT}%).",
        ))

    # 5. Сосед по железу. Порог 10%: обычный фон — 2–3%.
    if snap.steal_recent > STEAL_ALERT_PCT:
        alerts.append(Alert(
            key=f"{sid}:steal", level="warn",
            title="Сосед по железу забирает процессор",
            detail=(f"Последние 10 минут в среднем {snap.steal_recent}% "
                    f"(порог {STEAL_ALERT_PCT}%). Лечится только переездом "
                    "на другую машину — напиши хостеру."),
        ))
    # Тревоги на растущий тренд здесь сознательно НЕТ, хотя спека её просила.
    # При фоне 2–3% «сегодня в полтора раза хуже вчера» срабатывало бы на
    # колебаниях и ничего не требовало бы от админа — то есть было бы ровно
    # тем «прими к сведению», которое спека сама запрещает. Динамика
    # сегодня/вчера видна цифрой в экране сервера, где её смотрят осознанно.

    # 6. Потери UDP. Счётчик накопительный с загрузки, поэтому смотрим прирост
    # между замерами; после ребута счётчик меньше прошлого — прирост не считаем.
    if prev_udp_errors is not None and snap.udp_errors >= prev_udp_errors:
        delta = snap.udp_errors - prev_udp_errors
        if delta > UDP_ERR_ALERT:
            alerts.append(Alert(
                key=f"{sid}:udp", level="warn",
                title="Растут потери пакетов",
                detail=(f"С прошлой проверки потеряно {delta} UDP-пакетов "
                        f"(порог {UDP_ERR_ALERT}). У клиентов это рвущиеся "
                        "звонки и отваливающееся видео."),
            ))
    return alerts


# ── Сертификат приёма оплат ──────────────────────────────────────────────────
#
# Живёт не на ноде, а на самой машине бота, поэтому проверяется файлом, а не
# по SSH: лишний обход серверов ради локального файла — это ещё одна попытка
# входа в журнале и ещё один повод банилке нервничать.

def cert_path() -> Path | None:
    """Файл сертификата, за которым следим. None — следить не за чем.

    Обычно путь искать не надо: приёмник поднимается один, и в
    /etc/letsencrypt/live лежит ровно одна папка. Ищем сами, чтобы установка
    с нуля не требовала помнить про ещё одну настройку. Если папок несколько,
    угадывать не берёмся — тогда путь задаётся явно в WEBHOOK_CERT_PATH.
    """
    if settings.webhook_cert_path:
        return Path(settings.webhook_cert_path)
    if not settings.webhook_port:   # приёмник выключен — и сертификат не нужен
        return None
    try:
        found = sorted(Path("/etc/letsencrypt/live").glob("*/fullchain.pem"))
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


def cert_expiry(path: Path) -> datetime | None:
    """До какого момента сертификат действителен. None — прочитать не вышло.

    Молчим на любой беде с файлом: нечитаемый сертификат это повод разбираться
    руками, а не будить админа выдуманной аварией. Настоящую беду — что он
    протух — поймает Platega, и её ловит тревога ниже.
    """
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None
    # not_valid_after_utc появился в cryptography 42, на сервере стоит 49. На
    # телефоне, где гоняются тесты, — 41, там его нет. Порядок важен: старое
    # поле на свежей версии ругается в журнал устареванием, а бот трогает
    # сертификат каждые 10 минут. Оно отдаёт наивное время, но всегда в UTC.
    aware = getattr(cert, "not_valid_after_utc", None)
    if aware is not None:
        return aware
    naive = getattr(cert, "not_valid_after", None)
    return naive.replace(tzinfo=timezone.utc) if naive else None


def evaluate_cert(expires_at: datetime, now: datetime) -> Alert | None:
    """Тревога о сертификате. None — всё в порядке.

    Ключ один на оба уровня: «кончается» и «протух» — это одна и та же
    проблема в разных стадиях, и разные ключи прислали бы вторую пачку
    сообщений вместо повышения уровня.
    """
    left = expires_at - now
    if left <= timedelta(0):
        return Alert(
            key="cert", level="crit",
            title="Протух сертификат приёма оплат",
            detail=("Platega больше не может сообщить об оплате — она упрётся в "
                    "ошибку TLS. Деньги не потеряются, их доберёт поллинг, но "
                    "зачисление будет ждать до пяти минут.\n"
                    "Чинить: <code>certbot renew --cert-name &lt;IP&gt;</code>, "
                    "после — <code>certbot renew --dry-run</code> обязан "
                    "проходить НЕ останавливая nginx."),
        )
    if left <= timedelta(hours=CERT_WARN_HOURS):
        hours = int(left.total_seconds() // 3600)
        return Alert(
            key="cert", level="warn",
            title="Кончается сертификат приёма оплат",
            detail=(f"Осталось {hours} ч. В норме он продлевается сам и "
                    f"остаток не падает ниже {CERT_WARN_HOURS} ч — значит, "
                    "продление сломалось.\n"
                    "Проверить: <code>certbot renew --dry-run</code> "
                    "(обязан проходить НЕ останавливая nginx)."),
        )
    return None


# ── Антиспам ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    # OSError, а не только FileNotFoundError: нечитаемый файл состояния не
    # должен останавливать тревоги — хуже потерять историю, чем аварию.
    try:
        return json.loads(_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    except OSError as exc:  # не смогли записать — тревоги важнее состояния
        logger.warning("Не удалось сохранить состояние тревог: {}", exc)


def decide(
    state: dict, alerts: list[Alert], now: datetime
) -> tuple[list[Alert], list[str]]:
    """Что отправить: (новые/повторные тревоги, ключи отпустивших проблем).

    Состояние правится на месте — вызывающий сохраняет его после отправки,
    чтобы упавшая отправка не пометила тревогу как доставленную.
    """
    active = state.setdefault("active", {})
    to_send: list[Alert] = []
    for alert in alerts:
        seen = active.get(alert.key)
        if seen is None:
            active[alert.key] = {"since": now.isoformat(), "last": now.isoformat(),
                                 "title": alert.title}
            to_send.append(alert)
            continue
        try:
            last = datetime.fromisoformat(seen["last"])
        except (KeyError, ValueError):
            last = now - REPEAT_AFTER
        if now - last >= REPEAT_AFTER:
            seen["last"] = now.isoformat()
            to_send.append(alert)

    current = {a.key for a in alerts}
    resolved = [key for key in active if key not in current]
    return to_send, resolved


# ── Обход серверов и отправка ────────────────────────────────────────────────

def units_for(server) -> list[str]:
    """Какие службы обязаны работать на этом сервере.

    Бот в список не входит: он живёт на одном из серверов и о собственной
    смерти сообщить всё равно не сможет (известное ограничение спеки —
    закроется, когда ноды начнут проверять друг друга). Резервное подключение
    проверяем только там, где оно включено: на остальных его нет и «не
    работает» было бы враньём.
    """
    units = [f"awg-quick@{server.wg_interface}", "fail2ban"]
    if server.wdtt_enabled:
        units.append("wdtt")
    return units


async def _notify_admins(text: str) -> None:
    from bot.loader import bot

    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("Тревога админу {} не ушла: {}", admin_id, exc)


def format_alert(server_name: str, alert: Alert) -> str:
    icon = LEVEL_ICON.get(alert.level, "🟠")
    return f"{icon} <b>{alert.title}</b>\n🖥 {server_name}\n\n{alert.detail}"


async def run_round(session) -> None:
    """Один обход всех серверов. Исключения наружу не выпускаем: тик
    планировщика не должен падать из-за недоступного сервера."""
    from bot.db import repo
    from bot.services.ssh import SSHClient

    state = _load_state()
    counters = state.setdefault("udp", {})
    now = datetime.now(timezone.utc)
    collected: list[Alert] = []
    names: dict[str, str] = {}

    from bot.db.models import ServerStatus

    for server in await repo.list_all_servers(session):
        # Недоустановленный или сломанный при установке сервер клиентов не
        # обслуживает — тревожить о нём каждый час незачем, он и так на виду
        # в мастере.
        if server.status != ServerStatus.READY:
            continue
        units = units_for(server)
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                res = await ssh.run(probe_command(units))
        # Ловим всё: asyncssh кидает не только SSHError, а тревоги не должны
        # прекращаться из-за одного сервера с испорченными кредами.
        except Exception as exc:
            # Сервер не отвечает по SSH — это само по себе тревога, но не
            # такая же, как «служба упала»: сервер может быть просто
            # перезагружен. Ключ отдельный, отпустит сам при следующем тике.
            collected.append(Alert(
                key=f"{server.id}:ssh", level="crit",
                title="Сервер не отвечает",
                detail=f"Не удалось подключиться по SSH: <code>{ui.safe(exc)}</code>",
            ))
            names[f"{server.id}:ssh"] = server.name
            continue

        snap = build_snapshot(server.id, server.name, res.stdout, units=units)
        prev = counters.get(str(server.id))
        alerts = evaluate(snap, prev)
        drift = wdtt_version_alert(server, snap)
        if drift is not None:
            alerts.append(drift)
        counters[str(server.id)] = snap.udp_errors
        for alert in alerts:
            names[alert.key] = server.name
        collected.extend(alerts)

    # Сертификат приёмника оплат живёт на машине бота, а не на ноде, поэтому
    # он вне цикла по серверам. Не прочитался — молчим: см. cert_expiry.
    path = cert_path()
    if path is not None:
        expires_at = cert_expiry(path)
        if expires_at is not None:
            cert_alert = evaluate_cert(expires_at, now)
            if cert_alert is not None:
                names[cert_alert.key] = "приём оплат"
                collected.append(cert_alert)

    active = state.setdefault("active", {})
    titles = {key: rec.get("title", key) for key, rec in active.items()}
    server_of = {key: rec.get("server", "") for key, rec in active.items()}

    to_send, resolved = decide(state, collected, now)
    for alert in to_send:
        active[alert.key]["server"] = names.get(alert.key, "")
        await _notify_admins(format_alert(names.get(alert.key, "?"), alert))
    for key in resolved:
        await _notify_admins(
            f"✅ <b>Отпустило</b>\n🖥 {server_of.get(key) or '?'}\n\n"
            f"«{titles.get(key, key)}» — больше не наблюдается."
        )
        active.pop(key, None)

    state["last_run"] = now.isoformat()
    _save_state(state)


# ── Метрики поверх sysstat: то, чего стандартный сбор не знает ───────────────

@dataclass(slots=True)
class Extras:
    queues: tuple[tuple[str, int], ...] = ()   # (сокет, длина очереди приёма)
    udp_errors: int = 0
    banned_now: int = 0
    banned_total: int = 0
    steal_today: float = 0.0
    steal_yesterday: float = 0.0


def extras_command() -> str:
    """Очереди приёма по сокетам, потери UDP и счётчики банилки.

    Очередь приёма показываем по каждому сокету отдельно: общий счётчик
    потерь говорит, что пакеты теряются, но не говорит — у кого именно.
    В `ss -lunH` поле Recv-Q второе (Netid не печатается, протокол задан
    флагом `-u`) — отсчёт от начала строки тут проверен на живом сервере.
    """
    return (
        "echo '---QUEUE---'; ss -lunH 2>/dev/null | awk '$2 > 0 {print $4, $2}'; "
        "echo '---SNMP---'; grep -A1 '^Udp:' /proc/net/snmp || true; "
        "echo '---BAN---'; fail2ban-client status sshd 2>/dev/null "
        "| grep -E 'Currently banned|Total banned' || true; "
        + _STEAL_HISTORY_PROBE
    )


def parse_extras(out: str) -> Extras:
    sec = parse_sections(out)
    queues: list[tuple[str, int]] = []
    for line in sec.get("QUEUE", []):
        parts = line.split()
        if len(parts) >= 2:
            try:
                queues.append((parts[0], int(parts[1])))
            except ValueError:
                continue

    now = total = 0
    for line in sec.get("BAN", []):
        numbers = re.findall(r"\d+", line)
        if not numbers:
            continue
        if "Currently" in line:
            now = int(numbers[-1])
        elif "Total" in line:
            total = int(numbers[-1])

    return Extras(
        queues=tuple(queues),
        udp_errors=_udp_errors_from_snmp(sec.get("SNMP", [])),
        banned_now=now,
        banned_total=total,
        steal_today=_steal_from_sar(sec.get("STEALTODAY", [])),
        steal_yesterday=_steal_from_sar(sec.get("STEALPREV", [])),
    )


def format_extras(ex: Extras) -> str:
    """Дополнение к экрану «Состояние»."""
    if ex.queues:
        stuck = ", ".join(f"{addr} ({n})" for addr, n in ex.queues)
        queue_line = f"⚠️ <b>Очередь приёма:</b> {stuck}"
    else:
        queue_line = "✅ <b>Очередь приёма:</b> пусто"
    # Динамика соседа — цифрой здесь, а не тревогой в телеграм: при фоне
    # 2–3% сообщение «стало хуже в полтора раза» ничего от админа не требует,
    # а смотрят на неё осознанно — когда разбираются с жалобой на лаги.
    if ex.steal_today or ex.steal_yesterday:
        trend = "↑" if ex.steal_today > ex.steal_yesterday else "↓"
        steal_line = (
            f"\n🏚 <b>Сосед по железу:</b> сегодня {ex.steal_today}% "
            f"{trend} вчера {ex.steal_yesterday}%"
        )
    else:
        steal_line = ""

    return (
        f"{queue_line}\n"
        f"📉 <b>Потери UDP с загрузки:</b> {ex.udp_errors}\n"
        f"🚫 <b>Забанено:</b> сейчас {ex.banned_now}, всего {ex.banned_total}"
        f"{steal_line}"
    )


# ── Аналитика канала ─────────────────────────────────────────────────────────

# Потолок хостера по трафику в месяц (Fair Use). Процессор и память узким
# местом не были ни разу — упрёмся мы именно в трафик, поэтому считаем темп
# и показываем, сколько от потолка съедено.
FAIR_USE_TB = 16
_MONTH_SECONDS = 30 * 86400


@dataclass(slots=True)
class Channel:
    iface: str = "?"
    avg_rx_kbs: float = 0.0
    avg_tx_kbs: float = 0.0
    peak_rx_kbs: float = 0.0
    peak_tx_kbs: float = 0.0
    samples: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    uptime_sec: float = 0.0

    @property
    def total_bytes(self) -> int:
        return self.rx_bytes + self.tx_bytes

    @property
    def monthly_forecast_bytes(self) -> int:
        """Сколько натечёт за месяц при нынешнем темпе. Считаем от счётчиков
        интерфейса с момента загрузки, а не от sysstat: счётчики точные, а
        суточные средние sar пришлось бы домножать на неполные интервалы."""
        if self.uptime_sec <= 0:
            return 0
        return int(self.total_bytes / self.uptime_sec * _MONTH_SECONDS)


def channel_command() -> str:
    """Скорость за сегодня (sysstat) + объём с момента загрузки (счётчики
    интерфейса). Внешний интерфейс определяем маршрутом наружу, а не по имени:
    он бывает и ens3, и eth0.

    Строки `Average:` из подсчёта исключены признаком «в первом поле есть
    двоеточие» (время) — иначе итоговая строка попадала бы в выборку второй
    раз и тянула среднее.
    """
    return (
        "IF=$(ip route get 1.1.1.1 2>/dev/null "
        "| awk '{for (i = 1; i <= NF; i++) if ($i == \"dev\") { print $(i + 1); exit }}'); "
        "echo '---IFACE---'; echo \"$IF\"; "
        "echo '---SPEED---'; LC_ALL=C sar -n DEV 2>/dev/null | awk -v i=\"$IF\" "
        "'$1 ~ /:/ && $2 == i {rx += $5; tx += $6; n++; "
        "if ($5 > mx) mx = $5; if ($6 > mt) mt = $6} "
        "END {if (n) printf \"%.1f %.1f %.1f %.1f %d\\n\", rx / n, tx / n, mx, mt, n}' || true; "
        "echo '---BYTES---'; "
        "cat /sys/class/net/\"$IF\"/statistics/rx_bytes "
        "/sys/class/net/\"$IF\"/statistics/tx_bytes 2>/dev/null; "
        "echo '---UPTIME---'; cut -d' ' -f1 /proc/uptime"
    )


def parse_channel(out: str) -> Channel:
    sec = parse_sections(out)
    ch = Channel()
    iface = sec.get("IFACE", [])
    if iface:
        ch.iface = iface[0]

    speed = sec.get("SPEED", [])
    if speed:
        parts = speed[0].split()
        try:
            ch.avg_rx_kbs = float(parts[0])
            ch.avg_tx_kbs = float(parts[1])
            ch.peak_rx_kbs = float(parts[2])
            ch.peak_tx_kbs = float(parts[3])
            ch.samples = int(parts[4])
        except (IndexError, ValueError):
            pass

    data = sec.get("BYTES", [])
    if len(data) >= 2:
        try:
            ch.rx_bytes, ch.tx_bytes = int(data[0]), int(data[1])
        except ValueError:
            pass

    up = sec.get("UPTIME", [])
    if up:
        try:
            ch.uptime_sec = float(up[0])
        except ValueError:
            pass
    return ch


def _mbits(kbs: float) -> float:
    """Килобайты в секунду → мегабиты в секунду: скорость канала хостер
    считает в мегабитах, и сравнивать надо в тех же единицах."""
    return round(kbs * 8 / 1000, 1)


def format_channel(server_name: str, ch: Channel) -> str:
    from bot.services.amnezia import fmt_bytes

    forecast = ch.monthly_forecast_bytes
    cap = FAIR_USE_TB * 1024 ** 4
    pct = round(forecast / cap * 100) if cap else 0
    days = ch.uptime_sec / 86400

    if not ch.samples:
        speed_block = "Истории скорости пока нет — сбор статистики только начался."
    else:
        speed_block = (
            f"⬇️ <b>Приём:</b> в среднем {_mbits(ch.avg_rx_kbs)} Мбит/с, "
            f"пик {_mbits(ch.peak_rx_kbs)} Мбит/с\n"
            f"⬆️ <b>Отдача:</b> в среднем {_mbits(ch.avg_tx_kbs)} Мбит/с, "
            f"пик {_mbits(ch.peak_tx_kbs)} Мбит/с\n"
            f"<i>За сегодня, {ch.samples} замеров</i>"
        )

    return (
        f"📈 <b>Канал — {server_name}</b>\n"
        f"<code>{ch.iface}</code>\n\n"
        f"{speed_block}\n\n"
        f"📦 <b>Прокачано:</b> {fmt_bytes(ch.total_bytes)} за {days:.1f} дн\n"
        f"📅 <b>Темп:</b> {fmt_bytes(forecast)} в месяц — "
        f"{pct}% от потолка хостера ({FAIR_USE_TB} ТБ)"
    )


def due(now: datetime) -> bool:
    """Пора ли проверять. 0 в настройке выключает тревоги совсем."""
    interval = settings.health_interval_minutes
    if interval <= 0:
        return False
    try:
        last = datetime.fromisoformat(_load_state()["last_run"])
    except (KeyError, ValueError):
        return True
    return (now - last) >= timedelta(minutes=interval)
