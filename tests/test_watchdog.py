"""Сторож за машиной бота — тот, кто скажет, когда сам бот сказать не может.

27.08.2026 у машины бота на шесть минут пропал интернет: клиенты остались без
VPN, а единственное сообщение, которое дошло до админа, было «Отпустило». Эти
тесты гоняют сам сценарий сторожа настоящим шеллом, подменяя curl: логика
сторожа работает в тот единственный момент, когда всё остальное уже лежит, и
проверять её на боевой ноде поздно.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bot.services import watchdog

_CURL_STUB = """#!/bin/sh
# Подменённый curl: сеть и Telegram включаются файлами-флагами.
for a in "$@"; do last="$a"; done
case "$last" in
    *api.telegram.org*)
        [ -f "{root}/tg_up" ] || exit 1
        for a in "$@"; do
            case "$a" in
                text=*) printf '%s\\n---\\n' "${{a#text=}}" >> "{root}/sent.log" ;;
            esac
        done
        ;;
    *)
        [ -f "{root}/host_up" ] || exit 1
        ;;
esac
exit 0
"""


class Node:
    """Нода со сторожем: без сети, без Telegram — только файлы."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(_CURL_STUB.format(root=root))
        curl.chmod(0o755)

        self.script = root / "watchdog"
        self.script.write_text(watchdog.render_script())
        self.script.chmod(0o755)

        self.env_file = root / "watchdog.env"
        self.env_file.write_text(
            watchdog.render_env(
                url="https://31.77.157.162/app/alive",
                token="123:TOKEN",
                admins=[111, 222],
                node="Германия",
            )
        )
        self.state = root / "state"
        self.host_up = True
        self.telegram_up = True

    def _flag(self, name: str, on: bool) -> None:
        path = self.root / name
        if on:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    @property
    def host_up(self) -> bool:
        return (self.root / "host_up").exists()

    @host_up.setter
    def host_up(self, value: bool) -> None:
        self._flag("host_up", value)

    @property
    def telegram_up(self) -> bool:
        return (self.root / "tg_up").exists()

    @telegram_up.setter
    def telegram_up(self, value: bool) -> None:
        self._flag("tg_up", value)

    def tick(self) -> None:
        """Один запуск по таймеру."""
        subprocess.run(
            ["sh", str(self.script)],
            check=True,
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "WATCHDOG_ENV": str(self.env_file),
                "WATCHDOG_STATE": str(self.state),
            },
        )

    @property
    def sent(self) -> list[str]:
        log = self.root / "sent.log"
        if not log.exists():
            return []
        return [m.strip() for m in log.read_text().split("\n---\n") if m.strip()]

    def age_the_last_alert(self) -> None:
        """Сделать вид, что с прошлой тревоги прошёл час с лишним."""
        stamp = self.state / "last_sent"
        stamp.write_text(str(int(stamp.read_text()) - 3601))


@pytest.fixture
def node(tmp_path: Path) -> Node:
    return Node(tmp_path)


def test_living_bot_is_silence(node: Node) -> None:
    node.tick()
    node.tick()
    assert node.sent == []


def test_single_miss_is_not_an_alarm(node: Node) -> None:
    """Икание на полминуты — не авария: тревоги, приходящие зря, перестают
    читать, и это хуже, чем не иметь их вовсе."""
    node.host_up = False
    node.tick()
    assert node.sent == []


def test_two_misses_in_a_row_wake_the_admin(node: Node) -> None:
    node.host_up = False
    node.tick()
    node.tick()
    assert len(node.sent) == 2                      # обоим админам
    assert "Бот не отвечает" in node.sent[0]
    assert "Германия" in node.sent[0]


def test_alarm_is_not_repeated_every_two_minutes(node: Node) -> None:
    node.host_up = False
    for _ in range(5):
        node.tick()
    assert len(node.sent) == 2                      # по одному на админа


def test_alarm_repeats_once_an_hour(node: Node) -> None:
    node.host_up = False
    node.tick()
    node.tick()
    node.age_the_last_alert()
    node.tick()
    assert len(node.sent) == 4


def test_all_clear_when_the_bot_is_back(node: Node) -> None:
    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    node.tick()
    assert "Отпустило" in node.sent[-1]
    node.tick()
    assert sum("Отпустило" in m for m in node.sent) == 2   # один раз, двоим


def test_undelivered_alarm_is_retried(node: Node) -> None:
    """Самый вероятный случай: у ноды тоже проблемы со связью. Сообщение,
    которое не удалось доставить, не считается отправленным."""
    node.host_up = False
    node.telegram_up = False
    node.tick()
    node.tick()
    assert node.sent == []

    node.telegram_up = True
    node.tick()
    assert len(node.sent) == 2
    assert "Бот не отвечает" in node.sent[0]


def test_undelivered_all_clear_is_retried(node: Node) -> None:
    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    node.telegram_up = False
    node.tick()
    node.telegram_up = True
    node.tick()
    assert "Отпустило" in node.sent[-1]


def test_missing_settings_are_silence_not_a_crash(node: Node) -> None:
    """Файла настроек нет — сторож молча выходит: он ставится ботом, и
    полусобранная нода не должна будить админа мусором."""
    node.env_file.unlink()
    node.tick()
    assert node.sent == []


def test_flapping_does_not_come_in_pairs(node: Node) -> None:
    """03.09.2026: машина бота полтора часа пропадала и возвращалась каждые
    пять-десять минут, и на каждый цикл сторож слал тревогу и отбой — двенадцать
    сообщений за вечер. Второе падение внутри часа админ уже не узнаёт ничего
    нового, поэтому оно молчит."""
    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    node.tick()
    assert len(node.sent) == 4                      # тревога и отбой, двоим

    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    node.tick()
    assert len(node.sent) == 4                      # про мигание — молчок


def test_flapping_is_told_once_an_hour(node: Node) -> None:
    """Молчок не вечный: машина, пропадающая по разу в десять минут, для
    юзера почти мертва, и через час об этом говорится одним сообщением —
    даже если прямо сейчас она отвечает."""
    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    node.tick()

    node.host_up = False                            # тихое падение
    node.tick()
    node.tick()
    node.host_up = True
    node.tick()

    node.age_the_last_alert()
    node.tick()
    assert "мигает" in node.sent[-1]
    assert "2 раз" in node.sent[-1]


def test_alarm_and_all_clear_mention_the_flapping(node: Node) -> None:
    """Разница между «упала один раз» и «падает весь вечер» — это разница
    между «перезапусти» и «меняй машину», её нельзя терять."""
    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    node.tick()
    assert "мигает" not in node.sent[-1]             # одно падение — не мигание

    node.host_up = False
    node.tick()
    node.tick()
    node.age_the_last_alert()
    node.tick()
    assert "мигает" in node.sent[-1]

    node.host_up = True
    node.tick()
    assert "Отпустило" in node.sent[-1]
    assert "2 раз" in node.sent[-1]


def test_an_hour_of_calm_forgets_the_flapping(node: Node) -> None:
    node.host_up = False
    node.tick()
    node.tick()
    node.host_up = True
    for _ in range(30):                              # час ровной работы
        node.tick()

    node.age_the_last_alert()
    node.host_up = False
    node.tick()
    node.tick()
    assert "Бот не отвечает" in node.sent[-1]
    assert "мигает" not in node.sent[-1]


def test_upgrade_does_not_swallow_the_all_clear(node: Node) -> None:
    """Сторож обновляется на живой ноде, посреди начатой тревоги: признак
    «админу уже сказали» переехал в другой файл, и отбой обязан дойти."""
    import time

    node.state.mkdir(parents=True, exist_ok=True)
    (node.state / "alerted").write_text("1")         # состояние старой версии
    (node.state / "fails").write_text("2")
    (node.state / "last_sent").write_text(str(int(time.time())))

    node.tick()
    assert "Отпустило" in node.sent[-1]


# ── Сборка настроек ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "given, expected",
    [
        ("https://1.2.3.4/app/", "https://1.2.3.4/app/alive"),
        ("https://1.2.3.4/app", "https://1.2.3.4/app/alive"),
        ("https://1.2.3.4", "https://1.2.3.4/app/alive"),
        ("", ""),
    ],
)
def test_alive_url(given: str, expected: str) -> None:
    assert watchdog.alive_url(given) == expected


def test_node_name_with_a_space_does_not_break_the_settings() -> None:
    env = watchdog.render_env(
        url="https://1.2.3.4/app/alive", token="T", admins=[1], node="Германия 2"
    )
    assert 'NODE="Германия 2"' in env


# ── Отклик, который сторож проверяет ─────────────────────────────────────────

async def _alive_client():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from bot.miniapp.app import add_routes

    app = web.Application()
    add_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_alive_answers_without_auth_and_without_the_database():
    """Сторож ходит без подписи Telegram и не должен зависеть от базы:
    его дело — отличить живой процесс бота от мёртвого."""
    client = await _alive_client()
    try:
        resp = await client.get("/app/alive")
        assert resp.status == 200
        assert (await resp.text()) == "ok"
    finally:
        await client.close()


async def test_alive_is_not_shadowed_by_the_asset_route():
    """Отклик лежит внутри /app рядом с раздачей app.css и app.js — порядок
    маршрутов решает, кто ответит."""
    client = await _alive_client()
    try:
        assert (await client.get("/app/unknown.txt")).status == 404
        assert (await client.get("/app/alive")).status == 200
    finally:
        await client.close()


# ── Установка на ноду ────────────────────────────────────────────────────────

class _Res:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_status = 0


class FakeSSH:
    """Нода, которая только записывает, что ей сказали."""

    def __init__(self, *, bot_host: bool = False, curl: bool = True,
                 timer: str = "active") -> None:
        self.bot_host = bot_host
        self.curl = curl
        self.timer = timer
        self.files: dict[str, tuple[str, int]] = {}
        self.commands: list[str] = []

    async def run(self, cmd: str) -> _Res:
        self.commands.append(cmd)
        if "ip route get" in cmd:
            node, boss = "31.77.148.187", "31.77.157.162"
            if self.bot_host:
                boss = node
            return _Res(f"{node}\n{boss}\n")
        if "command -v curl" in cmd:
            return _Res("yes\n" if self.curl else "no\n")
        if "is-active" in cmd:
            out = f"{self.timer}\n"
            if self.timer == "active":
                out += "env-ok\n"
            return _Res(out)
        return _Res("")

    async def write_file(self, path: str, content: str, *, mode: int = 0o600) -> None:
        self.files[path] = (content, mode)


@pytest.fixture
def prod_url(monkeypatch):
    from bot.config import settings

    monkeypatch.setattr(settings, "miniapp_url", "https://31.77.157.162/app/")
    return settings


async def test_install_writes_the_settings_and_starts_the_timer(prod_url) -> None:
    ssh = FakeSSH()
    assert await watchdog.install(ssh, node_name="Германия") is True

    env, mode = ssh.files[watchdog.ENV_PATH]
    assert "https://31.77.157.162/app/alive" in env
    assert mode == 0o600, "файл с токеном обязан быть закрыт от чужих глаз"
    assert ssh.files[watchdog.SCRIPT_PATH][1] == 0o700
    assert any("enable --now" in c for c in ssh.commands)


async def test_token_never_appears_in_a_command_line(prod_url) -> None:
    """Аргументы команд видны в списке процессов, файлы — нет."""
    ssh = FakeSSH()
    await watchdog.install(ssh, node_name="Германия")
    assert not any(prod_url.bot_token in c for c in ssh.commands)


async def test_bot_own_machine_is_skipped(prod_url) -> None:
    ssh = FakeSSH(bot_host=True)
    assert await watchdog.install(ssh, node_name="Клопас") is False
    assert ssh.files == {}


async def test_without_the_miniapp_address_there_is_nothing_to_watch(monkeypatch):
    from bot.config import settings

    monkeypatch.setattr(settings, "miniapp_url", "")
    with pytest.raises(watchdog.WatchdogError):
        await watchdog.install(FakeSSH(), node_name="Германия")


async def test_node_without_curl_says_so(prod_url) -> None:
    with pytest.raises(watchdog.WatchdogError):
        await watchdog.install(FakeSSH(curl=False), node_name="Германия")


async def test_timer_that_did_not_start_is_a_failure(prod_url) -> None:
    with pytest.raises(watchdog.WatchdogError):
        await watchdog.install(FakeSSH(timer="inactive"), node_name="Германия")


async def test_running_needs_both_the_timer_and_the_settings() -> None:
    """«active» без файла настроек врёт: сценарий с пустыми настройками молча
    выходит на первой строке."""
    ssh = FakeSSH()

    async def half_ready(cmd: str) -> _Res:
        return _Res("active\n")          # таймер есть, файла настроек нет

    ssh.run = half_ready
    assert await watchdog.is_running(ssh) is False


# ── Проводка в боте ──────────────────────────────────────────────────────────

def test_install_wizard_sets_up_the_watchdog() -> None:
    """Новая нода начинает сторожить сама, без отдельной кнопки."""
    import inspect

    from bot.handlers import install

    src = inspect.getsource(install)
    assert "watchdog.install" in src
    # После эталона: сторож ходит наружу, фаервол к этому моменту построен.
    assert src.index("hardening.harden") < src.index("watchdog.install")


def test_protection_screen_shows_and_repairs_the_watchdog() -> None:
    """С телефона это единственный способ починить сторожа."""
    import inspect

    from bot.handlers.servers import card

    src = inspect.getsource(card)
    assert "_watchdog_state" in src and "watchdog.install" in src
