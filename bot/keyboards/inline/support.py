"""Сапорт-чат (Блок «Сапорт-чат»)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_MENU, CB_SUPPORT


def support_intro_kb() -> InlineKeyboardMarkup:
    """Экран поддержки.

    Документы продублированы здесь намеренно: с главного меню они уехали в
    «⚙️ Ещё» (Блок «Облик»), а банк требует ПОСТОЯННОГО доступа к ним из бота.
    Поддержка — тот самый экран, куда идут с вопросами про оплату, продление и
    возвраты, поэтому ссылки должны быть под рукой именно здесь.
    """
    from bot.config import settings

    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Написать в поддержку", callback_data=f"{CB_SUPPORT}:start",
              style="primary")
    sizes = [1]
    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
        sizes.append(1)
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
        sizes.append(1)
    kb.button(text="‹ Меню", callback_data=f"{CB_MENU}:open")
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def support_dialog_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Завершить диалог", callback_data=f"{CB_MENU}:open")
    return kb.as_markup()
