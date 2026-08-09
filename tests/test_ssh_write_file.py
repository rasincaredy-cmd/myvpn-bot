"""Запись файла на сервер идёт через SFTP, а не через кавычки в шелле."""
import asyncio
import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from bot.services.ssh import SSHClient, SSHCredentials, SSHError


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


@pytest.mark.asyncio
async def test_write_file_behavior() -> None:
    """Поведенческий тест: проверяем, что метод записывает точно то, что нужно."""
    # Сохраняем параметры вызова open()
    open_calls = []

    # Создаём мок для открытого файла
    mock_file = AsyncMock()

    # Создаём мок SFTP-клиента с собственной реализацией open()
    class MockSFTPClient:
        def open(self, path, mode, **kwargs):
            # Сохраняем параметры вызова
            open_calls.append({"path": path, "mode": mode, "kwargs": kwargs})

            # Возвращаем async context manager
            class FileContextManager:
                async def __aenter__(self):
                    return mock_file

                async def __aexit__(self, *args):
                    pass

            return FileContextManager()

    mock_sftp = MockSFTPClient()

    # Создаём async context manager для start_sftp_client
    @asynccontextmanager
    async def mock_start_sftp_client():
        yield mock_sftp

    # Создаём SSHClient с фиктивным соединением
    client = SSHClient(SSHCredentials(host="example.com"))
    mock_conn = MagicMock()
    mock_conn.start_sftp_client = mock_start_sftp_client
    client._conn = mock_conn

    # Тестовые данные
    test_path = "/tmp/test_file.txt"
    test_content = "test content with 'single' and \"double\" quotes"
    test_mode = 0o640

    # Вызываем метод
    await client.write_file(test_path, test_content, mode=test_mode)

    # Проверяем, что файл был открыт по правильному пути
    assert len(open_calls) == 1, f"ожидается ровно один вызов open(), получено {len(open_calls)}"
    call = open_calls[0]
    assert call["path"] == test_path, f"неверный путь: {call['path']}"
    assert call["mode"] == "w", f"неверный режим открытия: {call['mode']}"

    # Проверяем, что были установлены нужные права через attrs
    assert "attrs" in call["kwargs"], "отсутствует параметр attrs"
    attrs = call["kwargs"]["attrs"]
    assert hasattr(attrs, "permissions"), "attrs должен иметь permissions"
    assert attrs.permissions == test_mode, f"неверные права: {attrs.permissions:o} != {test_mode:o}"

    # Проверяем, что содержимое было записано
    mock_file.write.assert_called_once_with(test_content)


@pytest.mark.asyncio
async def test_write_file_no_connection() -> None:
    """Проверяем, что метод выбрасывает SSHError при отсутствии соединения."""
    client = SSHClient(SSHCredentials(host="example.com"))
    client._conn = None

    try:
        await client.write_file("/tmp/test.txt", "content")
        pytest.fail("должна была быть выброшена SSHError")
    except SSHError as e:
        assert "соединения" in str(e).lower()
