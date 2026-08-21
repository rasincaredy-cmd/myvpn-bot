"""Сборка серверной части резервного подключения (21.08.2026).

Сервер — чужой проект плюс ОДИН наш файл: управляющий канал, через который бот
выдаёт доступы. Хрупкое место здесь ровно одно — две строки, которые мы
вписываем в чужой `main.go` по якорям. Чужой проект живой, его переписывают, и
однажды якоря не найдутся.

Худший исход этого — не «сборка упала», а «сборка прошла без нашего канала»:
такой сервер поднимется, будет выглядеть здоровым, а бот перестанет выдавать
доступы, и узнаем мы это от первого же покупателя. Поэтому тесты требуют
громкого отказа, а не тихой сборки.
"""
from __future__ import annotations

import pytest

from bot.services import wdtt_build
from bot.services.wdtt_build import PatchError, apply_patch

THEIR_MAIN = '''package main

import (
\t"context"
\t"flag"
\t"os"
)

func main() {
\tlisten := flag.String("listen", "0.0.0.0:56000", "DTLS адрес")
\tflag.Parse()

\tinitDB(*configDir, mainPasswordValue, *adminID, botTokenValue)

\tgo statsLoop(ctx, *configDir)
\tgo expiredPasswordJanitor(ctx, wgDev)
}
'''


class TestPatch:
    def test_adds_both_pieces(self) -> None:
        out = apply_patch(THEIR_MAIN)
        assert "runCtlClient(os.Args[2:])" in out
        assert "go serveControl(ctx, wgDev)" in out

    def test_ctl_dispatch_goes_before_flag_parsing(self) -> None:
        """`ctl` — отдельный вход, сервер при нём не стартует. Окажись разбор
        флагов раньше, бинарь попытался бы поднять второй сервер на занятых
        портах вместо того, чтобы сходить в сокет."""
        out = apply_patch(THEIR_MAIN)
        assert out.index("runCtlClient") < out.index("flag.Parse()")

    def test_socket_starts_after_wireguard_is_ready(self) -> None:
        """Сокету нужен поднятый wgDev: он выдаёт и снимает пиров."""
        out = apply_patch(THEIR_MAIN)
        assert out.index("initDB(") < out.index("go serveControl")

    def test_is_idempotent(self) -> None:
        """Дерево, собранное повторно, не должно получить правку дважды."""
        once = apply_patch(THEIR_MAIN)
        assert apply_patch(once) == once

    def test_missing_main_anchor_is_loud(self) -> None:
        with pytest.raises(PatchError) as exc:
            apply_patch("package main\n\nfunc старт() {}\n")
        assert "руками" in str(exc.value)

    def test_missing_goroutine_anchor_is_loud(self) -> None:
        """Пропала вторая точка — управляющий сокет не запустится, и сервер
        будет выглядеть исправным, ничего не выдавая."""
        without = THEIR_MAIN.replace("\tgo statsLoop(ctx, *configDir)\n", "")
        with pytest.raises(PatchError):
            apply_patch(without)

    def test_patched_file_still_looks_like_go(self) -> None:
        out = apply_patch(THEIR_MAIN)
        assert out.count("func main() {") == 1
        assert out.strip().endswith("}")


class TestOurFileIsInTheRepo:
    """Правка обязана лежать в репозитории. До 21.08.2026 единственный её
    экземпляр жил в одной папке на одной машине."""

    def test_patch_file_exists(self) -> None:
        assert wdtt_build.PATCH_FILE.is_file(), wdtt_build.PATCH_FILE

    def test_patch_file_has_the_named_pieces(self) -> None:
        """Имена, которые вписываются в чужой main.go, должны существовать —
        иначе сборка развалится уже у компилятора."""
        text = wdtt_build.PATCH_FILE.read_text(encoding="utf-8")
        assert "func runCtlClient(" in text
        assert "func serveControl(" in text

    def test_patch_file_serves_every_operation_the_bot_uses(self) -> None:
        """Бот зовёт четыре операции. Пропала любая — отвалится соответствующая
        кнопка у юзера, а не сборка."""
        text = wdtt_build.PATCH_FILE.read_text(encoding="utf-8")
        for op in ('case "add"', 'case "remove"', 'case "unbind"', 'case "list"'):
            assert op in text, f"в правке нет операции {op}"


class TestBuildGuards:
    @pytest.mark.asyncio
    async def test_no_compiler_is_reported_not_crashed(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(wdtt_build, "GO_BIN", tmp_path / "нет-такого")
        res = await wdtt_build.build()
        assert not res.ok and "компилятора" in res.detail

    @pytest.mark.asyncio
    async def test_missing_patch_file_stops_the_build(self, monkeypatch, tmp_path) -> None:
        """Собрать чужой сервер без нашего канала — худший возможный исход:
        он поднимется, а доступы выдавать будет нечем."""
        monkeypatch.setattr(wdtt_build, "GO_BIN", __import__("pathlib").Path(__file__))
        monkeypatch.setattr(wdtt_build, "PATCH_FILE", tmp_path / "нет.go")
        res = await wdtt_build.build()
        assert not res.ok and "правки" in res.detail

    @pytest.mark.asyncio
    async def test_alive_check_accepts_both_answers(self, monkeypatch, tmp_path) -> None:
        """На ноде бота рядом живой сервер (ответ JSON), на голой машине —
        жалоба на сокет. Оба доказывают, что наш канал внутри."""
        binary = tmp_path / "wdtt-server"
        binary.write_text("")

        async def json_answer(cmd, cwd, timeout):
            return 0, '{"ok":true,"passwords":[]}'

        async def dial_error(cmd, cwd, timeout):
            return 1, "ctl: dial: no such file or directory"

        async def stranger(cmd, cwd, timeout):
            return 2, "flag provided but not defined: -op"

        monkeypatch.setattr(wdtt_build, "_run", json_answer)
        assert await wdtt_build._looks_alive(binary)
        monkeypatch.setattr(wdtt_build, "_run", dial_error)
        assert await wdtt_build._looks_alive(binary)
        monkeypatch.setattr(wdtt_build, "_run", stranger)
        assert not await wdtt_build._looks_alive(binary), "чужая сборка принята за нашу"
