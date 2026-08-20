"""Чужой текст в HTML-разметке сообщений.

Найдено 20.08.2026: имя из профиля Telegram подставлялось в приветствие как
есть. Имя с угловой скобкой делает сообщение непарсимым, Telegram отвечает
«can't parse entities» — и человек не может запустить бота ВООБЩЕ, ни разу.
Тихий баг: в логи он попадает как ошибка отправки, а не как жалоба.
"""
from __future__ import annotations

import pytest

from bot.keyboards.inline import MenuState

EVIL = "<b>Вася</b> & <script>"
_STATE = MenuState(sub_active=True, has_devices=False, is_trial=True)


class _StubUser:
    def __init__(self, name: str) -> None:
        self.full_name = name
        self.id = 777
        self.username = "vasya"


class _StubMessage:
    """Ровно то, что трогает _send_main_menu: from_user и answer()."""

    def __init__(self, name: str) -> None:
        self.from_user = _StubUser(name)
        self.sent: list[str] = []

    async def answer(self, text: str, **kw) -> None:
        self.sent.append(text)


@pytest.mark.asyncio
async def test_start_screen_escapes_user_name() -> None:
    from bot.handlers.common import _send_main_menu

    msg = _StubMessage(EVIL)
    await _send_main_menu(msg, False, _STATE)
    assert msg.sent, "приветствие не отправлено"
    assert "<script>" not in msg.sent[0]
    assert "&lt;script&gt;" in msg.sent[0]


@pytest.mark.asyncio
async def test_admin_start_screen_escapes_user_name() -> None:
    from bot.handlers.common import _send_main_menu

    msg = _StubMessage(EVIL)
    await _send_main_menu(msg, True, _STATE)
    assert "<script>" not in msg.sent[0]


def test_no_raw_full_name_in_user_facing_formats() -> None:
    """Сторож: `full_name` не должен попадать в текст сообщения без `safe()`.

    Смотрим исходники хендлеров: подстановка имени в шаблон — это всегда
    `format(name=...)`, и в нём обязан стоять `ui.safe`.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted((root / "bot" / "handlers").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "name":
                    continue
                src = ast.unparse(kw.value)
                if "full_name" in src and "safe" not in src:
                    offenders.append(f"{path.name}:{node.lineno} → {src}")
    assert not offenders, "имя подставлено в разметку без экранирования: " + "; ".join(offenders)
