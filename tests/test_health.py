"""Тревоги о состоянии серверов (этап 2B).

Фикстура PROBE — настоящий вывод probe_command с боевой ноды 11.08.2026,
а не придуманный: форматы sar/fail2ban/snmp отличаются между версиями, и
тест на выдуманном тексте доказывает только то, что парсер понимает сам себя.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.services import health

PROBE = """---SERVICES---
awg-quick@awg0 active
fail2ban active
wdtt active
---DISK---
15%
---RAM---
1963 1441
---OOM---
0
---STEAL---
07:42:12        all      1.04      0.00      0.76      0.16      2.46     95.59
07:44:26        all      0.32      0.00      0.71      0.05      2.14     96.78
07:46:07        all      0.50      0.00      0.92      0.04      2.07     96.47
07:48:03        all      1.20      0.00      1.64      0.25      2.69     94.23
07:50:26        all      0.37      0.00      0.67      0.13      2.53     96.29
07:52:26        all      0.89      0.00      3.19      0.12      3.02     92.79
---STEALTODAY---
Average:        all      0.76      0.03      0.81      0.12      1.51     96.76
---STEALPREV---
Average:        all      1.38      0.00      1.78      0.10      2.49     94.25
---SNMP---
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti MemErrors
Udp: 22601563 9427 1148 6775259 1146 229 2 0 0
UdpLite: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti MemErrors
---BAN---
   |- Currently banned:\t0
   `- Banned IP list:\t
---OWNIP---
31.77.157.162
"""

UNITS = ["awg-quick@awg0", "fail2ban", "wdtt"]
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def snap(**over) -> health.Snapshot:
    """Снимок здоровой боевой ноды, в который тест портит одно поле."""
    s = health.build_snapshot(1, "Нидерланды", PROBE, units=UNITS)
    for key, value in over.items():
        setattr(s, key, value)
    return s


# ── Разбор вывода ────────────────────────────────────────────────────────────

def test_snapshot_from_real_probe():
    s = health.build_snapshot(1, "Нидерланды", PROBE, units=UNITS)
    assert s.services == {"awg-quick@awg0": "active", "fail2ban": "active",
                          "wdtt": "active"}
    assert s.disk_free_pct == 85          # df показал 15% занято
    assert s.ram_free_pct == 73           # 1441 доступно из 1963
    assert s.oom_kills == 0
    assert s.steal_recent == 2.48         # среднее шести последних отсчётов
    assert s.steal_today == 1.51
    assert s.steal_yesterday == 2.49
    assert s.udp_errors == 1148 + 1146    # InErrors + RcvbufErrors
    assert s.banned == ()
    assert s.own_ip == "31.77.157.162"


def test_healthy_server_is_silent():
    assert health.evaluate(snap(), prev_udp_errors=2294) == []


def test_steal_column_survives_12_hour_clock():
    """При 12-часовом формате времени строка начинается двумя полями
    («07:42:12 AM»), и отсчёт колонок слева съезжает — берём с конца."""
    lines = ["07:42:12 AM     all      1.04      0.00      0.76      0.16"
             "      9.90     95.59"]
    assert health._steal_from_sar(lines) == 9.9


def test_steal_ignores_header_lines():
    lines = ["12:00:01        CPU     %user     %nice   %system   %iowait"
             "    %steal     %idle",
             "07:42:12        all      1.04      0.00      0.76      0.16"
             "      4.00     95.59"]
    assert health._steal_from_sar(lines) == 4.0


def test_udp_errors_follow_header_not_position():
    """У Udp набор счётчиков менялся между версиями ядра: считаем по именам
    из заголовка, иначе однажды начнём складывать не те колонки."""
    lines = ["Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors",
             "Udp: 100 5 7 200 9"]
    assert health._udp_errors_from_snmp(lines) == 16


def test_banned_ips_are_parsed():
    probe = PROBE.replace(
        "`- Banned IP list:\t", "`- Banned IP list:\t1.2.3.4 31.77.157.162"
    )
    s = health.build_snapshot(1, "Нидерланды", probe, units=UNITS)
    assert s.banned == ("1.2.3.4", "31.77.157.162")


def test_garbage_output_does_not_invent_alerts():
    """Сломанный парсер обязан молчать, а не будить админа выдуманной
    аварией: все поля остаются в безопасных значениях."""
    s = health.build_snapshot(1, "Х", "полная белиберда\nбез секций", units=[])
    assert health.evaluate(s, prev_udp_errors=None) == []


def test_unit_without_answer_counts_as_down():
    """Юнита нет в выводе вовсе — сервер не ответил на вопрос, и это отказ,
    а не «всё хорошо»."""
    s = health.build_snapshot(1, "Х", "---SERVICES---\n", units=["wdtt"])
    keys = [a.key for a in health.evaluate(s, None)]
    assert "1:svc:wdtt" in keys


# ── Пороги ───────────────────────────────────────────────────────────────────

def test_service_down_is_critical():
    alerts = health.evaluate(snap(services={"awg-quick@awg0": "failed"}), 2294)
    assert [(a.key, a.level) for a in alerts] == [("1:svc:awg-quick@awg0", "crit")]
    assert "VPN" in alerts[0].title       # админ читает «VPN», не имя юнита


def test_self_ban_is_first_alert():
    """Самая важная тревога спеки идёт первой: без доступа по SSH всё
    остальное чинить нечем."""
    alerts = health.evaluate(
        snap(banned=("31.77.157.162",), own_ip="31.77.157.162",
             services={"wdtt": "failed"}), 2294
    )
    assert alerts[0].key.startswith("1:selfban")


def test_ban_of_the_bot_is_caught_on_a_foreign_node():
    """Слепая зона, найденная аудитом 20.08.2026.

    Тревога сверяла с баном ТОЛЬКО адрес самой ноды. Бот живёт на отдельной
    машине, и на первой ноде адреса совпадали — поэтому слепота не проявлялась.
    На немецкой ноде бан бота эта тревога поймать не могла в принципе: её
    собственный адрес чист, а отрезан от сервера именно бот.
    """
    alerts = health.evaluate(
        snap(banned=("31.77.157.162",), own_ip="31.77.148.187",
             manager_ip="31.77.157.162"),
        2294,
    )
    assert alerts, "бан бота остался незамеченным"
    assert "31.77.157.162" in alerts[0].detail
    assert alerts[0].level == "crit"


def test_ban_of_a_stranger_is_not_an_alert():
    """Обычный перебор паролей с чужого адреса — это работа банилки, а не
    авария. На нодах таких банов сотни."""
    alerts = health.evaluate(
        snap(banned=("121.227.31.13",), own_ip="31.77.148.187",
             manager_ip="31.77.157.162"),
        2294,
    )
    assert alerts == []


def test_one_alert_even_if_both_addresses_banned():
    """Лечение одинаковое — два сообщения об одном и том же только шумят."""
    alerts = health.evaluate(
        snap(banned=("31.77.157.162", "31.77.148.187"),
             own_ip="31.77.148.187", manager_ip="31.77.157.162"),
        2294,
    )
    assert len([a for a in alerts if "selfban" in a.key]) == 1


def test_manager_ip_is_parsed_from_the_probe():
    """Адрес бота приходит из SSH_CONNECTION прямо в пробе."""
    probe = PROBE + "\n---MYIP---\n31.77.157.162"
    s = health.build_snapshot(1, "Германия", probe, units=UNITS)
    assert s.manager_ip == "31.77.157.162"


def test_missing_manager_ip_does_not_break_anything():
    """Старый сервер, где проба ещё без этой секции, обязан работать."""
    s = health.build_snapshot(1, "Нидерланды", PROBE, units=UNITS)
    assert s.manager_ip == ""
    assert health.evaluate(s, 2294) == []


def test_disk_threshold():
    assert health.evaluate(snap(disk_free_pct=14), 2294)[0].key == "1:disk"
    assert health.evaluate(snap(disk_free_pct=15), 2294) == []


def test_oom_wins_over_low_ram():
    """Убийства процессов и низкая память — одна беда; два сообщения о ней
    подряд это спам."""
    alerts = health.evaluate(snap(oom_kills=3, ram_free_pct=2), 2294)
    assert [a.key for a in alerts] == ["1:oom"]


def test_low_ram_alone():
    assert health.evaluate(snap(ram_free_pct=9), 2294)[0].key == "1:ram"


def test_steal_threshold_is_ten_percent():
    """Порог 20% из первой редакции спеки не сработал бы никогда: пик на
    боевой ноде — 14.31%."""
    assert health.evaluate(snap(steal_recent=14.31), 2294)[0].key == "1:steal"
    assert health.evaluate(snap(steal_recent=10.0), 2294) == []


def test_growing_trend_is_not_a_telegram_alert():
    """Решение 11.08.2026: тревоги на растущий тренд нет. При фоне 2–3%
    «сегодня в полтора раза хуже вчера» срабатывало бы на колебаниях и
    ничего не требовало бы от админа — то есть было бы ровно тем «прими к
    сведению», которое спека запрещает. Динамика видна цифрой в экране."""
    assert health.evaluate(snap(steal_today=9.0, steal_yesterday=5.0), 2294) == []


def test_udp_growth_alert():
    alerts = health.evaluate(snap(udp_errors=3000), prev_udp_errors=2000)
    assert alerts[0].key == "1:udp"


def test_udp_first_run_is_silent():
    """Первый замер сравнивать не с чем — счётчик накопительный с загрузки,
    и без предыдущего значения он означал бы «потеряно 2294 пакета»."""
    assert health.evaluate(snap(), prev_udp_errors=None) == []


def test_udp_counter_reset_after_reboot_is_silent():
    assert health.evaluate(snap(udp_errors=10), prev_udp_errors=999999) == []


# ── Антиспам ─────────────────────────────────────────────────────────────────

def alert(key: str = "1:disk") -> health.Alert:
    return health.Alert(key=key, level="crit", title="Диск", detail="…")


def test_first_alert_is_sent():
    state: dict = {}
    to_send, resolved = health.decide(state, [alert()], NOW)
    assert [a.key for a in to_send] == ["1:disk"]
    assert resolved == []


def test_same_problem_is_not_repeated_within_an_hour():
    state: dict = {}
    health.decide(state, [alert()], NOW)
    to_send, _ = health.decide(state, [alert()], NOW + timedelta(minutes=59))
    assert to_send == []


def test_same_problem_repeats_after_an_hour():
    state: dict = {}
    health.decide(state, [alert()], NOW)
    to_send, _ = health.decide(state, [alert()], NOW + timedelta(hours=1))
    assert [a.key for a in to_send] == ["1:disk"]


def test_resolved_problem_is_reported_once():
    """«Отпустило» обязательно: без него админ не знает, чинилось оно само
    или до сих пор висит."""
    state: dict = {}
    health.decide(state, [alert()], NOW)
    to_send, resolved = health.decide(state, [], NOW + timedelta(minutes=10))
    assert to_send == [] and resolved == ["1:disk"]


def test_state_survives_restart():
    """Состояние живёт в файле, а не в памяти: рестарт бота не должен
    приводить к повторной пачке сообщений о том, что и так известно."""
    state: dict = {}
    health.decide(state, [alert()], NOW)
    reloaded = {"active": dict(state["active"])}   # как после чтения файла
    to_send, _ = health.decide(reloaded, [alert()], NOW + timedelta(minutes=5))
    assert to_send == []


# ── Состав проверок ──────────────────────────────────────────────────────────

class FakeServer:
    def __init__(self, wdtt_enabled: bool):
        self.wg_interface = "awg0"
        self.wdtt_enabled = wdtt_enabled


def test_units_include_bypass_only_where_enabled():
    assert "wdtt" in health.units_for(FakeServer(True))
    assert "wdtt" not in health.units_for(FakeServer(False))


def test_bot_service_is_not_checked():
    """Бот живёт на одном из серверов и о собственной смерти сообщить не
    сможет — проверять его значит обещать несбыточное."""
    assert not any("myvpn" in u for u in health.units_for(FakeServer(True)))


# ── Канал ────────────────────────────────────────────────────────────────────

CHANNEL = """---IFACE---
ens3
---SPEED---
38.2 39.3 1429.7 1517.9 240
---BYTES---
62535610523
64990981255
---UPTIME---
569282.53
"""


def test_channel_parsed_from_real_output():
    ch = health.parse_channel(CHANNEL)
    assert ch.iface == "ens3"
    assert (ch.avg_rx_kbs, ch.avg_tx_kbs) == (38.2, 39.3)
    assert (ch.peak_rx_kbs, ch.peak_tx_kbs) == (1429.7, 1517.9)
    assert ch.samples == 240
    assert ch.total_bytes == 62535610523 + 64990981255


def test_channel_speed_shown_in_megabits():
    """Хостер меряет канал в мегабитах — сравнивать надо в тех же единицах."""
    assert health._mbits(1429.7) == 11.4


def test_monthly_forecast_from_uptime():
    """119 ГиБ за 6.6 суток → около 540 ГиБ в месяц, то есть проценты от
    потолка в 16 ТБ, а не близко к нему."""
    ch = health.parse_channel(CHANNEL)
    gib = ch.monthly_forecast_bytes / 1024 ** 3
    assert 520 < gib < 560
    assert "% от потолка" in health.format_channel("Нидерланды", ch)


def test_channel_survives_empty_history():
    """Сервер только что установлен: sysstat ещё ничего не собрал, делить
    на ноль нельзя, а сообщение всё равно должно быть осмысленным."""
    ch = health.parse_channel("---IFACE---\nens3\n---SPEED---\n---BYTES---\n"
                              "---UPTIME---\n0\n")
    text = health.format_channel("Новый", ch)
    assert "Истории скорости пока нет" in text
    assert ch.monthly_forecast_bytes == 0


@pytest.mark.parametrize("fmt", ["+%Y%m%d", "+%d"])
def test_probe_tries_both_sysstat_filename_formats(fmt: str):
    """Имя вчерашнего файла sysstat зависит от версии пакета: на боевой ноде
    это sa20260810, в старых — sa10. Жёсткий выбор одного формата молча
    ломает тревогу на тренд: секция просто приходит пустой."""
    assert fmt in health.probe_command(["wdtt"])


# ── Метрики поверх sysstat ───────────────────────────────────────────────────

EXTRAS = """---QUEUE---
0.0.0.0:585 4096
---SNMP---
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors
Udp: 22601563 9427 1148 6775259 1146 229
---BAN---
   |- Currently banned:\t2
   |- Total banned:\t29
"""


def test_extras_parsed():
    ex = health.parse_extras(EXTRAS)
    assert ex.queues == (("0.0.0.0:585", 4096),)
    assert ex.udp_errors == 1148 + 1146
    assert (ex.banned_now, ex.banned_total) == (2, 29)


def test_extras_queue_empty_is_not_a_problem():
    """Пустая очередь — норма, и выглядеть она должна как норма, а не как
    отсутствие данных."""
    ex = health.parse_extras("---QUEUE---\n---SNMP---\n---BAN---\n")
    assert "пусто" in health.format_extras(ex)


def test_extras_names_the_socket_that_piles_up():
    """Общий счётчик потерь говорит, что пакеты теряются, но не говорит —
    у кого. Сокет обязан быть виден в тексте."""
    assert "0.0.0.0:585" in health.format_extras(health.parse_extras(EXTRAS))


def test_steal_dynamics_shown_in_server_screen():
    """Цифру убрали из тревог, но не из виду: сравнение сегодня/вчера должно
    остаться на экране, иначе при жалобе на лаги снова нечего предъявить."""
    ex = health.parse_extras(
        "---STEALTODAY---\nAverage: all 0.76 0.03 0.81 0.12 4.20 96.76\n"
        "---STEALPREV---\nAverage: all 1.38 0.00 1.78 0.10 2.49 94.25\n"
    )
    assert (ex.steal_today, ex.steal_yesterday) == (4.2, 2.49)
    text = health.format_extras(ex)
    assert "4.2%" in text and "2.49%" in text and "↑" in text


def test_steal_dynamics_hidden_when_history_is_empty():
    """Свежий сервер: истории нет — строку не показываем, а не рисуем «0%»,
    как будто сосед измерен и его нет."""
    assert "Сосед" not in health.format_extras(health.parse_extras("---BAN---\n"))


# ── Сертификат приёма оплат (авария 21.08.2026) ──────────────────────────────
#
# Сертификат на IP живёт 160 часов и продлевается таймером. 11.08 его выпустили
# способом, который занимает 80-й порт, — а порт навсегда занят nginx. Первый
# выпуск прошёл (nginx тогда лежал), каждое продление потом молча падало,
# сертификат протух 18.08, и Platega три раза не смогла достучаться. Никто не
# заметил три дня. Эти тесты — чтобы следующий раз заметили за сутки.

# Настоящий наш сертификат от 21.08.2026: ECDSA, без домена, адрес в SAN.
# Придуманный доказал бы только то, что парсер понимает сам себя.
REAL_CERT = """\
-----BEGIN CERTIFICATE-----
MIIDTDCCAtGgAwIBAgISBXTSa1sQBvSbQy14fc4SrLDjMAoGCCqGSM49BAMDMDMx
CzAJBgNVBAYTAlVTMRYwFAYDVQQKEw1MZXQncyBFbmNyeXB0MQwwCgYDVQQDEwNZ
RTIwHhcNMjYwODIxMDczMDU5WhcNMjYwODI3MjMzMDU4WjAAMFkwEwYHKoZIzj0C
AQYIKoZIzj0DAQcDQgAEEzQHRnGHyl3uor8Y8zyG7dQrlupP1UbqbUulwr7MV64f
KV+fSmxFFQDn9qb/7dkX6F+9SCHzIbisyojMGyJPo6OCAfYwggHyMA4GA1UdDwEB
/wQEAwIHgDATBgNVHSUEDDAKBggrBgEFBQcDATAMBgNVHRMBAf8EAjAAMB8GA1Ud
IwQYMBaAFLlZ8o7PIvCG0zdI/3YUGLqC2FWHMDMGCCsGAQUFBwEBBCcwJTAjBggr
BgEFBQcwAoYXaHR0cDovL3llMi5pLmxlbmNyLm9yZy8wEgYDVR0RAQH/BAgwBocE
H02dojATBgNVHSAEDDAKMAgGBmeBDAECATAvBgNVHR8EKDAmMCSgIqAghh5odHRw
Oi8veWUyLmMubGVuY3Iub3JnLzEwMC5jcmwwggELBgorBgEEAdZ5AgQCBIH8BIH5
APcAdQDLOPcViXyEoURfW8Hd+8lu8ppZzUcKaQWFsMsUwxRY5wAAAaAjcH1OAAAE
AwBGMEQCICV32E/LSIUQhoEL56HWP97rA9YH+mA1XWsi9/Xc1mQbAiBZf+NVmO3m
zD4rwQ5yKzOHbe//b6BVni/7qxPB62bkjAB+ABqLnWsP/r+BtHk5xtIxCobW0QLU
8EbiGCyd419eJiXvAAABoCNwf94ACAAABQA22l8hBAMARzBFAiEAv0L8+EdgfVHh
nyamHELJg3e8F+lKxYHQJO2t2J75uOkCIGcBAfkrVw4ow+PuHyWYy82uzzyFMGQ0
snKHGVR1dz55MAoGCCqGSM49BAMDA2kAMGYCMQDIb6gNs1GWtJHKMfWqxtckrPlx
uP4AOsPhAbEc30vn5Jrceo7PhzWZspmJ+ymbgYQCMQDiUcALdowa9ervNajz3mnD
foZVUghlCgyxxGwB92bbFsHhEOqfl4yKXYi3GNWIcsI=
-----END CERTIFICATE-----
"""

REAL_CERT_EXPIRES = datetime(2026, 8, 27, 23, 30, 58, tzinfo=timezone.utc)


def test_expiry_read_from_real_certificate(tmp_path):
    path = tmp_path / "fullchain.pem"
    path.write_text(REAL_CERT)
    assert health.cert_expiry(path) == REAL_CERT_EXPIRES


def test_unreadable_certificate_is_silent(tmp_path):
    """Сломанный или отсутствующий файл — молчание, а не выдуманная авария."""
    assert health.cert_expiry(tmp_path / "нет-такого.pem") is None
    broken = tmp_path / "broken.pem"
    broken.write_text("это не сертификат")
    assert health.cert_expiry(broken) is None


def test_fresh_certificate_is_quiet():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    assert health.evaluate_cert(now + timedelta(days=6), now) is None


def test_normal_renewal_cycle_never_alerts():
    """Порог обязан молчать на здоровом цикле.

    Сертификат живёт 160 часов, продление начинается, когда осталась треть, —
    то есть в норме остаток НИКОГДА не опускается ниже ~53 часов. Порог должен
    стоять ниже этого дна, иначе тревога будет приходить каждый цикл и её
    перестанут читать.
    """
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    assert health.evaluate_cert(now + timedelta(hours=53), now) is None


def test_certificate_running_out_warns():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    alert = health.evaluate_cert(now + timedelta(hours=20), now)
    assert alert is not None
    assert alert.level == "warn"
    assert "20" in alert.detail   # сколько часов осталось — в сообщении


def test_expired_certificate_is_critical():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    alert = health.evaluate_cert(now - timedelta(hours=1), now)
    assert alert is not None
    assert alert.level == "crit"


def test_certificate_alert_keeps_one_key():
    """Ключ один и тот же и при «кончается», и при «протух»: иначе при
    переходе одной тревоги в другую админ получит вторую пачку сообщений, а
    первая никогда не отпустит."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    warn = health.evaluate_cert(now + timedelta(hours=10), now)
    crit = health.evaluate_cert(now - timedelta(hours=10), now)
    assert warn.key == crit.key


# --- Расхождение версий обхода (21.08.2026) ----------------------------------

class TestWdttVersionDrift:
    """Отставшая нода снаружи выглядит идеально: служба active, сокет отвечает,
    доступы выдаются. Отличается только программа — и узнать об этом можно было
    единственным способом: сходить руками и сравнить отпечатки.
    """

    class _Srv:
        def __init__(self, wdtt_enabled: bool = True, sid: int = 7) -> None:
            self.id = sid
            self.wdtt_enabled = wdtt_enabled

    def _snap(self, sha: str):
        from bot.services.health import Snapshot

        snap = Snapshot(server_id=7, server_name="de1")
        snap.wdtt_sha = sha
        return snap

    def test_alerts_when_node_lags(self, monkeypatch) -> None:
        from bot.services import health, wdtt_update

        monkeypatch.setattr(wdtt_update, "reference_sha256", lambda: "a" * 64)
        alert = health.wdtt_version_alert(self._Srv(), self._snap("b" * 64))
        assert alert is not None and alert.level == "warn"
        assert "bbbbbbbb" in alert.detail and "aaaaaaaa" in alert.detail

    def test_silent_when_versions_match(self, monkeypatch) -> None:
        from bot.services import health, wdtt_update

        monkeypatch.setattr(wdtt_update, "reference_sha256", lambda: "a" * 64)
        assert health.wdtt_version_alert(self._Srv(), self._snap("a" * 64)) is None

    def test_silent_where_bypass_is_off(self, monkeypatch) -> None:
        """Нода без резервного подключения не обязана нести эту программу."""
        from bot.services import health, wdtt_update

        monkeypatch.setattr(wdtt_update, "reference_sha256", lambda: "a" * 64)
        assert health.wdtt_version_alert(
            self._Srv(wdtt_enabled=False), self._snap("b" * 64)
        ) is None

    def test_silent_without_a_reference(self, monkeypatch) -> None:
        """Нет эталона на машине бота — сравнивать не с чем, будить админа
        нечем: выдуманная авария хуже пропущенной."""
        from bot.services import health, wdtt_update

        monkeypatch.setattr(wdtt_update, "reference_sha256", lambda: None)
        assert health.wdtt_version_alert(self._Srv(), self._snap("b" * 64)) is None

    def test_probe_asks_for_the_fingerprint(self) -> None:
        """Отпечаток снимается тем же заходом по SSH, что и остальной снимок:
        лишнее подключение — лишняя попытка входа в журнале ноды."""
        from bot.services.health import probe_command

        assert "---WDTTBIN---" in probe_command(["wdtt"])
        assert "sha256sum" in probe_command(["wdtt"])

    def test_snapshot_reads_the_fingerprint(self) -> None:
        from bot.services.health import build_snapshot

        out = "---SERVICES---\nwdtt active\n---WDTTBIN---\n" + "c" * 64 + "\n"
        snap = build_snapshot(1, "nl1", out, units=["wdtt"])
        assert snap.wdtt_sha == "c" * 64
