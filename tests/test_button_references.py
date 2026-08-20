"""Названия кнопок в текстах обязаны совпадать с самими кнопками.

Влад поймал на живом боте 20.08.2026: карточка резервного подключения писала
«жми „📱 Подключаюсь с другого устройства“ ниже», а кнопка под ней называлась
«📱 Сменить устройство» — я укоротил подпись и не тронул текст, который на неё
ссылается. Человек ищет глазами названную кнопку, не находит и решает, что
инструкция не про этот экран.

Правило было записано в скил `bot-ui` с самого начала, но проверять его было
нечем — и оно сломалось в тот же день. Теперь проверяет тест.

Ищем в текстах ссылки вида «<эмодзи> Название» и требуем, чтобы такая кнопка
где-то в боте существовала. Смотрим ТОЛЬКО строковые литералы: комментарии и
докстринги — внутренняя кухня (тот же приём, что в test_wording.py).
"""
from __future__ import annotations

import ast
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Флаги (🇩🇪, 🇳🇱) — это названия ЛОКАЦИЙ, а не кнопок: такие кнопки строятся из
# базы на лету, литерала для них не существует и существовать не должно.
_REGIONAL_INDICATOR = range(0x1F1E6, 0x1F200)


def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) in ("So", "Sk")


def _button_labels() -> set[str]:
    """Все подписи кнопок бота.

    Учитываем три формы: литерал, f-строка и тернарник (`"ВКЛ" if x else "выкл"`
    — так сделаны все тумблеры). Пустые огрызки f-строк отбрасываем: пустая
    строка «содержится» в любом тексте и делает проверку бессмысленной — на
    этом первая версия теста показала ноль расхождений при трёх настоящих.
    """
    labels: set[str] = set()

    def collect(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            labels.add(node.value.strip())
        elif isinstance(node, ast.JoinedStr):
            labels.add("".join(
                p.value for p in node.values
                if isinstance(p, ast.Constant) and isinstance(p.value, str)
            ).strip())
        elif isinstance(node, ast.IfExp):
            collect(node.body)
            collect(node.orelse)

    paths = list((ROOT / "bot" / "keyboards").rglob("*.py"))
    paths += list((ROOT / "bot" / "handlers").rglob("*.py"))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "text":
                        collect(kw.value)
    return {b for b in labels if len(b) >= 3}


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _button_references() -> dict[str, set[str]]:
    """Ссылки на кнопки, найденные в текстах для людей: «<эмодзи> Название»."""
    refs: dict[str, set[str]] = {}
    paths = [ROOT / "bot" / "texts" / "ru.py"]
    paths += list((ROOT / "bot" / "handlers").rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in skip:
                continue
            if not isinstance(node.value, str):
                continue
            for m in re.finditer(r"«([^»]{2,40})»", node.value):
                q = m.group(1).replace("<b>", "").replace("</b>", "").strip()
                if not q or not _is_emoji(q[0]):
                    continue
                if ord(q[0]) in _REGIONAL_INDICATOR:
                    continue  # название локации, а не кнопка
                if "/" in q:
                    continue  # «♻️ Автопродление: ВКЛ/выкл» — описание двух состояний
                refs.setdefault(q, set()).add(
                    f"{path.relative_to(ROOT)}:{node.lineno}"
                )
    return refs


def test_every_named_button_exists() -> None:
    labels = _button_labels()
    missing = [
        f"«{q}» ({', '.join(sorted(where))})"
        for q, where in sorted(_button_references().items())
        if not any(q == b or q in b for b in labels)
    ]
    assert not missing, (
        "текст называет кнопку, которой нет — человек будет искать её глазами "
        "и не найдёт: " + "; ".join(missing)
    )


def test_scanner_sees_real_buttons() -> None:
    """Проверка самой проверки: сканер обязан находить обычные кнопки.

    Без этого теста молчание `test_every_named_button_exists` ничего не значит —
    ровно так первая версия «прошла» при трёх настоящих расхождениях.
    """
    labels = _button_labels()
    for expected in ("‹ Меню", "📱 Сменить устройство", "⚙️ Сменить тариф"):
        assert expected in labels, f"сканер не видит кнопку «{expected}»"


def test_scanner_sees_references() -> None:
    """И ссылки в текстах он тоже обязан находить."""
    assert len(_button_references()) >= 10
