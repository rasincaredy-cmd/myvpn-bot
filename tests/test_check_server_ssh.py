"""Скрипт проверки должен собирать креды так же, как бот, и не падать
трейсбеком на битых кредах."""
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services.ssh import SSHCredentials
from scripts.check_server_ssh import build_credentials


def test_prefers_key_over_password(monkeypatch) -> None:
    """build_credentials должен брать креды из боевой сборки repo.creds_from_server
    (а не собирать их заново — иначе разъедется при следующей правке боевой
    функции) и явно зануля́ть пароль, если есть ключ."""
    import scripts.check_server_ssh as mod

    seen: list[object] = []

    def fake_creds_from_server(server):
        seen.append(server)
        return SSHCredentials(
            host=server.host, port=server.ssh_port, username=server.ssh_user,
            password="pwd", private_key="KEY", key_passphrase=None,
        )

    monkeypatch.setattr(mod, "creds_from_server", fake_creds_from_server)
    server = SimpleNamespace(host="10.0.0.1", ssh_port=22, ssh_user="root")

    creds = build_credentials(server)

    assert seen == [server], "боевая сборка должна получить именно этот server"
    assert creds.private_key == "KEY"
    assert creds.password is None, (
        "при наличии ключа пароль слаться не должен: боевая сборка кладёт и "
        "ключ, и пароль сразу, и asyncssh при отказе ключа молча откатится "
        "на пароль — а этот скрипт должен строго проверять именно ключ"
    )


def test_falls_back_to_password(monkeypatch) -> None:
    import scripts.check_server_ssh as mod

    monkeypatch.setattr(
        mod,
        "creds_from_server",
        lambda server: SSHCredentials(
            host=server.host, port=server.ssh_port, username=server.ssh_user,
            password="pwd", private_key=None, key_passphrase=None,
        ),
    )
    server = SimpleNamespace(host="10.0.0.1", ssh_port=22, ssh_user="root")

    creds = build_credentials(server)

    assert creds.password == "pwd"
    assert creds.private_key is None


async def test_main_prints_error_instead_of_traceback_on_bad_creds(
    monkeypatch, capsys
) -> None:
    """decrypt() кидает RuntimeError на битом шифртексте/чужом ENCRYPTION_KEY.
    main() обязан поймать это и напечатать ОШИБКУ в обычном формате, а не
    уронить скрипт голым трейсбеком — это ровно тот случай, ради диагностики
    которого скрипт и запускают перед опасным шагом."""
    import scripts.check_server_ssh as mod

    server = SimpleNamespace(host="31.77.157.162", ssh_port=22, ssh_user="root")

    @asynccontextmanager
    async def fake_session_scope():
        yield None

    async def fake_get_server(session, server_id):
        return server

    def fake_build_credentials(server):
        raise RuntimeError("Не удалось расшифровать — другой ENCRYPTION_KEY?")

    monkeypatch.setattr(mod, "session_scope", fake_session_scope)
    monkeypatch.setattr(mod.repo, "get_server", fake_get_server)
    monkeypatch.setattr(mod, "build_credentials", fake_build_credentials)

    code = await mod.main(1)

    assert code == 1
    out = capsys.readouterr().out
    assert "ОШИБКА" in out
    assert server.host in out
