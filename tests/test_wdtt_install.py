"""Автоустановка обхода БС на новый сервер.

Повод — 10.08.2026: на германскую ноду мастер поставил только VPN, а тумблер
«Обход БС» админ включил руками. Юзер выбирал обход на этой ноде и получал
«на сервере заминка», потому что программы там не было вовсе.

Порядок шагов взят из ручной установки, которая реально сработала:
залить программу → написать службу → запустить → убедиться, что управляющий
сокет отвечает. Тумблер включается ТОЛЬКО после последнего шага: служба может
подняться и тут же упасть (демон падает, если у него нет ни одного активного
пароля), и «active» сам по себе ничего не доказывает.
"""
import pytest

from bot.services.ssh import CommandResult, SSHError
from bot.services.wdtt_install import UNIT_PATH, install


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Паузы ожидания готовности демона тут не нужны: сервер поддельный и
    отвечает мгновенно. Без этого два теста на отказ ждут по 10 секунд."""
    monkeypatch.setattr("bot.services.wdtt_install._READY_DELAY", 0)


class _FakeSSH:
    """Сервер, который отвечает так, как задано в конструкторе."""

    def __init__(
        self,
        order: list[str],
        *,
        already_active: bool = False,
        ctl_ok: bool = True,
        starts: bool = True,
        put_raises: bool = False,
    ) -> None:
        self.order = order
        self.already_active = already_active
        self.ctl_ok = ctl_ok
        self.starts = starts
        self.put_raises = put_raises
        self.written: dict[str, str] = {}
        self.put_files: list[tuple[str, str]] = []

    async def put_file(self, local_path: str, remote_path: str, *, mode: int = 0o644) -> None:
        if self.put_raises:
            raise SSHError("не удалось залить")
        self.order.append(f"put {remote_path}")
        self.put_files.append((local_path, remote_path))

    async def write_file(self, path: str, content: str, *, mode: int = 0o600) -> None:
        self.order.append(f"write {path}")
        self.written[path] = content

    async def run(self, cmd: str, *, check: bool = False, timeout=None) -> CommandResult:
        self.order.append(cmd)
        if "is-active" in cmd:
            active = self.already_active or (
                self.starts and any("enable --now" in c for c in self.order)
            )
            return CommandResult(cmd=cmd, exit_code=0 if active else 3,
                                 stdout="active\n" if active else "inactive\n", stderr="")
        if "ctl -op list" in cmd:
            out = '{"ok":true,"passwords":[]}' if self.ctl_ok else ""
            return CommandResult(cmd=cmd, exit_code=0 if self.ctl_ok else 127,
                                 stdout=out, stderr="" if self.ctl_ok else "No such file")
        return CommandResult(cmd=cmd, exit_code=0, stdout="", stderr="")


async def _noop(_text: str) -> None:
    pass


async def test_install_success() -> None:
    order: list[str] = []
    ssh = _FakeSSH(order)

    assert await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop) is True


async def test_install_steps_in_order() -> None:
    """Программа заливается до службы, служба пишется до запуска,
    проверка — после запуска. Иначе запускать нечего."""
    order: list[str] = []
    ssh = _FakeSSH(order)

    await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop)

    put = next(i for i, c in enumerate(order) if c.startswith("put "))
    unit = next(i for i, c in enumerate(order) if c.startswith(f"write {UNIT_PATH}"))
    start = next(i for i, c in enumerate(order) if "enable --now" in c)
    ctl = next(i for i, c in enumerate(order) if "ctl -op list" in c)
    assert put < unit < start < ctl, f"порядок шагов нарушен: {order}"


async def test_install_is_idempotent() -> None:
    """Повторный прогон на живом сервере ничего не трогает.

    Перезапись службы означала бы новый пароль владельца и перезапуск демона
    — то есть обрыв всем, кто прямо сейчас сидит через обход.
    """
    order: list[str] = []
    ssh = _FakeSSH(order, already_active=True)

    assert await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop) is True
    assert not ssh.put_files, "программа перезалита на работающем сервере"
    assert UNIT_PATH not in ssh.written, "служба переписана на работающем сервере"


async def test_install_false_when_service_dead() -> None:
    order: list[str] = []
    ssh = _FakeSSH(order, starts=False)

    assert await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop) is False


async def test_install_false_when_control_socket_silent() -> None:
    """Служба «active», но управляющий сокет молчит — обход нерабочий.

    Ровно этот случай бот и обязан поймать: именно через сокет он выдаёт
    доступы, и без него юзер получит «на сервере заминка».
    """
    order: list[str] = []
    ssh = _FakeSSH(order, ctl_ok=False)

    assert await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop) is False


async def test_install_false_when_upload_fails() -> None:
    order: list[str] = []
    ssh = _FakeSSH(order, put_raises=True)

    assert await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop) is False


async def test_unit_has_owner_password() -> None:
    """Без пароля владельца демон не стартует вовсе (проверено по исходнику:
    `[WRAP] нет активных паролей для WRAP` — и процесс падает)."""
    order: list[str] = []
    ssh = _FakeSSH(order)

    await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop)

    unit = ssh.written[UNIT_PATH]
    assert "-password " in unit, "в службе нет пароля владельца — демон не поднимется"
    password = unit.split("-password ")[1].split()[0]
    assert len(password) >= 12, "пароль владельца слишком короткий"


async def test_owner_password_is_random_per_server() -> None:
    """Один пароль на все серверы означал бы, что утечка с одной ноды
    открывает обход на всех остальных."""
    first, second = [], []
    ssh1 = _FakeSSH(first)
    ssh2 = _FakeSSH(second)
    await install(ssh1, ports="56000,56001,9000", dns=None, progress=_noop)
    await install(ssh2, ports="56000,56001,9000", dns=None, progress=_noop)

    p1 = ssh1.written[UNIT_PATH].split("-password ")[1].split()[0]
    p2 = ssh2.written[UNIT_PATH].split("-password ")[1].split()[0]
    assert p1 != p2, "пароль владельца одинаков на разных серверах"


async def test_unit_uses_given_ports_and_dns() -> None:
    order: list[str] = []
    ssh = _FakeSSH(order)

    await install(ssh, ports="57000,57001,9000", dns="9.9.9.9", progress=_noop)

    unit = ssh.written[UNIT_PATH]
    assert "0.0.0.0:57000" in unit
    assert "-wg-port 57001" in unit
    assert "-dns 9.9.9.9" in unit


async def test_service_is_enabled_for_reboot() -> None:
    """Служба без автозапуска переживёт установку, но не перезагрузку."""
    order: list[str] = []
    ssh = _FakeSSH(order)

    await install(ssh, ports="56000,56001,9000", dns=None, progress=_noop)

    assert any("enable" in c for c in order), "обход не встанет после перезагрузки"


async def test_password_never_leaves_unit_file() -> None:
    """Пароль владельца — секрет. Он обязан попасть ровно в один файл службы
    и никуда больше: ни в команду шелла (её видно в `ps` и в отладочном
    журнале бота), ни в сообщение о ходе установки, которое читает админ."""
    order: list[str] = []
    shown: list[str] = []

    async def progress(text: str) -> None:
        shown.append(text)

    ssh = _FakeSSH(order)
    await install(ssh, ports="56000,56001,9000", dns=None, progress=progress)

    password = ssh.written[UNIT_PATH].split("-password ")[1].split()[0]
    assert all(password not in c for c in order), "пароль владельца ушёл в команду шелла"
    assert all(password not in t for t in shown), "пароль владельца показан админу"
