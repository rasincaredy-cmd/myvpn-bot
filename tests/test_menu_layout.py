"""Раскладка главного меню и строка статуса подписки."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def test_main_menu_pairs_secondary_buttons(monkeypatch) -> None:
    """Главные действия — во всю ширину, второстепенные — парами: иначе меню
    выглядит как восемь одинаковых кнопок без иерархии."""
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")

    rows = main_menu(is_admin=False).inline_keyboard
    assert len(rows[0]) == 1
    assert len(rows[1]) == 1
    assert any(len(row) == 2 for row in rows)


def test_main_menu_marks_primary_action(monkeypatch) -> None:
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")

    styles = [b.style for row in main_menu(is_admin=False).inline_keyboard for b in row]
    assert "primary" in styles


def test_sub_status_line_active() -> None:
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    user = User(
        tg_id=1, sub_expires_at=datetime.now(timezone.utc) + timedelta(days=5)
    )
    line = build_sub_status_line(user)
    assert "5" in line


def test_sub_status_line_expired() -> None:
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    user = User(
        tg_id=1, sub_expires_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert "не активна" in build_sub_status_line(user).lower()
