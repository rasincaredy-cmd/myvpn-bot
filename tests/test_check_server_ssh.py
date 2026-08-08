"""Скрипт проверки должен собирать креды так же, как бот."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_server_ssh import build_credentials


def test_prefers_key_over_password(monkeypatch) -> None:
    import scripts.check_server_ssh as mod

    monkeypatch.setattr(mod, "decrypt", lambda blob: None if blob is None else blob.decode())
    server = SimpleNamespace(
        host="10.0.0.1", ssh_port=22, ssh_user="root",
        ssh_password_enc=b"pwd", ssh_key_enc=b"KEY", ssh_key_passphrase_enc=None,
    )
    creds = build_credentials(server)
    assert creds.private_key == "KEY"
    assert creds.password is None, "при наличии ключа пароль слаться не должен"


def test_falls_back_to_password(monkeypatch) -> None:
    import scripts.check_server_ssh as mod

    monkeypatch.setattr(mod, "decrypt", lambda blob: None if blob is None else blob.decode())
    server = SimpleNamespace(
        host="10.0.0.1", ssh_port=22, ssh_user="root",
        ssh_password_enc=b"pwd", ssh_key_enc=None, ssh_key_passphrase_enc=None,
    )
    creds = build_credentials(server)
    assert creds.password == "pwd"
    assert creds.private_key is None
