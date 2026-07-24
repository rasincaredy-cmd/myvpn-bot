from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class InstallStates(StatesGroup):
    name = State()
    location = State()
    host = State()
    ssh_port = State()
    ssh_user = State()
    auth_method = State()
    password = State()
    key = State()
    key_passphrase = State()
    wg_port = State()
    confirm = State()


class InviteStates(StatesGroup):
    pick_server = State()
    label = State()


class BroadcastStates(StatesGroup):
    target  = State()
    select  = State()   # ручной выбор получателей (чекбоксы)
    message = State()
    confirm = State()


class PeerRenameStates(StatesGroup):
    label = State()


class ServerEditStates(StatesGroup):
    location = State()
    dns = State()
    wdtt_limit = State()  # ёмкость обхода: Server.wdtt_max_accesses
    name = State()        # переименование сервера (Блок «Ревизия»)


class WdttStates(StatesGroup):
    platform = State()
    pick_server = State()
    pick_device = State()
    vk = State()        # выбор: ссылка сервиса или своя
    vk_link = State()   # ввод своей VK-ссылки


class DeviceStates(StatesGroup):
    label = State()
    rename = State()  # юзер переименовывает своё устройство (Блок «Ревизия»)


class SubAdminStates(StatesGroup):
    set_limit = State()
    extend_days = State()
    set_traffic = State()
    set_bypass = State()
    set_balance = State()  # ручное ± к балансу (Блок «Баланс»)


class BalanceStates(StatesGroup):
    custom_amount = State()  # своя сумма пополнения


class SupportStates(StatesGroup):
    dialog = State()  # юзер пишет в поддержку; каждое сообщение уходит админам
