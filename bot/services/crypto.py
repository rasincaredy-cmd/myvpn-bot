from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from bot.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    try:
        return Fernet(settings.encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "ENCRYPTION_KEY невалидный. Сгенерируй: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc


def encrypt(plaintext: str | bytes | None) -> bytes | None:
    if plaintext is None:
        return None
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    return _fernet().encrypt(plaintext)


def decrypt(ciphertext: bytes | None) -> str | None:
    if ciphertext is None:
        return None
    try:
        return _fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Не удалось расшифровать — другой ENCRYPTION_KEY?") from exc
