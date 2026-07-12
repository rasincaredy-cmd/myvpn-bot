from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class InstallStates(StatesGroup):
    name = State()
    host = State()
    ssh_port = State()
    ssh_user = State()
    auth_method = State()
    password = State()
    key = State()
    key_passphrase = State()
    wg_port = State()
    confirm = State()


class PeerStates(StatesGroup):
    pick_server = State()
    label = State()


class InviteStates(StatesGroup):
    pick_server = State()
    label = State()


class BroadcastStates(StatesGroup):
    text    = State()
    confirm = State()


class PeerLimitStates(StatesGroup):
    set_expires = State()
    set_traffic = State()


class PeerRenameStates(StatesGroup):
    label = State()


class WdttStates(StatesGroup):
    label = State()
    days = State()
    platform = State()
    pick_server = State()
    pick_device = State()


class DeviceStates(StatesGroup):
    label = State()


class SubAdminStates(StatesGroup):
    set_limit = State()
    extend_days = State()
