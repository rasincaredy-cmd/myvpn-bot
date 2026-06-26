from __future__ import annotations

from aiogram import Dispatcher

from bot.middlewares.block import BlockMiddleware
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.throttle import ThrottleMiddleware


def setup_middlewares(dp: Dispatcher) -> None:
    throttle = ThrottleMiddleware()
    db       = DbSessionMiddleware()
    block    = BlockMiddleware()

    for observer in (dp.message, dp.callback_query):
        observer.middleware(throttle)
        observer.middleware(db)
        observer.middleware(block)  # после db — нужна сессия
