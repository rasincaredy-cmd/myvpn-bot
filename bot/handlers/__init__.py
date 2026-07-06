from __future__ import annotations

from aiogram import Dispatcher

from bot.handlers import admin_panel, common, configs, install, menu


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(common.router)
    dp.include_router(menu.router)
    dp.include_router(configs.router)
    dp.include_router(install.router)
    dp.include_router(admin_panel.router)
