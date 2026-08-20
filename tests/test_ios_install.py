"""Установка на iPhone — один текст на весь бот (20.08.2026).

Приложения нет в российском App Store, и до этой правки бот отвечал ссылкой на
документацию Amnezia — в ЧЕТЫРЁХ местах отдельными копиями. Ссылка живая и
правильная, но это не скачивание: человек уходил на сторонний сайт читать
длинную страницу вместо того, что все и так делают — смены страны магазина.
Влад поймал это на экране поддержки.

Четыре копии — это гарантия, что следующая правка обновит одну и забудет три.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEXTS = (ROOT / "bot" / "texts" / "ru.py").read_text(encoding="utf-8")


def test_ios_doc_link_is_gone() -> None:
    """Ссылка на инструкцию Amnezia больше не предлагается вместо действия."""
    assert "installing-amneziavpn-on-ios" not in TEXTS


def test_instruction_lives_in_one_place() -> None:
    """Текст один и подставляется, а не копируется: иначе правка обновит одну
    копию из четырёх."""
    from bot.texts.ru import IOS_INSTALL

    assert IOS_INSTALL.count("App Store") >= 1
    # Сам текст встречается в файле ровно один раз — остальные экраны его
    # подставляют по имени.
    body = IOS_INSTALL.split("—")[1][:30].strip()
    assert TEXTS.count(body) == 1, "инструкция снова размножилась копиями"


def test_instruction_names_the_actual_steps() -> None:
    """Человеку нужен путь в настройках и что выбрать, а не «поменяй регион»."""
    from bot.texts.ru import IOS_INSTALL

    for expected in ("Настройки", "Медиаматериалы", "Страна", "AmneziaVPN"):
        assert expected in IOS_INSTALL, f"в инструкции нет «{expected}»"


def test_instruction_warns_about_apple_id_balance() -> None:
    """Смена страны не проходит с ненулевым балансом Apple ID и активными
    подписками — без этого человек упрётся в отказ и решит, что мы соврали."""
    from bot.texts.ru import IOS_INSTALL

    assert "баланс" in IOS_INSTALL.lower()


# Все четыре экрана, где заходит речь об установке. Список явный: если
# появится пятый со своей копией текста, тест о нём не узнает, зато
# test_instruction_lives_in_one_place поймает саму копию.
@pytest.mark.parametrize(
    "attr",
    ["help_text", "onboard_help", "device_created", "invite_config_created"],
)
def test_screens_use_the_shared_instruction(attr: str) -> None:
    from bot.texts import t
    from bot.texts.ru import IOS_INSTALL

    text = getattr(t, attr)
    assert IOS_INSTALL.strip() in text, f"{attr} не подставляет общую инструкцию"


def test_instruction_gives_a_direct_app_link() -> None:
    """Смена страны — только полдела: дальше человеку нужна сама ссылка, а не
    «ищи в App Store». Влад попросил вставить её 20.08.2026.

    Ссылка проверена живой (200 без редиректов) и взята с amnezia.org/downloads,
    а не по памяти.
    """
    from bot.texts.ru import IOS_INSTALL

    assert "apps.apple.com" in IOS_INSTALL
    assert "id1600529900" in IOS_INSTALL


def test_instruction_explains_the_region_error() -> None:
    """Прямая ссылка на аккаунте без смены страны отвечает «недоступно в вашем
    регионе». Без объяснения это читается как «сервис врёт»."""
    from bot.texts.ru import IOS_INSTALL

    assert "регионе" in IOS_INSTALL
