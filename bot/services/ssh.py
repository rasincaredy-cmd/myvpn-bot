from __future__ import annotations

import asyncio
from dataclasses import dataclass

import asyncssh
from loguru import logger

from bot.config import settings


class SSHError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class SSHCredentials:
    host: str
    port: int = 22
    username: str = "root"
    password: str | None = None
    private_key: str | None = None
    key_passphrase: str | None = None

    def __repr__(self) -> str:
        # Маскируем секреты — этот объект может попасть в стек-трейсы и логи.
        return (
            f"SSHCredentials(host={self.host!r}, port={self.port}, "
            f"username={self.username!r}, "
            f"password={'***' if self.password else None}, "
            f"private_key={'***' if self.private_key else None})"
        )


@dataclass(slots=True)
class CommandResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SSHClient:
    """Async-context-manager обёртка над asyncssh с тайм-аутами."""

    def __init__(self, creds: SSHCredentials) -> None:
        self._creds = creds
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> "SSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        c = self._creds
        # known_hosts=None — пользователь сам решает, кому доверять, бот лезет
        # только туда, куда ему сказали.
        connect_kwargs: dict[str, object] = {
            "host": c.host,
            "port": c.port,
            "username": c.username,
            "known_hosts": None,
            "connect_timeout": settings.ssh_connect_timeout,
        }
        if c.password:
            connect_kwargs["password"] = c.password
        if c.private_key:
            try:
                key = asyncssh.import_private_key(
                    c.private_key, passphrase=c.key_passphrase or None
                )
            except (asyncssh.KeyImportError, ValueError) as exc:
                raise SSHError(f"Некорректный SSH-ключ: {exc}") from exc
            connect_kwargs["client_keys"] = [key]

        logger.info("SSH connect to {}@{}:{}", c.username, c.host, c.port)
        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(**connect_kwargs),
                timeout=settings.ssh_connect_timeout + 5,
            )
        except asyncio.TimeoutError as exc:
            raise SSHError(f"Тайм-аут подключения к {c.host}:{c.port}") from exc
        except (asyncssh.PermissionDenied,) as exc:
            raise SSHError("SSH: доступ запрещён (проверь логин/пароль/ключ)") from exc
        except (OSError, asyncssh.Error) as exc:
            raise SSHError(f"SSH-ошибка: {exc}") from exc

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def run(
        self,
        cmd: str,
        *,
        check: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        if self._conn is None:
            raise SSHError("SSH-соединение не открыто")

        # Усечённое превью — в команде могут быть длинные heredoc'и или секреты.
        safe_preview = cmd if len(cmd) < 120 else cmd[:117] + "..."
        logger.debug("ssh$ {}", safe_preview)

        try:
            res = await asyncio.wait_for(
                self._conn.run(cmd, check=False),
                timeout=timeout or settings.ssh_command_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise SSHError(f"Тайм-аут выполнения команды (> {timeout}s)") from exc

        result = CommandResult(
            cmd=cmd,
            exit_code=res.exit_status if res.exit_status is not None else -1,
            stdout=str(res.stdout or ""),
            stderr=str(res.stderr or ""),
        )
        if check and not result.ok:
            raise SSHError(
                f"Команда упала ({result.exit_code}): {safe_preview}\n"
                f"stderr: {result.stderr.strip()[:500]}"
            )
        return result

    async def read_file(self, path: str) -> str:
        async with self._conn.start_sftp_client() as sftp:  # type: ignore[union-attr]
            async with sftp.open(path, "r") as f:
                return await f.read()
