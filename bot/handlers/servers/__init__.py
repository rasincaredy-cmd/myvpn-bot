"""Админское управление серверами: карточка, пиры, метрики, обходы БС.

Раньше это был bot/handlers/menu.py на 966 строк. Разложен по экранам;
здесь модули собираются под общий роутер.

AdminFilter повешен только на callback_query — как и было в menu.py.
У message-хендлеров фильтр стоит свой, персонально в декораторе; переносить
его на роутер не стали, чтобы не менять поведение при дроблении.
"""
from __future__ import annotations

from aiogram import Router

from bot.filters.admin import AdminFilter
from bot.handlers.servers import bypass, card, metrics, peers

router = Router(name="servers")
router.callback_query.filter(AdminFilter())

router.include_router(card.router)
router.include_router(peers.router)
router.include_router(metrics.router)
router.include_router(bypass.router)

__all__ = ["router"]
