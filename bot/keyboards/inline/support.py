"""Сапорт-чат (Блок «Сапорт-чат»)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_MENU, CB_SUPPORT


def support_intro_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Написать в поддержку", callback_data=f"{CB_SUPPORT}:start")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def support_dialog_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Завершить диалог", callback_data=f"{CB_MENU}:open")
    return kb.as_markup()
