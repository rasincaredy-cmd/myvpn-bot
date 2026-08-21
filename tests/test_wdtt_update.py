"""Обновление программы резервного подключения на нодах (21.08.2026).

Обновление рвёт живые подключения и подменяет единственный бинарь, через
который бот вообще выдаёт доступы. Поэтому проверка «взлетело» тут не
формальность: служба может быть `active` при онемевшем управляющем сокете —
демон падает без активных паролей и тут же поднимается по Restart=always. Если
поверить systemd, нода останется с новой программой, которая не выдаёт ничего,
и узнаем мы об этом от юзера.

Тесты держат три обещания: бэкап делается ДО подмены, откат случается на любом
неудачном исходе, и одинаковая версия не трогается вовсе.
"""
from __future__ import annotations

import json

import pytest

from bot.services import wdtt_update
from bot.services.ssh import SSHError

REF = "a" * 64
OLD = "b" * 64


class _Res:
    def __init__(self, stdout: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code


class FakeSSH:
    """Нода, которая ведёт себя по сценарию.

    `sha` — что лежит на диске сейчас (меняется при `mv`), `socket` — отвечает
    ли управляющий сокет, `accesses` — сколько на нём доступов.
    """

    def __init__(self, *, sha=OLD, active=True, socket=True, accesses=3,
                 after_update=None) -> None:
        self.sha = sha
        self.active = active
        self.socket = socket
        self.accesses = accesses
        # Каким станет узел после подмены программы: (socket, accesses).
        self.after_update = after_update
        self.log: list[str] = []
        self.put: list[tuple[str, str]] = []
        self.fail_on: str | None = None

    async def run(self, cmd, *, check=False, timeout=None):
        self.log.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            raise SSHError("нода отвалилась")
        if "sha256sum" in cmd:
            if self.sha is None:
                return _Res("MISSING")
            return _Res(f"{self.sha}\n8000000\n")
        if "systemctl is-active" in cmd:
            return _Res("active" if self.active else "inactive")
        if "ctl -op list" in cmd:
            if not self.socket:
                return _Res("", exit_code=1)
            body = {"ok": True, "passwords": [{"password": f"p{i}"} for i in range(self.accesses)]}
            return _Res(json.dumps(body))
        if cmd.startswith("mv -f"):
            self.sha = REF
            if self.after_update is not None:
                self.socket, self.accesses = self.after_update
            return _Res()
        if cmd.startswith("cp -f") and ".bak-" in cmd and cmd.index(".bak-") > cmd.index("cp -f"):
            # Откат: `cp -f <бэкап> <бинарь>` — возвращаем прежнее состояние.
            src = cmd.split()[2]
            if src.endswith(f".bak-{OLD[:8]}"):
                self.sha = OLD
                self.socket, self.accesses = True, 3
            return _Res()
        return _Res()

    async def put_file(self, local, remote, *, mode=0o644):
        self.put.append((local, remote))


@pytest.fixture(autouse=True)
def _fast_and_stable(monkeypatch):
    monkeypatch.setattr(wdtt_update, "_READY_DELAY", 0)
    monkeypatch.setattr(wdtt_update, "reference_sha256", lambda: REF)


class TestProbe:
    @pytest.mark.asyncio
    async def test_reads_version_and_accesses(self) -> None:
        p = await wdtt_update.probe(FakeSSH())
        assert p.installed and p.active and p.socket_ok
        assert p.sha256 == OLD and p.accesses == 3
        assert p.short == OLD[:8]

    @pytest.mark.asyncio
    async def test_missing_binary(self) -> None:
        p = await wdtt_update.probe(FakeSSH(sha=None))
        assert not p.installed and p.accesses is None

    @pytest.mark.asyncio
    async def test_active_service_with_dead_socket_is_not_ok(self) -> None:
        """Главная ловушка: демон перезапускается по кругу, systemd доволен."""
        p = await wdtt_update.probe(FakeSSH(socket=False))
        assert p.active and not p.socket_ok


class TestUpdate:
    @pytest.mark.asyncio
    async def test_same_version_is_left_alone(self) -> None:
        ssh = FakeSSH(sha=REF)
        res = await wdtt_update.update(ssh)
        assert res.ok and not res.changed
        assert not ssh.put, "трогали ноду, на которой и так эталон"

    @pytest.mark.asyncio
    async def test_force_updates_even_the_same_version(self) -> None:
        ssh = FakeSSH(sha=REF)
        res = await wdtt_update.update(ssh, force=True)
        assert res.ok and res.changed and ssh.put

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        ssh = FakeSSH()
        res = await wdtt_update.update(ssh)
        assert res.ok and res.changed and not res.rolled_back
        assert ssh.sha == REF

    @pytest.mark.asyncio
    async def test_backup_happens_before_the_swap(self) -> None:
        """Бэкап после подмены бесполезен — откатывать будет не на что."""
        ssh = FakeSSH()
        await wdtt_update.update(ssh)
        backup = next(i for i, c in enumerate(ssh.log) if c.startswith("cp -f"))
        swap = next(i for i, c in enumerate(ssh.log) if c.startswith("mv -f"))
        assert backup < swap

    @pytest.mark.asyncio
    async def test_uploads_to_a_temp_path_not_over_the_running_binary(self) -> None:
        """Заливка поверх работающего файла даёт «text file busy», и нода
        остаётся вообще без программы."""
        ssh = FakeSSH()
        await wdtt_update.update(ssh)
        assert ssh.put and ssh.put[0][1].endswith(".new")

    @pytest.mark.asyncio
    async def test_silent_socket_rolls_back(self) -> None:
        ssh = FakeSSH(after_update=(False, 0))
        res = await wdtt_update.update(ssh)
        assert not res.ok and res.rolled_back
        assert ssh.sha == OLD, "остались на неисправной версии"

    @pytest.mark.asyncio
    async def test_lost_accesses_roll_back(self) -> None:
        """Сокет отвечает, но доступы пропали — значит, программа не увидела
        базу паролей. Для юзеров это то же самое, что мёртвая нода."""
        ssh = FakeSSH(after_update=(True, 1))
        res = await wdtt_update.update(ssh)
        assert not res.ok and res.rolled_back
        assert ssh.sha == OLD

    @pytest.mark.asyncio
    async def test_missing_binary_is_not_an_update(self) -> None:
        res = await wdtt_update.update(FakeSSH(sha=None))
        assert not res.ok and not res.changed
        assert "установка" in res.detail

    @pytest.mark.asyncio
    async def test_no_reference_no_update(self, monkeypatch) -> None:
        """Без эталона на ноде бота обновлять нечем — и молчать об этом нельзя."""
        monkeypatch.setattr(wdtt_update, "reference_sha256", lambda: None)
        res = await wdtt_update.update(FakeSSH())
        assert not res.ok and "эталон" in res.detail

    @pytest.mark.asyncio
    async def test_upload_failure_leaves_the_node_alone(self) -> None:
        ssh = FakeSSH()
        ssh.fail_on = "mv -f"
        res = await wdtt_update.update(ssh)
        assert not res.ok and ssh.sha == OLD


# --- Экран админа ------------------------------------------------------------

class TestNodesScreen:
    def _probe(self, **kw):
        base = dict(installed=True, active=True, socket_ok=True,
                    sha256=REF, size=8_000_000, accesses=2)
        base.update(kw)
        return wdtt_update.Probe(**base)

    class _Srv:
        def __init__(self, name: str) -> None:
            self.id = 1
            self.name = name

    def test_matching_version_is_marked_ok(self) -> None:
        from bot.handlers.admin.wdtt_nodes import _node_line

        line = _node_line(self._Srv("nl1"), self._probe(), REF)
        assert line.startswith("✅") and "отличается" not in line

    def test_drift_is_visible(self) -> None:
        from bot.handlers.admin.wdtt_nodes import _node_line

        line = _node_line(self._Srv("de1"), self._probe(sha256=OLD), REF)
        assert "отличается от эталона" in line

    def test_dead_socket_beats_the_version(self) -> None:
        """Онемевший сокет важнее версии: доступы на такой ноде не выдаются."""
        from bot.handlers.admin.wdtt_nodes import _node_line

        line = _node_line(self._Srv("de1"), self._probe(socket_ok=False), REF)
        assert line.startswith("⚠️") and "сокет молчит" in line

    def test_server_name_is_escaped(self) -> None:
        """Имя сервера вводит админ свободным текстом — угловая скобка в нём
        сделала бы сообщение непарсимым, и экран не открылся бы вообще."""
        from bot.handlers.admin.wdtt_nodes import _node_line

        line = _node_line(self._Srv("<b>nl1"), self._probe(), REF)
        assert "<b>nl1" not in line and "&lt;b&gt;nl1" in line

    def test_update_all_button_appears_only_when_there_is_work(self) -> None:
        from bot.keyboards.inline import wdtt_nodes_kb

        def datas(kb):
            return [b.callback_data for row in kb.inline_keyboard for b in row]

        assert "pnl:wdttup:all" in datas(wdtt_nodes_kb([(1, "nl1")], [1]))
        assert "pnl:wdttup:all" not in datas(wdtt_nodes_kb([(1, "nl1")], []))
