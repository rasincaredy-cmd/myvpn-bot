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


MARKER = '# wording: ok'

# Символы, которые допустимы на строке с маркером перед ним: закрывающие
# скобки и запятая многострочной склейки.
_CLOSERS = ')]},'


def _has_suppression_marker(source: str, node: ast.Constant) -> bool:
    """Проверяет маркер `# wording: ok` рядом с концом литерала.

    Маркер действует точечно, годятся два места:
      • хвост строки ПОСЛЕ закрывающей кавычки литерала;
      • следующая строка, если в ней из кода только закрывающие скобки или
        запятая — так маркер можно поставить на `)` многострочной склейки,
        где его и ищешь глазами.

    Текст `# wording: ok` внутри самого литерала подавлением не считается:
    смотрим только то, что идёт после конца литерала.

    Ограничение: два литерала на одной строке подавляются одним маркером.
    """
    if node.end_lineno is None or node.end_col_offset is None:
        return False
    lines = source.split('\n')
    if node.end_lineno > len(lines):
        return False

    # col_offset у ast — смещение в БАЙТАХ utf-8, а не в символах. Тексты у нас
    # русские, поэтому режем байты, иначе хвост уезжает.
    raw = lines[node.end_lineno - 1].encode('utf-8')
    tail = raw[node.end_col_offset:].decode('utf-8', 'ignore')
    if MARKER in tail:
        return True

    if node.end_lineno < len(lines):
        nxt = lines[node.end_lineno].strip()
        if MARKER in nxt:
            head = nxt.split('#', 1)[0].strip()
            if head and all(ch in _CLOSERS for ch in head):
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


def _scan(source: str) -> list[str]:
    """Ищет стоп-слова в строковых литералах исходника.

    ЕДИНСТВЕННОЕ место, где живёт логика стража: и проверка реальных файлов, и
    тест-канарейка зовут ровно эту функцию. Если её обезвредить — канарейка
    покраснеет, то есть страж не может тихо перестать работать.
    """
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
    return hits


@pytest.mark.parametrize(
    "path", _user_facing_files(), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_no_forbidden_wording(path: Path) -> None:
    hits = _scan(path.read_text(encoding="utf-8"))
    assert not hits, "Запрещённые формулировки:\n" + "\n".join(hits)


def test_scanner_sees_files() -> None:
    """Защита от опечатки в глобах: каждый глоб должен находить файлы."""
    files_by_glob = _files_by_glob()
    empty_globs = [glob for glob, files in files_by_glob.items() if not files]

    assert not empty_globs, (
        f"Глобы не находят файлы: {empty_globs}. "
        f"Это может означать опечатку в пути или переименование папки."
    )


def _single_str_node(code: str) -> ast.Constant:
    """Единственный строковый литерал в коде.

    Через неё берём узел явно: цикл с `if node.value == ...` прошёл бы молча,
    если бы литерал перестал совпадать, и тест ничего бы не проверил.
    """
    tree = ast.parse(code)
    nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert len(nodes) == 1, f"ожидался один литерал, найдено {len(nodes)}"
    return nodes[0]


def test_suppression_marker_detects_marker() -> None:
    """Тест: маркер `# wording: ok` в хвосте строки литерала находится."""
    code = 'message = "Обход БС"  # wording: ok\n'
    assert _has_suppression_marker(code, _single_str_node(code)), (
        "Маркер должен быть найден на строке с литералом"
    )


def test_suppression_marker_missing_without_marker() -> None:
    """Тест: без маркера литерал не подавляется."""
    code = 'message = "Обход БС"\n'
    assert not _has_suppression_marker(code, _single_str_node(code)), (
        "Без маркера подавления быть не должно"
    )


def test_suppression_marker_on_multiline_string() -> None:
    """Тест: маркер на строке последнего фрагмента склейки подавляет её."""
    code = '''x = ("text "
     "text")  # wording: ok
'''
    assert _has_suppression_marker(code, _single_str_node(code)), (
        "Маркер на последней строке многострочного литерала должен подавлять"
    )


def test_suppression_marker_on_closing_paren() -> None:
    """Тест: маркер на закрывающей скобке подавляет литерал.

    Так выглядят длинные тексты в ru.py: скобка съезжает на свою строку, и
    комментарий ставят рядом с ней, а не на строке с текстом.
    """
    code = '''x = (
    "text "
    "Обход БС"
)  # wording: ok
'''
    assert _has_suppression_marker(code, _single_str_node(code)), (
        "Маркер на закрывающей скобке должен подавлять литерал"
    )


def test_marker_inside_literal_does_not_suppress() -> None:
    """Тест: `# wording: ok` внутри текста литерала подавлением не считается."""
    code = 'message = "Обход БС # wording: ok"\n'
    assert not _has_suppression_marker(code, _single_str_node(code)), (
        "Маркер внутри строки не должен подавлять: иначе стоп-слово уедет "
        "пользователю вместе с текстом комментария"
    )
    assert _scan(code), "и стоп-слово в таком литерале должно находиться"


def test_marker_of_next_literal_does_not_suppress_previous() -> None:
    """Тест: маркер у СЛЕДУЮЩЕГО литерала не гасит предыдущий."""
    code = 'a = "Обход"\nb = "Обход БС"  # wording: ok\n'
    tree = ast.parse(code)
    nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert len(nodes) == 2, f"ожидалось два литерала, найдено {len(nodes)}"
    first = min(nodes, key=lambda n: n.lineno)
    assert not _has_suppression_marker(code, first), (
        "Маркер на следующей строке с кодом относится к своему литералу"
    )
    assert _scan(code), "непокрытый литерал должен остаться находкой"


def test_marker_after_cyrillic_literal() -> None:
    """Тест: конец литерала считается в БАЙТАХ utf-8, а не в символах.

    Тексты у нас русские: если резать строку по символам, хвост уезжает за её
    конец и легальный маркер молча теряется.
    """
    code = 'message = "Очень длинный русский текст про обход"  # wording: ok\n'
    assert _has_suppression_marker(code, _single_str_node(code)), (
        "Маркер после кириллического литерала должен находиться"
    )
    assert not _scan(code), "с маркером находок быть не должно"


def test_wording_guard_finds_violations() -> None:
    """Тест: страж вообще способен обнаруживать стоп-слова без маркера.

    Вызывает ТУ ЖЕ _scan, что и test_no_forbidden_wording — если обезвредить
    _scan (ранний return, сломанная регулярка), канарейка покраснеет.
    """
    assert _scan("message = 'Обход БС'\n"), (
        "Страж не увидел стоп-слово. "
        "Если тест упал — сломан механизм _scan, а не продуктовый текст."
    )
