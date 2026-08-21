"""Новости о версиях приложения резервного подключения (21.08.2026).

Серверная часть у нас теперь чужая плюс наш файл сверху, и обновляется она не
сама. Пока никто не следит за релизами, «не отстанем» держится на том, что
кто-то вспомнил зайти на страницу проекта.

Три обещания: молчим, когда не знаем (сеть отвалилась — это не «нового нет»);
молчим на первом запуске (иначе бот начнёт с новости о той версии, что у нас и
так стоит); говорим ровно один раз на версию.
"""
from __future__ import annotations

import pytest

from bot.services import health, upstream


class TestLatestRelease:
    @pytest.mark.asyncio
    async def test_none_on_network_failure(self, monkeypatch) -> None:
        """Ошибка сети — это «не знаю», а не «нового нет»."""
        class _Boom:
            def __call__(self, *a, **kw):
                raise RuntimeError("сети нет")

        monkeypatch.setattr(upstream.aiohttp, "ClientSession", _Boom())
        assert await upstream.latest_release() is None

    def test_message_names_the_version_and_links(self) -> None:
        text = upstream.release_message("v1.4.3")
        assert "v1.4.3" in text
        assert upstream.RELEASES_URL in text


class TestNotice:
    @pytest.fixture(autouse=True)
    def _no_telegram(self, monkeypatch):
        self.sent: list[str] = []

        async def fake_notify(text: str) -> None:
            self.sent.append(text)

        monkeypatch.setattr(health, "_notify_admins", fake_notify)

    def _pin(self, monkeypatch, tag):
        async def fake_latest():
            return tag

        monkeypatch.setattr(upstream, "latest_release", fake_latest)

    @pytest.mark.asyncio
    async def test_first_run_is_silent_but_remembers(self, monkeypatch) -> None:
        self._pin(monkeypatch, "v1.4.2")
        state: dict = {}
        await health._upstream_release_notice(state)
        assert self.sent == []
        assert state["qwdtt_release"] == "v1.4.2"

    @pytest.mark.asyncio
    async def test_new_version_is_announced_once(self, monkeypatch) -> None:
        self._pin(monkeypatch, "v1.4.3")
        state = {"qwdtt_release": "v1.4.2"}
        await health._upstream_release_notice(state)
        assert len(self.sent) == 1 and "v1.4.3" in self.sent[0]

        # Второй тик по той же версии обязан молчать: повторяющаяся новость
        # перестаёт читаться, и следующую настоящую пролистают вместе с ней.
        await health._upstream_release_notice(state)
        assert len(self.sent) == 1

    @pytest.mark.asyncio
    async def test_same_version_is_silent(self, monkeypatch) -> None:
        self._pin(monkeypatch, "v1.4.2")
        state = {"qwdtt_release": "v1.4.2"}
        await health._upstream_release_notice(state)
        assert self.sent == []

    @pytest.mark.asyncio
    async def test_unknown_version_does_not_erase_memory(self, monkeypatch) -> None:
        """Сеть отвалилась — нельзя забывать, что мы уже видели: иначе
        следующий успешный запрос сочтёт старую версию новой."""
        self._pin(monkeypatch, None)
        state = {"qwdtt_release": "v1.4.2"}
        await health._upstream_release_notice(state)
        assert self.sent == []
        assert state["qwdtt_release"] == "v1.4.2"
