"""Страж формулировок: платёжный провайдер требует, чтобы в текстах для
пользователя не было упоминаний обхода блокировок, DPI, ТСПУ, белых списков,
LTE и ИНН (требование законодательства РФ для VPN-проектов).

Разбираем файлы через ast и смотрим ТОЛЬКО строковые литералы: комментарии и
докстринги — внутренняя кухня, их чистить не нужно и незачем.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Файлы, которые видит пользователь.
SCAN_GLOBS = [
    "bot/texts/*.py",
    "bot/keyboards/inline/*.py",
    "bot/handlers/*.py",
    "bot/services/*.py",
    "bot/utils/menu_commands.py",
    "bot/middlewares/*.py",
]

# Админские экраны: их видит только Влад, формулировки там остаются.
EXCLUDE = {
    "bot/keyboards/inline/admin.py",
    "bot/keyboards/inline/servers.py",
    "bot/keyboards/inline/install.py",
    "bot/handlers/install.py",
}

FORBIDDEN = re.compile(
    r"обход|блокиров|белы[йех]\s+спис|\bDPI\b|ТСПУ|глушилк|\bLTE\b|\bИНН\b",
    re.IGNORECASE,
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() узлов-докстрингов — их из проверки исключаем."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _has_suppression_marker(source: str, node: ast.Constant) -> bool:
    """Проверяет, есть ли маркер `# wording: ok` в диапазоне узла."""
    if node.end_lineno is None:
        return False
    lines = source.split('\n')
    for line_idx in range(node.lineno - 1, min(node.end_lineno, len(lines))):
        if '# wording: ok' in lines[line_idx]:
            return True
    return False


def _files_by_glob() -> dict[str, list[Path]]:
    """Возвращает найденные файлы, сгруппированные по глобам."""
    result = {}
    for pattern in SCAN_GLOBS:
        files = []
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            if rel in EXCLUDE or path.name == "__init__.py":
                continue
            files.append(path)
        result[pattern] = files
    return result


def _user_facing_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCAN_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            if rel in EXCLUDE or path.name == "__init__.py":
                continue
            files.append(path)
    return files


@pytest.mark.parametrize(
    "path", _user_facing_files(), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_no_forbidden_wording(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)

    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        if _has_suppression_marker(source, node):
            continue
        match = FORBIDDEN.search(node.value)
        if match:
            # Показываем окно вокруг стоп-слова для лучшего контекста
            start = max(0, match.start() - 20)
            end = min(len(node.value), match.end() + 20)
            context = node.value[start:end]
            hits.append(f"строка {node.lineno}: «{match.group(0)}» в ...{context!r}...")

    assert not hits, "Запрещённые формулировки:\n" + "\n".join(hits)


def test_scanner_sees_files() -> None:
    """Защита от опечатки в глобах: каждый глоб должен находить файлы."""
    files_by_glob = _files_by_glob()
    empty_globs = [glob for glob, files in files_by_glob.items() if not files]

    assert not empty_globs, (
        f"Глобы не находят файлы: {empty_globs}. "
        f"Это может означать опечатку в пути или переименование папки."
    )


def test_suppression_marker_works() -> None:
    """Проверяем, что маркер `# wording: ok` подавляет ошибки."""
    # Создаём временный файл с запрещённым словом и маркером подавления
    test_code = '''
message = "Обход БС"  # wording: ok
'''
    tree = ast.parse(test_code)
    skip = _docstring_nodes(tree)

    # Проверяем, что маркер работает
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        if FORBIDDEN.search(node.value):
            # Если найдено стоп-слово, проверяем маркер
            assert _has_suppression_marker(test_code, node), (
                "Маркер подавления не сработал для строки с запрещённым текстом"
            )
