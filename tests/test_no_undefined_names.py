"""Страж: в коде бота нет имён, которых негде взять.

Повод — живой отказ 10.08.2026: в карточке инвайта админка падала
с `NameError: name 'fmt_msk' is not defined`. Имя использовалось в трёх
строках `bot/handlers/configs.py`, а импорта не было. Тесты этого не ловили:
ветка редкая, а импорт модуля проходит успешно — NameError выстреливает
только в момент выполнения строки.

Линтера в проекте нет, поэтому проверка написана на ast: собираем всё,
что в модуле хоть где-то связывается (импорты, присваивания, def/class,
аргументы, for/with/except, comprehension), и сверяем с тем, что читается.
Проверка нарочно грубая — «связано хоть где-то в файле» вместо настоящих
областей видимости. Она пропустит обращение до присваивания, но не даст
ложных срабатываний, а значит её не начнут отключать.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}


def _bound_names(tree: ast.AST) -> set[str]:
    """Имена, связанные где угодно в модуле: импорт, присваивание, def, аргумент."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.partition(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                    bound.add(a.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound


def _read_names(tree: ast.AST) -> dict[str, int]:
    """Имена, которые читаются, и номер первой строки чтения."""
    read: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read.setdefault(node.id, node.lineno)
    return read


MODULES = sorted(BOT_DIR.rglob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(BOT_DIR)))
def test_no_undefined_names(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    undefined = {
        name: line
        for name, line in _read_names(tree).items()
        if name not in _bound_names(tree) and name not in BUILTINS
    }
    assert not undefined, (
        f"{path.relative_to(BOT_DIR.parent)}: имена без источника — "
        + ", ".join(f"{n} (строка {ln})" for n, ln in sorted(undefined.items()))
    )
