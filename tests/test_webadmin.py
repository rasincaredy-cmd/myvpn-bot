"""Тесты авторизации и API веб-панели администратора."""
from __future__ import annotations

import pytest
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.webadmin import auth, views_api


def test_magic_login_token() -> None:
    admin_id = settings.admin_ids[0] if settings.admin_ids else 111
    if admin_id not in settings.admin_ids:
        settings.admin_ids.append(admin_id)

    token = auth.create_magic_login_token(admin_id)
    assert token is not None
    verified = auth.verify_magic_login_token(token)
    assert verified == admin_id

    # Поддельный токен не проходит
    fake_token = token[:-5] + "aaaaa"
    assert auth.verify_magic_login_token(fake_token) is None


def test_session_creation() -> None:
    admin_id = 111
    sess_token = auth.create_session(admin_id)
    assert sess_token in auth._ACTIVE_SESSIONS
    assert auth._ACTIVE_SESSIONS[sess_token].admin_id == admin_id


@pytest.mark.asyncio
async def test_api_overview(session: AsyncSession) -> None:
    # Создаем тестового пользователя
    await repo.get_or_create_user(session, tg_id=888001, username="admin_tester", full_name="Tester")

    # Получаем данные сводки
    data = await views_api.get_overview_data(session)
    assert "users" in data
    assert "infrastructure" in data
    assert "finance" in data
    assert data["users"]["total"] >= 1

