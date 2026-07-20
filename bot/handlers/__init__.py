from __future__ import annotations

from aiogram import Dispatcher

from bot.handlers import (
    admin_panel,
    balance,
    common,
    configs,
    devices,
    install,
    menu,
    support,
    wdtt,
)


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(common.router)
    dp.include_router(menu.router)
    dp.include_router(configs.router)
    dp.include_router(devices.router)
    dp.include_router(install.router)
    dp.include_router(admin_panel.router)
    dp.include_router(wdtt.router)
    dp.include_router(balance.router)
    # Сапорт-чат — СТРОГО последним: его реплай-хендлер без state-фильтра
    # ловит только сообщения, не забранные FSM-сценариями выше.
    dp.include_router(support.router)
