from __future__ import annotations

from aiogram import Dispatcher

from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.throttle import ThrottleMiddleware


def setup_middlewares(dp: Dispatcher) -> None:
    # Порядок важен: сначала throttle (дешёвый отсев), потом DB.
    throttle = ThrottleMiddleware()
    db = DbSessionMiddleware()

    for observer in (dp.message, dp.callback_query):
        observer.middleware(throttle)
        observer.middleware(db)
