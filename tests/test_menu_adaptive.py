"""Адаптивное главное меню (Блок «Облик»).

До 20.08.2026 меню было одно на всех и содержало до одиннадцати кнопок, из
которых две полноширинные занимали юридические ссылки — их открывают один раз
в жизни. Теперь набор один, но из него выпадают неуместные кнопки, а редкое
уезжает в «⚙️ Ещё».
"""
from __future__ import annotations

import pytest

from bot.keyboards.inline import MenuState, main_menu, more_menu


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _primary(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row if b.style == "primary"]


NEWBIE = MenuState(sub_active=True, has_devices=False, is_trial=True)
ACTIVE = MenuState(sub_active=True, has_devices=True, is_trial=False)
EXPIRED = MenuState(sub_active=False, has_devices=True, is_trial=False)


class TestSize:
    @pytest.mark.parametrize("state", [NEWBIE, ACTIVE, EXPIRED])
    def test_never_more_than_six_buttons(self, state: MenuState) -> None:
        """Шесть — потолок для самого частого экрана бота."""
        assert len(_texts(main_menu(is_admin=False, state=state))) <= 6

    @pytest.mark.parametrize("state", [NEWBIE, ACTIVE, EXPIRED])
    def test_legal_links_are_not_on_the_main_screen(
        self, state: MenuState, monkeypatch
    ) -> None:
        from bot.config import settings

        monkeypatch.setattr(settings, "legal_privacy_url", "https://example.com/p")
        monkeypatch.setattr(settings, "legal_terms_url", "https://example.com/t")
        joined = " ".join(_texts(main_menu(is_admin=False, state=state)))
        assert "онфиденциальност" not in joined
        assert "оглашени" not in joined

    @pytest.mark.parametrize("state", [NEWBIE, ACTIVE, EXPIRED])
    def test_exactly_one_primary_action(self, state: MenuState) -> None:
        """Ровно одно главное действие: два «синих» — это уже не иерархия."""
        assert len(_primary(main_menu(is_admin=False, state=state))) == 1

    @pytest.mark.parametrize("state", [NEWBIE, ACTIVE, EXPIRED])
    def test_support_and_more_always_available(self, state: MenuState) -> None:
        joined = " ".join(_texts(main_menu(is_admin=False, state=state)))
        assert "Поддержка" in joined
        assert "Ещё" in joined


class TestByState:
    def test_newbie_is_pushed_to_connect(self) -> None:
        """Новичку незачем «Мои устройства» — у него их нет. Ему нужен один
        понятный первый шаг и витрина."""
        texts = _texts(main_menu(is_admin=False, state=NEWBIE))
        assert _primary(main_menu(is_admin=False, state=NEWBIE)) == ["🚀 Подключить устройство"]
        assert any("Тарифы" in t for t in texts)

    def test_active_user_gets_devices_first(self) -> None:
        assert _primary(main_menu(is_admin=False, state=ACTIVE)) == ["📱 Устройства"]

    def test_active_user_has_no_showcase(self) -> None:
        """Тарифы и локации — витрина. Тому, кто уже платит, они место не занимают."""
        texts = " ".join(_texts(main_menu(is_admin=False, state=ACTIVE)))
        assert "Тарифы" not in texts
        assert "Локации" not in texts

    def test_expired_user_is_pushed_to_renew(self) -> None:
        assert _primary(main_menu(is_admin=False, state=EXPIRED)) == ["🔁 Продлить подписку"]

    def test_admin_panel_only_for_admin(self) -> None:
        assert not any("Админ" in t for t in _texts(main_menu(is_admin=False, state=ACTIVE)))
        assert any("Админ" in t for t in _texts(main_menu(is_admin=True, state=ACTIVE)))


class TestMore:
    def test_more_holds_what_left_the_main_screen(self, monkeypatch) -> None:
        from bot.config import settings

        monkeypatch.setattr(settings, "legal_privacy_url", "https://example.com/p")
        monkeypatch.setattr(settings, "legal_terms_url", "https://example.com/t")
        joined = " ".join(_texts(more_menu()))
        for expected in ("Оповещения", "Локации", "Тарифы", "онфиденциальност", "оглашени"):
            assert expected in joined, f"«{expected}» потерялось при переезде в «Ещё»"

    def test_more_has_a_way_back(self, monkeypatch) -> None:
        from bot.config import settings

        monkeypatch.setattr(settings, "legal_privacy_url", "")
        monkeypatch.setattr(settings, "legal_terms_url", "")
        assert any("Меню" in t for t in _texts(more_menu()))

    def test_more_hides_unset_legal_links(self, monkeypatch) -> None:
        from bot.config import settings

        monkeypatch.setattr(settings, "legal_privacy_url", "")
        monkeypatch.setattr(settings, "legal_terms_url", "")
        joined = " ".join(_texts(more_menu()))
        assert "онфиденциальност" not in joined
