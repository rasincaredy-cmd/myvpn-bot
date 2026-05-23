"""Юнит-тесты Fernet-обёртки."""
from __future__ import annotations

import pytest

from bot.services.crypto import decrypt, encrypt


class TestCrypto:
    def test_roundtrip_str(self) -> None:
        ct = encrypt("hello world")
        assert ct is not None
        assert isinstance(ct, bytes)
        assert ct != b"hello world"
        assert decrypt(ct) == "hello world"

    def test_roundtrip_bytes(self) -> None:
        ct = encrypt(b"binary stuff")
        assert decrypt(ct) == "binary stuff"

    def test_none_passthrough(self) -> None:
        assert encrypt(None) is None
        assert decrypt(None) is None

    def test_different_ciphertexts_each_call(self) -> None:
        """Fernet включает IV — каждый раз другой ciphertext (защита от replay)."""
        a = encrypt("same")
        b = encrypt("same")
        assert a != b
        assert decrypt(a) == decrypt(b) == "same"

    def test_unicode(self) -> None:
        text = "🔐 пароль с эмодзи и кириллицей"
        assert decrypt(encrypt(text)) == text

    def test_long_payload(self) -> None:
        """SSH-ключи бывают по 3-4 KB — должно работать."""
        text = "A" * 8192
        assert decrypt(encrypt(text)) == text

    def test_decrypt_garbage_raises(self) -> None:
        with pytest.raises(RuntimeError):
            decrypt(b"not-a-valid-fernet-token")
