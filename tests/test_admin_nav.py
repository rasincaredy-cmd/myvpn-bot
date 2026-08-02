"""Переходы из карточек юзера в серверные карточки объектов."""
from __future__ import annotations

from bot.keyboards.inline import admin_user_bypass_card_kb, admin_user_device_card_kb


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestJumpToServerCard:
    def test_device_card_links_to_peer(self) -> None:
        """Из устройства юзера админ должен попадать в карточку пира на
        сервере — там трафик, хендшейк и отзыв."""
        kb = admin_user_device_card_kb(
            device_id=7, user_id=3, page=0, configs=[(42, "🇳🇱 Нидерланды")]
        )

        assert "adm:peer:42" in _callbacks(kb)

    def test_bypass_card_links_to_bypass(self) -> None:
        kb = admin_user_bypass_card_kb(
            access_id=15, user_id=3, page=0, is_active=True, server_id=9
        )

        assert "srv:wopen:15" in _callbacks(kb)

    def test_revoked_bypass_still_links(self) -> None:
        """Отозванный обход тоже надо уметь открыть: разбор жалобы начинается
        как раз с того, что доступ уже погас."""
        kb = admin_user_bypass_card_kb(
            access_id=15, user_id=3, page=0, is_active=False, server_id=9
        )

        assert "srv:wopen:15" in _callbacks(kb)
