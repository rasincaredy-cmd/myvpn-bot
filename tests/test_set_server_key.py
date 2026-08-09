"""Ключ должен ложиться в базу зашифрованным, пароль остаётся на месте."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.set_server_key import prepare_update


def test_key_is_encrypted_and_password_untouched(monkeypatch) -> None:
    import scripts.set_server_key as mod

    monkeypatch.setattr(mod, "encrypt", lambda text: b"ENC:" + text.encode())
    update = prepare_update("PRIVATE-KEY-BODY")
    assert update["ssh_key_enc"] == b"ENC:PRIVATE-KEY-BODY"
    assert "ssh_password_enc" not in update, (
        "пароль нельзя стирать здесь — сначала надо доказать вход по ключу"
    )


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError):
        prepare_update("   ")
