"""Префиксы callback_data — единственное, что общее у всех клавиатур."""
from __future__ import annotations

# --- Callback prefixes --------------------------------------------------------
# Формат: "<ns>:<action>[:<arg>]"
CB_MENU = "menu"
CB_INSTALL = "install"
CB_SERVERS = "srv"
CB_INVITES = "inv"
CB_ADMIN = "adm"          # admin-панель: управление пирами любого юзера
CB_PANEL = "pnl"   # admin-панель
CB_WDTT = "wdtt"   # обход белых списков (wdtt / proxy-turn-vk)
CB_DEVICE = "dev"  # устройства (Блок 9)
CB_CFG = "cfg"     # выбор формата конфига (Этап B)
CB_SUB = "sub"     # подписка (Блок 9)
CB_NOP = "nop"
CB_CANCEL = "cancel"
CB_BAL = "bal"     # баланс/оплата/рефералка (Блок «Баланс»)
CB_SUPPORT = "sup" # сапорт-чат (Блок «Сапорт-чат»)
