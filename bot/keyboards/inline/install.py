"""Клавиатуры мастера установки AmneziaWG на VPS."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_CANCEL, CB_INSTALL


def install_auth_method() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗝 Пароль", callback_data=f"{CB_INSTALL}:auth:password")
    kb.button(text="🔑 SSH-ключ", callback_data=f"{CB_INSTALL}:auth:key")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(2, 1)
    return kb.as_markup()


def install_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=f"{CB_INSTALL}:run")
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    kb.adjust(2)
    return kb.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=CB_CANCEL)
    return kb.as_markup()
