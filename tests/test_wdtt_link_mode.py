"""Шаг «включи тумблер «Режим ссылки»» в инструкции к резервному подключению.

Без этого тумблера на Android приложение не показывает, куда вставлять ссылку,
и человек упирается в пустой экран — ровно та точка, где юзер молча уходит.
Шаг обязан быть на Android и обязан НЕ появляться там, где такой настройки не
видели: отправить человека искать несуществующий тумблер — тоже потерять его.

Отдельно проверяем нумерацию: пока шаг был жёстко зашит, при его отсутствии
инструкция читалась как «1, 3».
"""

from bot.handlers.wdtt import _app_block, _link_mode
from bot.texts import t


def _created(platform: str) -> str:
    return t.wdtt_created.format(
        label="iPhone", server="Нидерланды", app="WDTT",
        app_block=_app_block(platform), link="wdtt://xxx",
        link_mode=t.wdtt_link_mode if _link_mode(platform) else "",
        n="3️⃣" if _link_mode(platform) else "2️⃣",
    )


def _link(platform: str | None) -> str:
    return t.wdtt_link.format(
        link="wdtt://xxx", app_line="Импортируй её в приложение.",
        link_mode=t.wdtt_link_mode_short if _link_mode(platform) else "",
    )


class TestCreated:
    def test_android_gets_the_toggle_step(self):
        text = _created("android")
        assert "Режим ссылки" in text
        assert "2️⃣ Запусти приложение" in text
        assert "3️⃣ Скопируй ссылку" in text

    def test_ios_has_no_toggle_step(self):
        text = _created("ios")
        assert "Режим ссылки" not in text

    def test_pc_has_no_toggle_step(self):
        assert "Режим ссылки" not in _created("pc")

    def test_numbering_has_no_hole_without_the_step(self):
        text = _created("ios")
        assert "2️⃣ Скопируй ссылку" in text
        assert "3️⃣" not in text

    def test_steps_are_in_order_on_android(self):
        text = _created("android")
        assert text.index("1️⃣") < text.index("2️⃣") < text.index("3️⃣")


class TestLinkAgain:
    def test_android_gets_the_reminder(self):
        assert "Режим ссылки" in _link("android")

    def test_ios_does_not(self):
        assert "Режим ссылки" not in _link("ios")

    def test_old_access_without_platform_does_not_crash(self):
        # У доступов, выданных до появления платформы, platform = None.
        assert "Режим ссылки" not in _link(None)
