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
    "bot/services/scheduler.py",
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
        match = FORBIDDEN.search(node.value)
        if match:
            hits.append(f"строка {node.lineno}: «{match.group(0)}» в {node.value[:70]!r}")

    assert not hits, "Запрещённые формулировки:\n" + "\n".join(hits)


def test_scanner_sees_files() -> None:
    """Защита от опечатки в глобах: если список пуст, страж молча зеленеет."""
    assert len(_user_facing_files()) >= 8
