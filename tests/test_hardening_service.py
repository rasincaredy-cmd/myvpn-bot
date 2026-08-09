"""Разбор вывода сценария-эталона и заведение ключа для бота."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh

from bot.services.hardening import KEY_PATH, SCRIPT_PATH, ensure_bot_key, parse_check
from bot.services.ssh import CommandResult, SSHError


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
