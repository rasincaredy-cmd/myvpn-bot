"""Сторож дизайн-системы (Блок «Облик», 20.08.2026).

Правила интерфейса легко нарушить, добавляя один экран: где-то «« В меню», где-то
«‹ Меню», где-то «Моя подписка», где-то «Подписка». Через месяц бот выглядит
собранным из трёх разных ботов. Тесты держат согласованность механически.

Смотрим только экраны ПОЛЬЗОВАТЕЛЯ: админские клавиатуры живут своей жизнью,
их видит один человек, и единообразие там не окупается.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Клавиатуры, которые видит платящий человек.
USER_KEYBOARDS = [
    "bot/keyboards/inline/menu.py",
    "bot/keyboards/inline/devices.py",
    "bot/keyboards/inline/balance.py",
    "bot/keyboards/inline/wdtt.py",
    "bot/keyboards/inline/support.py",
    # Кнопки, которые строятся прямо в хендлерах пользователя, — те же правила.
    "bot/handlers/balance.py",
    "bot/handlers/devices.py",
    "bot/handlers/wdtt.py",
    "bot/handlers/common.py",
]


def _button_texts(path: Path) -> list[tuple[int, str]]:
    """Все подписи кнопок файла: (строка, текст)."""
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "text":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                found.append((node.lineno, kw.value.value))
            elif isinstance(kw.value, ast.JoinedStr):
                # f-строка: берём литеральные куски, их достаточно для правил.
                parts = [
                    v.value for v in kw.value.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
                found.append((node.lineno, "".join(parts)))
    return found


@pytest.mark.parametrize("rel", USER_KEYBOARDS)
def test_back_arrow_is_single_chevron(rel: str) -> None:
    """Возврат помечается «‹», а не «««: одна стрелка спокойнее и не спорит с
    системной стрелкой «назад» в клиенте Telegram."""
    bad = [f"{rel}:{line} → {text}" for line, text in _button_texts(ROOT / rel)
           if text.startswith("«")]
    assert not bad, "старая двойная стрелка возврата: " + "; ".join(bad)


@pytest.mark.parametrize("rel", USER_KEYBOARDS)
def test_no_possessive_section_names(rel: str) -> None:
    """Раздел называется существительным: «Подписка», а не «Моя подписка».

    Кнопка и заголовок экрана обязаны говорить одно и то же слово — человек
    ищет глазами ровно то, на что нажал.
    """
    bad = [f"{rel}:{line} → {text}" for line, text in _button_texts(ROOT / rel)
           if "Мои " in text or "Моя " in text or "Моё " in text]
    assert not bad, "притяжательное в названии раздела: " + "; ".join(bad)


@pytest.mark.parametrize("rel", USER_KEYBOARDS)
def test_button_labels_stay_short(rel: str) -> None:
    """Длинная подпись обрезается на телефоне многоточием — и человек не видит
    конца фразы. 30 символов помещаются на любой ширине."""
    bad = [
        f"{rel}:{line} → {text} ({len(text)})"
        for line, text in _button_texts(ROOT / rel)
        if len(text) > 30
    ]
    assert not bad, "слишком длинные подписи кнопок: " + "; ".join(bad)
