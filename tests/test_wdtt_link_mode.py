"""Третий шаг инструкции к резервному подключению — куда вставлять ссылку.

История шага: на Android в старом приложении WDTT поле для ссылки прятали за
тумблером «Режим ссылки», и без этого шага человек упирался в пустой экран —
ровно та точка, где юзер молча уходит (найдено на живом Samsung 17.08.2026).

21.08.2026 приложение сменилось: WDTT архивирован автором, вместо него qWDTT,
и путь импорта там другой — «Профили» → «+» → «Из буфера». Путь взят из
исходников приложения, а не по памяти.

Постоянное требование, которое и стережёт этот тест: шаг ЕСТЬ всегда (без него
инструкция обрывается на скопированной ссылке), нумерация не прыгает, а точный
путь по кнопкам называется только там, где мы его знаем. Отправить человека
искать кнопку, которой у него нет, — тот же тупик, что и промолчать.
"""

import pytest

from bot.handlers.wdtt import _app_block, _import_hint, _import_step
from bot.texts import t


def _created(platform: str) -> str:
    return t.wdtt_created.format(
        label="iPhone", server="Нидерланды", app="qWDTT",
        app_block=_app_block(platform), link="wdtt://xxx",
        import_step=_import_step(platform),
    )


def _link(platform: str | None) -> str:
    return t.wdtt_link.format(
        link="wdtt://xxx", app_line="Импортируй её в приложение.",
        import_hint=_import_hint(platform),
    )


class TestCreated:
    def test_android_gets_the_exact_path(self) -> None:
        text = _created("android")
        assert "Профили" in text and "Из буфера" in text

    @pytest.mark.parametrize("platform", ["ios", "pc"])
    def test_other_platforms_get_a_generic_step(self, platform: str) -> None:
        """Путь по кнопкам там не проверен — называть его нельзя."""
        text = _created(platform)
        assert "Профили" not in text
        assert "Импортируй ссылку" in text

    @pytest.mark.parametrize("platform", ["android", "ios", "pc"])
    def test_three_steps_in_order_always(self, platform: str) -> None:
        """Шаг про импорт есть всегда: без него инструкция обрывается на
        «ссылка скопирована» — и человек не знает, что дальше."""
        text = _created(platform)
        assert text.index("1️⃣") < text.index("2️⃣") < text.index("3️⃣")

    @pytest.mark.parametrize("platform", ["android", "ios", "pc"])
    def test_link_comes_before_the_import_step(self, platform: str) -> None:
        """Сначала копируем ссылку, потом идём вставлять: наоборот в буфере
        нечего вставлять."""
        text = _created(platform)
        assert text.index("<code>") < text.index("3️⃣")


class TestLinkAgain:
    def test_android_gets_the_reminder(self) -> None:
        assert "Из буфера" in _link("android")

    def test_ios_does_not(self) -> None:
        assert "Из буфера" not in _link("ios")

    def test_old_access_without_platform_does_not_crash(self) -> None:
        # У доступов, выданных до появления платформы, platform = None.
        assert "Из буфера" not in _link(None)


class TestApp:
    def test_android_points_to_the_maintained_app(self) -> None:
        """Прежнее приложение архивировано автором: его репозиторий встречает
        человека надписью «разработка и поддержка прекращены»."""
        block = _app_block("android")
        assert "SpaceNeuroX" in block
        assert "amurcanov" not in block
