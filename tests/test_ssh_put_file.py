"""Заливка двоичного файла на сервер: программа обхода — 8 МБ, не текст.

`write_file` принимает строку и не годится: бинарь в str не превратить без
порчи байтов. Отдельный метод кладёт локальный файл как есть и сразу с нужными
правами — тем же приёмом, что и `write_file` (права в момент создания, а не
`chmod` следом, иначе между созданием и правами есть окно).
"""
import inspect
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from bot.services.ssh import SSHClient, SSHCredentials, SSHError


def test_put_file_exists() -> None:
    assert hasattr(SSHClient, "put_file"), "нет метода заливки файла"


def test_put_file_signature() -> None:
    sig = inspect.signature(SSHClient.put_file)
    assert "local_path" in sig.parameters
    assert "remote_path" in sig.parameters
    assert "mode" in sig.parameters, "права на файл должны задаваться явно"


class _MockSFTP:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.chmod_calls: list[tuple] = []

    async def put(self, local, remote, **kwargs):
        self.put_calls.append({"local": local, "remote": remote, "kwargs": kwargs})

    async def chmod(self, path, mode):
        self.chmod_calls.append((path, mode))


def _client_with(sftp) -> SSHClient:
    @asynccontextmanager
    async def _start():
        yield sftp

    client = SSHClient(SSHCredentials(host="example.com"))
    conn = MagicMock()
    conn.start_sftp_client = _start
    client._conn = conn
    return client


@pytest.mark.asyncio
async def test_put_file_sends_local_file() -> None:
    sftp = _MockSFTP()
    client = _client_with(sftp)

    await client.put_file("/usr/local/bin/wdtt-server", "/usr/local/bin/wdtt-server",
                          mode=0o755)

    assert len(sftp.put_calls) == 1, "файл должен заливаться ровно один раз"
    call = sftp.put_calls[0]
    assert call["local"] == "/usr/local/bin/wdtt-server"
    assert call["remote"] == "/usr/local/bin/wdtt-server"


@pytest.mark.asyncio
async def test_put_file_sets_mode() -> None:
    """Права обязаны выставиться: без бита исполнения программа не запустится."""
    sftp = _MockSFTP()
    client = _client_with(sftp)

    await client.put_file("/local/bin", "/remote/bin", mode=0o755)

    from_put = sftp.put_calls[0]["kwargs"]
    set_in_put = any(
        getattr(v, "permissions", None) == 0o755 for v in from_put.values()
    )
    set_by_chmod = ("/remote/bin", 0o755) in sftp.chmod_calls
    assert set_in_put or set_by_chmod, "права 755 не выставлены ни при создании, ни chmod"


@pytest.mark.asyncio
async def test_put_file_no_connection() -> None:
    client = SSHClient(SSHCredentials(host="example.com"))
    client._conn = None

    with pytest.raises(SSHError, match="соединени"):
        await client.put_file("/local", "/remote")


@pytest.mark.asyncio
async def test_put_file_wraps_sftp_error() -> None:
    """Сбой заливки не должен вылетать наружу чужим типом: мастер ловит SSHError."""
    import asyncssh

    class _Failing(_MockSFTP):
        async def put(self, local, remote, **kwargs):
            raise asyncssh.SFTPFailure(4, "disk full")

    client = _client_with(_Failing())

    with pytest.raises(SSHError):
        await client.put_file("/local", "/remote", mode=0o755)
