"""Название локации — тоже чужой текст, и его надо экранировать.

Найдено аудитом 20.08.2026. Локация вводится админом свободным текстом (шаг
установки сервера и кнопка «🌍 Локация» в карточке) и нигде не проверяется: в
базу уходит `raw[:64]` как есть. Дальше она подставляется в HTML-разметку
экранов, которые видят ВСЕ юзеры: витрина «🌍 Локации», подписи к конфигам,
блоки со ссылками.

Одна угловая скобка в названии — и Telegram отказывается принимать сообщение.
То есть опечатка админа ломает выдачу конфигов всем сразу, а причина выглядит
как «бот сломался».

Тот же класс, что и имя из профиля Telegram, которое чинили этим же утром, —
только источник другой.
"""
from __future__ import annotations

import pytest

from bot.db.models import Server
from bot.texts import ui

EVIL = "<b>Германия</b> & Co"


def _server(location: str | None, name: str = "de1") -> Server:
    return Server(id=1, name=name, host="1.1.1.1", wg_port=585, location=location)


class TestDisplayBase:
    def test_escapes_location(self) -> None:
        from bot.handlers.configs import config_display_base

        out = config_display_base(_server(EVIL))
        assert "<b>" not in out
        assert "&lt;b&gt;" in out

    def test_plain_location_survives_readable(self) -> None:
        from bot.handlers.configs import config_display_base

        assert config_display_base(_server("🇳🇱 Нидерланды")) == "🇳🇱 Нидерланды"

    def test_falls_back_to_server_name(self) -> None:
        from bot.handlers.configs import config_display_base

        assert config_display_base(_server(None, name="nl1")) == "nl1"


class TestFilenameStillClean:
    def test_filename_has_no_escapes(self) -> None:
        """В имя файла экранирование попасть не должно: там нужен читаемый
        текст, а не «&lt;». Amnezia называет конфиг по имени файла."""
        from bot.handlers.config_delivery import _conf_filename

        name = _conf_filename(_server("🇳🇱 Нидерланды"), "phone")
        assert "&" not in name
        assert name.endswith(".conf")


class TestLocationsScreen:
    @pytest.mark.asyncio
    async def test_showcase_escapes_locations(self, session) -> None:
        """Витрина «🌍 Локации» — экран для всех. Кривая локация не должна
        обрушить его целиком."""
        from bot.db import repo
        from bot.db.models import ServerStatus
        from bot.handlers.common import build_locations_text

        await repo.create_server(
            session, name="s1", host="1.1.1.1", wg_port=585, owner_tg_id=1,
            status=ServerStatus.READY, location=EVIL,
            server_public_key="pub", server_endpoint="1.1.1.1:585",
        )
        user = await repo.get_or_create_user(
            session, tg_id=9301, username="u", full_name="U"
        )
        text = await build_locations_text(session, user)
        assert "<b>Германия</b>" not in text
        assert "&lt;b&gt;" in text


class TestLocationValidation:
    """Второй рубеж: кривое название не должно попадать в базу вовсе.

    Экранирование на выводе спасает уже существующие данные, но новое кривое
    значение всплывёт в первом же экране, где экранировать забудут.
    """

    @pytest.mark.parametrize("bad", ["<b>Германия", "A & B", "Гер>мания", "", "   ", "x" * 65])
    def test_rejects_markup_and_junk(self, bad: str) -> None:
        from bot.utils.validators import clean_location

        assert clean_location(bad) is None

    @pytest.mark.parametrize("good", ["🇩🇪 Германия", "Нидерланды", "🇳🇱 Нидерланды 2", "USA-East"])
    def test_accepts_normal_names(self, good: str) -> None:
        from bot.utils.validators import clean_location

        assert clean_location(good) == good

    def test_collapses_double_spaces(self) -> None:
        """«🇩🇪  Германия» с двумя пробелами плодила вторую локацию-двойник."""
        from bot.utils.validators import clean_location

        assert clean_location("🇩🇪  Германия ") == "🇩🇪 Германия"
