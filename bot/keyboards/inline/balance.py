"""Баланс, пополнение, инвойсы и конструктор тарифа (Блок «Баланс»)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_BAL, CB_MENU, CB_NOP, CB_SUB


def balance_kb(can_deposit: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_deposit:
        kb.button(text="➕ Пополнить", callback_data=f"{CB_BAL}:dep", style="success")
    kb.button(text="📜 История", callback_data=f"{CB_BAL}:hist")
    kb.button(text="👥 Реферальная программа", callback_data=f"{CB_BAL}:ref")
    kb.button(text="« В меню", callback_data=f"{CB_MENU}:open")
    kb.adjust(1)
    return kb.as_markup()


def topup_kb() -> InlineKeyboardMarkup:
    """Одна кнопка пополнения — для уведомлений, где деньги закончились не
    вовремя (например, автопродление срезало срок подписки): юзеру не нужно
    искать раздел «Баланс» в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Пополнить баланс", callback_data=f"{CB_BAL}:dep",
              style="success")
    return kb.as_markup()


def deposit_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """amounts: (рубли, подпись кнопки) — подписи считаются из прайсинга
    («90 ₽ — месяц»), чтобы суммы не выглядели случайными числами."""
    kb = InlineKeyboardBuilder()
    for rub, label in amounts:
        kb.button(text=label, callback_data=f"{CB_BAL}:dep:{rub}")
    kb.button(text="✏️ Своя сумма", callback_data=f"{CB_BAL}:dep:custom")
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def invoice_kb(pay_url: str, row_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить в @CryptoBot", url=pay_url, style="success")
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"{CB_BAL}:check:{row_id}")
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()


def extend_kb(
    devices: int, bypass: int, term_prices: list[tuple[int, str]],
    max_devices: int, max_bypass: int,
) -> InlineKeyboardMarkup:
    """Экран продления: тариф крутится ±, сроки с ценами. Всё состояние — в
    callback data (без FSM): ext:<dev>:<byp> перерисовка, buy:<dev>:<byp>:<mes>.

    Подписи средних кнопок — только эмодзи+число («📱 2»): в ряду из трёх кнопок
    длинный текст обрезается на телефоне и числа не видно; расшифровка типов —
    в тексте сообщения. На границах (0, максимум, «последняя позиция») «−»/«+»
    рисуем заглушкой CB_NOP — не гоняем пустые перерисовки."""
    kb = InlineKeyboardBuilder()

    def _step(cur_d: int, cur_b: int, ok: bool) -> str:
        return f"{CB_BAL}:ext:{cur_d}:{cur_b}" if ok else CB_NOP

    # «−» недоступен на нуле и когда это последняя позиция тарифа (0+0 нельзя).
    kb.button(text="−", callback_data=_step(devices - 1, bypass, devices > 0 and devices + bypass > 1))
    kb.button(text=f"📱 {devices}", callback_data=CB_NOP)
    kb.button(text="+", callback_data=_step(devices + 1, bypass, devices < max_devices))
    kb.button(text="−", callback_data=_step(devices, bypass - 1, bypass > 0 and devices + bypass > 1))
    kb.button(text=f"⚡ {bypass}", callback_data=CB_NOP)
    kb.button(text="+", callback_data=_step(devices, bypass + 1, bypass < max_bypass))
    for months, label in term_prices:
        kb.button(text=label, callback_data=f"{CB_BAL}:buy:{devices}:{bypass}:{months}",
                  style="success")
    # Выход на пополнение прямо отсюда: юзеру с пустым балансом не нужно
    # догадываться, что пополнение живёт в разделе «Баланс».
    kb.button(text="➕ Пополнить баланс", callback_data=f"{CB_BAL}:dep")
    kb.button(text="« К подписке", callback_data=f"{CB_SUB}:my")
    kb.adjust(3, 3, 2, 2, 1, 1)
    return kb.as_markup()
