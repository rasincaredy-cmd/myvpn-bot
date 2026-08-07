"""Согласие с условиями при первом входе (требование платёжного провайдера)."""
from __future__ import annotations

import pytest

from bot.db import repo


@pytest.mark.asyncio
async def test_new_user_has_no_consent(session) -> None:
    user = await repo.get_or_create_user(
        session, tg_id=9001, username="new", full_name="Новый"
    )
    assert user.terms_accepted_at is None


@pytest.mark.asyncio
async def test_accept_terms_writes_timestamp(session) -> None:
    user = await repo.get_or_create_user(
        session, tg_id=9002, username="new", full_name="Новый"
    )
    await repo.accept_terms(session, user)
    assert user.terms_accepted_at is not None


@pytest.mark.asyncio
async def test_accept_terms_is_idempotent(session) -> None:
    """Повторное нажатие «Согласен» не переписывает исходную дату — она может
    понадобиться при разборе спора об оплате."""
    user = await repo.get_or_create_user(
        session, tg_id=9003, username="new", full_name="Новый"
    )
    await repo.accept_terms(session, user)
    first = user.terms_accepted_at
    await repo.accept_terms(session, user)
    assert user.terms_accepted_at == first


def test_consent_keyboard_has_both_documents(monkeypatch) -> None:
    from bot.config import settings
    from bot.keyboards.inline import consent_kb

    monkeypatch.setattr(settings, "legal_privacy_url", "https://telegra.ph/p")
    monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/t")

    kb = consent_kb()
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    callbacks = [
        b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data
    ]
    assert "https://telegra.ph/p" in urls
    assert "https://telegra.ph/t" in urls
    assert any(c.endswith(":accept") for c in callbacks)
    assert any(c.endswith(":decline") for c in callbacks)
