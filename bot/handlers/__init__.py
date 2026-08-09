from __future__ import annotations

from aiogram import Dispatcher

from bot.handlers import (
    admin,
    balance,
    common,
    config_delivery,
    config_move,
    configs,
    devices,
    errors,
    install,
    legal,
    servers,
    stars,
    support,
    wdtt,
)


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(common.router)
    dp.include_router(servers.router)
    dp.include_router(configs.router)
    dp.include_router(config_delivery.router)
    dp.include_router(config_move.router)
    dp.include_router(devices.router)
    dp.include_router(install.router)
    dp.include_router(admin.router)
    dp.include_router(wdtt.router)
    dp.include_router(balance.router)
    dp.include_router(stars.router)
    dp.include_router(legal.router)
    # Сапорт-чат — СТРОГО последним: его реплай-хендлер без state-фильтра
    # ловит только сообщения, не забранные FSM-сценариями выше.
    dp.include_router(support.router)
    # Ловушка необработанных ошибок. Порядок среди роутеров ей не важен
    # (@router.errors() живёт отдельно от апдейтов), но держим её в конце,
    # чтобы читалось как «последний рубеж».
    dp.include_router(errors.router)
