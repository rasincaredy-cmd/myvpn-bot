"""Клавиатуры юридических экранов: согласие с условиями."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.menu import back_target
from bot.keyboards.inline.prefixes import CB_BAL, CB_LEGAL


def consent_kb() -> InlineKeyboardMarkup:
    """Документы ссылками + «Согласен»/«Не согласен». Ссылки показываем, только
    если адреса заданы: кнопка с пустым url ломает отправку сообщения."""
    from bot.config import settings

    kb = InlineKeyboardBuilder()
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
    kb.button(text="Согласен", callback_data=f"{CB_LEGAL}:accept", style="success")
    kb.button(text="Не согласен", callback_data=f"{CB_LEGAL}:decline", style="danger")
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def tariffs_kb(origin: str | None = None) -> InlineKeyboardMarkup:
    """Витрина цен с выходом к покупке.

    До 22.08.2026 здесь была одна кнопка «назад»: человек дочитывал цены и
    оставался ни с чем — где платят, он должен был догадаться сам.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🎫 Выбрать тариф", callback_data=f"{CB_BAL}:shop",
              style="success")
    text, data = back_target(origin)
    kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()
