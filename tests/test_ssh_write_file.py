"""Запись файла на сервер идёт через SFTP, а не через кавычки в шелле."""
import inspect

from bot.services.ssh import SSHClient


def test_write_file_exists() -> None:
    assert hasattr(SSHClient, "write_file"), "нет метода записи файла"


def test_write_file_signature() -> None:
    sig = inspect.signature(SSHClient.write_file)
    assert "path" in sig.parameters
    assert "content" in sig.parameters
    assert "mode" in sig.parameters, "права на файл должны задаваться явно"


def test_write_file_uses_sftp() -> None:
    # Через шелл сценарий с кавычками обоих видов не передать надёжно.
    src = inspect.getsource(SSHClient.write_file)
    assert "sftp" in src.lower(), "запись должна идти через SFTP"
