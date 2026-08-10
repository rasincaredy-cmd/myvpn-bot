"""Разбор вывода сценария-эталона и заведение ключа для бота."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh

from bot.services.hardening import KEY_PATH, SCRIPT_PATH, ensure_bot_key, parse_check
from bot.services.ssh import CommandResult, SSHError


def test_harden_order_password_last() -> None:
    """Порядок шагов — это и есть безопасность.

    Выключение пароля обязано идти после заведения ключа, а фаервол —
    после того, как нужные порты уже разрешены. Перестановка любого из
    этих шагов оставляет сервер без доступа.

    Ищем порядок в теле функции без докстринга: упоминание команды
    в описании — не вызов, а тест на нём уже один раз ложно упал.
    """
    import inspect

    from bot.services.hardening import harden

    src = inspect.getsource(harden)
    body_start = src.index('"""', src.index('"""') + 3) + 3
    src = src[body_start:]
    order = [
        src.index("apply-stats"),
        src.index("apply-journal"),
        src.index("apply-fail2ban"),
        src.index("apply-firewall"),
        src.index("ensure_bot_key"),
        src.index("disable-password"),
    ]
    assert order == sorted(order), "порядок шагов приведения к эталону нарушен"


def test_script_is_taken_from_repo() -> None:
    # Копия текста сценария в коде разъедется с самим сценарием.
    assert SCRIPT_PATH.is_file(), "сценарий-эталон не найден в репозитории"
    assert SCRIPT_PATH.name == "harden.sh"


def test_parse_all_green() -> None:
    out = (
        "=== проверка соответствия эталону ===\n"
        "собственный адрес: 1.2.3.4\n"
        "OK   вход по паролю выключен\n"
        "OK   фаервол включён\n"
        "\n"
        "ИТОГ: сервер соответствует эталону\n"
    )
    report = parse_check(out, 0)
    assert report.compliant is True
    assert report.failed == []
    assert "вход по паролю выключен" in report.ok


def test_parse_with_failures() -> None:
    out = (
        "OK   фаервол включён\n"
        "FAIL вход по паролю РАЗРЕШЁН\n"
        "FAIL банилки перебора нет\n"
        "ИТОГ: есть несоответствия (см. FAIL выше)\n"
    )
    report = parse_check(out, 1)
    assert report.compliant is False
    assert report.failed == ["вход по паролю РАЗРЕШЁН", "банилки перебора нет"]


def test_nonzero_exit_is_not_compliant_even_without_fail_lines() -> None:
    # Сценарий мог упасть до печати проверок — молча считать это
    # соответствием нельзя.
    report = parse_check("что-то пошло не так\n", 2)
    assert report.compliant is False


def test_ensure_bot_key_does_not_touch_password() -> None:
    # Пароль — единственный путь на сервер, пока ключ не доказан.
    # Его стирание на этом шаге лишает возможности откатиться.
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "ssh_password_enc" not in src, (
        "пароль нельзя трогать до подтверждённого входа по ключу"
    )


def test_ensure_bot_key_clears_stale_passphrase() -> None:
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "ssh_key_passphrase_enc" in src, (
        "фраза-пароль от старого ключа должна зануляться"
    )


def test_ensure_bot_key_commits_before_returning() -> None:
    """Ключ обязан лежать в базе ДО того, как гасится пароль.

    Следующим шагом `harden` выключает вход по паролю. Если ключ к этому
    моменту записан только в память сессии и бот упадёт (или сессия
    откатится) — пароль уже выключен, а ключа в базе нет: сервер потерян
    для бота навсегда.
    """
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "commit" in src, "ключ не фиксируется в базе до выключения пароля"


# --- Поведенческие тесты: без сети, но на настоящей проверке доказательства
# входа. Текстовые тесты выше стерегут «что не делать» (пароль, коммит),
# эти — «что именно считается доказанным входом».


def _generate_keypair() -> tuple[str, str]:
    """Настоящая пара ed25519-ключей — чтобы import_private_key внутри
    ensure_bot_key реально её распознавал, а не притворялась заглушкой."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    private = key.export_private_key().decode()
    public = key.export_public_key().decode().strip()
    return private, public


class _FakeInitialSSH:
    """Фейк того подключения, которым бот уже вошёл на сервер (по паролю).
    Им ensure_bot_key генерирует ключ и раскладывает authorized_keys."""

    def __init__(self, public: str, private: str) -> None:
        self._public = public
        self._private = private

    async def run(self, cmd: str, *, check: bool = False, timeout=None) -> CommandResult:
        if cmd.startswith("cat") and cmd.endswith(".pub"):
            return CommandResult(cmd=cmd, exit_code=0, stdout=self._public + "\n", stderr="")
        return CommandResult(cmd=cmd, exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        assert path == KEY_PATH
        return self._private


def _fake_server() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        host="203.0.113.9",
        ssh_port=2222,
        ssh_user="root",
        ssh_key_enc=None,
        ssh_key_passphrase_enc=b"STALE-OLD-PASSPHRASE",
    )


async def test_probe_connection_failure_returns_false_and_writes_nothing(
    monkeypatch,
) -> None:
    """asyncssh при отказе подключения (не тот ключ, connection refused и
    т.п.) кидает SSHError — проба обязана считать это провалом, а не
    исключением наружу, и не трогать базу."""
    import bot.services.hardening as mod

    private, public = _generate_keypair()
    server = _fake_server()

    async def fake_get_server(_session, _server_id):
        return server

    monkeypatch.setattr("bot.db.repo.get_server", fake_get_server)

    class _FakeProbeConnFails:
        def __init__(self, creds) -> None:
            self.creds = creds

        async def __aenter__(self):
            raise SSHError("SSH: доступ запрещён")

        async def __aexit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(mod, "SSHClient", _FakeProbeConnFails)

    session = SimpleNamespace(commit=AsyncMock())
    ok = await ensure_bot_key(_FakeInitialSSH(public, private), session, server.id)

    assert ok is False
    assert server.ssh_key_enc is None
    session.commit.assert_not_awaited()


async def test_probe_wrong_output_is_not_substring_matched(monkeypatch) -> None:
    """Регрессия на I4: раньше проверялось `"ok" in stdout`, и любой вывод
    из ~/.bashrc, содержащий «ok», красил бы пробу в зелёный. Успех — это
    строго exit_code == 0 и stdout.strip() == "ok"."""
    import bot.services.hardening as mod

    private, public = _generate_keypair()
    server = _fake_server()

    async def fake_get_server(_session, _server_id):
        return server

    monkeypatch.setattr("bot.db.repo.get_server", fake_get_server)

    class _FakeProbeNoisyOutput:
        def __init__(self, creds) -> None:
            self.creds = creds

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def run(self, cmd: str, **kwargs) -> CommandResult:
            return CommandResult(
                cmd=cmd, exit_code=0, stdout="приветствие из .bashrc: ok\n", stderr=""
            )

    monkeypatch.setattr(mod, "SSHClient", _FakeProbeNoisyOutput)

    session = SimpleNamespace(commit=AsyncMock())
    ok = await ensure_bot_key(_FakeInitialSSH(public, private), session, server.id)

    assert ok is False
    assert server.ssh_key_enc is None
    session.commit.assert_not_awaited()


async def test_unparseable_private_key_returns_false_without_encrypt(monkeypatch) -> None:
    """Пустой или битый приватный ключ (например 0-байтовый файл от
    прерванного ssh-keygen) не должен уходить в базу: иначе бот потеряет
    и пароль (следующим шагом), и рабочий ключ разом."""
    import bot.services.hardening as mod

    server = _fake_server()

    async def fake_get_server(_session, _server_id):
        return server

    monkeypatch.setattr("bot.db.repo.get_server", fake_get_server)

    encrypt_calls: list[str] = []
    monkeypatch.setattr(
        "bot.services.crypto.encrypt",
        lambda text: encrypt_calls.append(text) or b"SHOULD-NOT-BE-CALLED",
    )

    def _boom(*args, **kwargs):
        raise AssertionError("SSHClient не должен создаваться для непарсящегося ключа")

    monkeypatch.setattr(mod, "SSHClient", _boom)

    session = SimpleNamespace(commit=AsyncMock())
    fake_ssh = _FakeInitialSSH(public="ssh-ed25519 AAAAnotreal comment", private="совсем не ключ")

    ok = await ensure_bot_key(fake_ssh, session, server.id)

    assert ok is False
    assert server.ssh_key_enc is None
    assert encrypt_calls == []
    session.commit.assert_not_awaited()


async def test_server_missing_returns_false(monkeypatch) -> None:
    async def fake_get_server(_session, _server_id):
        return None

    monkeypatch.setattr("bot.db.repo.get_server", fake_get_server)

    session = SimpleNamespace(commit=AsyncMock())
    ssh = AsyncMock()

    ok = await ensure_bot_key(ssh, session, 999)

    assert ok is False
    session.commit.assert_not_awaited()
    ssh.run.assert_not_awaited()


async def test_happy_path_writes_encrypted_key_clears_passphrase_and_commits(
    monkeypatch,
) -> None:
    import bot.services.hardening as mod

    private, public = _generate_keypair()
    server = _fake_server()

    async def fake_get_server(_session, _server_id):
        return server

    monkeypatch.setattr("bot.db.repo.get_server", fake_get_server)
    monkeypatch.setattr(
        "bot.services.crypto.encrypt", lambda text: b"ENC:" + text.encode()
    )

    captured: dict[str, object] = {}

    class _FakeProbeOK:
        def __init__(self, creds) -> None:
            captured["creds"] = creds

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def run(self, cmd: str, **kwargs) -> CommandResult:
            return CommandResult(cmd=cmd, exit_code=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(mod, "SSHClient", _FakeProbeOK)

    session = SimpleNamespace(commit=AsyncMock())
    ok = await ensure_bot_key(_FakeInitialSSH(public, private), session, server.id)

    assert ok is True
    assert server.ssh_key_enc == b"ENC:" + private.encode()
    assert server.ssh_key_passphrase_enc is None
    session.commit.assert_awaited_once()

    # C1/I6: проба обязана идти по реальным host/port/user сервера и
    # строго ключом — без пароля в кредах вовсе (иначе asyncssh при отказе
    # ключа молча откатится на пароль, и проба перестанет что-либо доказывать).
    creds = captured["creds"]
    assert creds.host == server.host
    assert creds.port == server.ssh_port
    assert creds.username == server.ssh_user
    assert creds.private_key == private
    assert creds.password is None


# --- Поведенческие тесты оркестровки `harden`: без сети, на фейковом SSH.
# Текстовый тест выше (`test_harden_order_password_last`) стережёт порядок
# по исходнику, но останется зелёным и на сломанной логике вызова — эти
# проверяют, что шаги реально вызываются в нужном порядке и с нужными
# последствиями отказов.


class _FakeHardenSSH:
    """Фейковый SSH для `harden`: пишет команды в общий журнал `order`,
    чтобы порядок вызова шагов (включая `ensure_bot_key`, который не
    ходит через `ssh.run`) был виден в одном списке."""

    def __init__(
        self,
        order: list[str],
        *,
        firewall_ok: bool = True,
        disable_password_ok: bool = True,
        failing: tuple[str, ...] = (),
        check_stdout: str = "ИТОГ: сервер соответствует эталону\n",
    ) -> None:
        self.order = order
        self.firewall_ok = firewall_ok
        self.disable_password_ok = disable_password_ok
        # Команды, которые «проваливаются» (ненулевой код) — по подстроке.
        self.failing = failing
        # Что печатает `check` в конце. По умолчанию — всё зелёное: именно
        # эта комбинация («шаг упал, а состояние сервера выглядит нормальным»)
        # и была дырой из Critical-1.
        self.check_stdout = check_stdout

    async def write_file(self, path: str, content: str, *, mode: int = 0o700) -> None:
        pass

    async def run(self, cmd: str, *, check: bool = False, timeout=None) -> CommandResult:
        self.order.append(cmd)
        if any(f in cmd for f in self.failing):
            return CommandResult(cmd=cmd, exit_code=1, stdout="", stderr="")
        if "apply-firewall" in cmd:
            return CommandResult(cmd=cmd, exit_code=0 if self.firewall_ok else 1, stdout="", stderr="")
        if "disable-password" in cmd:
            return CommandResult(cmd=cmd, exit_code=0 if self.disable_password_ok else 1, stdout="", stderr="")
        if cmd.endswith(" check"):
            return CommandResult(cmd=cmd, exit_code=0, stdout=self.check_stdout, stderr="")
        return CommandResult(cmd=cmd, exit_code=0, stdout="", stderr="")


async def _noop_progress(_text: str) -> None:
    pass


def _patch_get_server(monkeypatch, server) -> None:
    async def fake_get_server(_session, _server_id):
        return server

    monkeypatch.setattr("bot.db.repo.get_server", fake_get_server)


async def test_harden_calls_steps_in_order(monkeypatch) -> None:
    """`ensure_bot_key` идёт после фаервола и до выключения пароля —
    проверка не по тексту исходника, а по факту вызова."""
    import bot.services.hardening as mod

    order: list[str] = []
    ssh = _FakeHardenSSH(order)
    server = _fake_server()
    _patch_get_server(monkeypatch, server)

    async def fake_ensure_bot_key(_ssh, _session, _server_id):
        order.append("ensure_bot_key")
        return True

    monkeypatch.setattr(mod, "ensure_bot_key", fake_ensure_bot_key)

    session = SimpleNamespace(commit=AsyncMock())
    await mod.harden(ssh, session, server.id, wg_port=51820, progress=_noop_progress)

    steps = ["apply-stats", "apply-journal", "apply-fail2ban", "apply-firewall", "ensure_bot_key", "disable-password"]
    positions = [next(i for i, c in enumerate(order) if step in c) for step in steps]
    assert positions == sorted(positions), f"нарушен порядок вызовов: {order}"


async def test_harden_skips_disable_password_when_key_not_proven(monkeypatch) -> None:
    """Если `ensure_bot_key` вернул False, команда выключения пароля не
    должна выполняться вовсе — иначе сервер останется без единого
    рабочего способа входа."""
    import bot.services.hardening as mod

    order: list[str] = []
    ssh = _FakeHardenSSH(order)
    server = _fake_server()
    _patch_get_server(monkeypatch, server)

    async def fake_ensure_bot_key(_ssh, _session, _server_id):
        return False

    monkeypatch.setattr(mod, "ensure_bot_key", fake_ensure_bot_key)

    messages: list[str] = []

    async def capture_progress(text: str) -> None:
        messages.append(text)

    session = SimpleNamespace(commit=AsyncMock())
    await mod.harden(ssh, session, server.id, wg_port=51820, progress=capture_progress)

    assert not any("disable-password" in cmd for cmd in order)
    assert any("не подтверждён" in m for m in messages)


async def test_harden_firewall_failure_does_not_abort_remaining_steps(monkeypatch) -> None:
    """Отказ фаервола не должен обрывать оркестровку: сервер уже
    установлен, частичная защита лучше никакой."""
    import bot.services.hardening as mod

    order: list[str] = []
    ssh = _FakeHardenSSH(order, firewall_ok=False)
    server = _fake_server()
    _patch_get_server(monkeypatch, server)

    async def fake_ensure_bot_key(_ssh, _session, _server_id):
        order.append("ensure_bot_key")
        return True

    monkeypatch.setattr(mod, "ensure_bot_key", fake_ensure_bot_key)

    session = SimpleNamespace(commit=AsyncMock())
    await mod.harden(ssh, session, server.id, wg_port=51820, progress=_noop_progress)

    assert any("ensure_bot_key" in c for c in order)
    assert any("disable-password" in c for c in order)
    assert any(c.endswith(" check") for c in order)


async def _run_harden(monkeypatch, ssh, server, *, key_ok: bool = True):
    """Прогнать `harden` на фейковом SSH и вернуть отчёт."""
    import bot.services.hardening as mod

    _patch_get_server(monkeypatch, server)

    async def fake_ensure_bot_key(_ssh, _session, _server_id):
        return key_ok

    monkeypatch.setattr(mod, "ensure_bot_key", fake_ensure_bot_key)
    session = SimpleNamespace(commit=AsyncMock())
    return await mod.harden(
        ssh, session, server.id, wg_port=51820, progress=_noop_progress
    )


async def test_harden_passes_real_ssh_port_of_the_server(monkeypatch) -> None:
    """Important-2: обязательный порт для фаервола — тот, на котором ssh
    реально слушает У ЭТОГО сервера (мастер установки спрашивает порт у
    админа, не-22 — штатный ввод). Зашитый `tcp/22` на сервере с ssh на
    2222 не слушает, сценарий отказывается включать фаервол — и не включит
    его уже никогда: ни при установке, ни по кнопке в админке.

    Порты передаются строго как `tcp/NNN` и `udp/NNN`: голое число
    сценарий не распознаёт и тоже откажется включать фаервол.
    """
    import bot.services.hardening as mod

    order: list[str] = []
    server = _fake_server()  # ssh_port=2222
    await _run_harden(monkeypatch, _FakeHardenSSH(order), server)

    fw_cmds = [c for c in order if "apply-firewall" in c]
    assert fw_cmds == [
        f"{mod.REMOTE_PATH} apply-firewall tcp/{server.ssh_port} udp/51820"
    ], f"порт ssh взят не из данных сервера: {fw_cmds}"


async def test_harden_ssh_port_follows_the_server_record(monkeypatch) -> None:
    """Тот же вызов на сервере с обычным портом 22 — чтобы «взято из базы»
    не выродилось в другую константу."""
    import bot.services.hardening as mod

    order: list[str] = []
    server = _fake_server()
    server.ssh_port = 22
    await _run_harden(monkeypatch, _FakeHardenSSH(order), server)

    fw_cmds = [c for c in order if "apply-firewall" in c]
    assert fw_cmds == [f"{mod.REMOTE_PATH} apply-firewall tcp/22 udp/51820"]


# --- Critical-1: отказ шага обязан дожить до итогового отчёта --------------
#
# Итог целиком строится из `check`, а `check` видит только текущее состояние
# сервера. Шаг мог применить настройку и НЕ пройти самопроверку — тогда на
# сервере остался вооружённый автооткат, который через 10 минут снимет
# защиту, а бот бы отрапортовал «сервер соответствует эталону».


async def test_disable_password_failure_reaches_the_report(monkeypatch) -> None:
    server = _fake_server()
    report = await _run_harden(
        monkeypatch, _FakeHardenSSH([], failing=("disable-password",)), server
    )
    assert report.compliant is False, (
        "отказ выключения пароля не дожил до отчёта — админ прочитает "
        "«соответствует эталону» на сервере с вооружённым автооткатом"
    )
    assert any("парол" in f.lower() for f in report.failed), report.failed


async def test_firewall_failure_reaches_the_report(monkeypatch) -> None:
    server = _fake_server()
    report = await _run_harden(
        monkeypatch, _FakeHardenSSH([], failing=("apply-firewall",)), server
    )
    assert report.compliant is False
    assert any("фаервол" in f.lower() for f in report.failed), report.failed


async def test_quiet_steps_failures_reach_the_report(monkeypatch) -> None:
    """apply-stats, apply-journal и apply-fail2ban раньше не проверялись на
    успех вовсе — их отказ пропадал бесследно."""
    for step, word in (
        ("apply-stats", "статистик"),
        ("apply-journal", "журнал"),
        ("apply-fail2ban", "перебор"),
    ):
        server = _fake_server()
        report = await _run_harden(
            monkeypatch, _FakeHardenSSH([], failing=(step,)), server
        )
        assert report.compliant is False, f"отказ {step} не дожил до отчёта"
        assert any(word in f.lower() for f in report.failed), (step, report.failed)


async def test_unproven_key_reaches_the_report(monkeypatch) -> None:
    """Пароль остался включённым — это несоответствие эталону, даже если
    остальное зелёное."""
    server = _fake_server()
    report = await _run_harden(
        monkeypatch, _FakeHardenSSH([]), server, key_ok=False
    )
    assert report.compliant is False
    assert any("ключ" in f.lower() for f in report.failed), report.failed


async def test_all_steps_ok_keeps_report_from_check(monkeypatch) -> None:
    """Обратная сторона: когда все шаги прошли, отчёт остаётся ровно тем,
    что вернула проверка, — иначе кнопка вечно рапортует «частично»."""
    server = _fake_server()
    report = await _run_harden(monkeypatch, _FakeHardenSSH([]), server)
    assert report.compliant is True
    assert report.failed == []
