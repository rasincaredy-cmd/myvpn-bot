"""Состав пробного периода.

Триал не должен быть щедрее платной базы: платить незачем, если бесплатно
дают больше. Лимит обходов раньше не задавался вовсе и брался из умолчания
модели (2) — при базе 1 устройство + 1 обход за 90 ₽ это и был перекос.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo


@pytest.mark.asyncio
async def test_new_user_gets_trial_limits_from_config(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session, tg_id=4242, username="new", full_name="Новый"
    )
    assert user.sub_max_devices == settings.trial_devices
    assert user.sub_max_bypass == settings.trial_bypass


@pytest.mark.asyncio
async def test_trial_is_not_richer_than_paid_base(session: AsyncSession) -> None:
    """Ловит перекос напрямую: база продаётся как 1+1, триал не может быть больше."""
    user = await repo.get_or_create_user(
        session, tg_id=4243, username="new", full_name="Новый"
    )
    assert user.sub_max_devices <= 1, "устройств на триале больше платной базы"
    assert user.sub_max_bypass <= 1, "обходов на триале больше платной базы"


@pytest.mark.asyncio
async def test_trial_bypass_follows_config(session: AsyncSession, monkeypatch) -> None:
    """Значение берётся из настройки, а не зашито числом."""
    monkeypatch.setattr(settings, "trial_bypass", 3)
    user = await repo.get_or_create_user(
        session, tg_id=4244, username="new", full_name="Новый"
    )
    assert user.sub_max_bypass == 3
