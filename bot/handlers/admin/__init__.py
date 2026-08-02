"""Админ-панель: /admin, статистика, юзеры, подписки, рассылка, бэкап.

Раньше это был один файл на 1150 строк. Разложен по экранам — каждый модуль
держит свой Router, а здесь они собираются под общий родительский роутер.
AdminFilter висит именно на родителе: в aiogram фильтры роутера проверяются
до вложенных, поэтому одной проверки хватает на все экраны — и невозможно
завести новый модуль, забыв её повесить.
"""
from __future__ import annotations

from aiogram import Router

from bot.filters.admin import AdminFilter
from bot.handlers.admin import (
    audit,
    broadcast,
    entry,
    stats,
    subscription,
    user_items,
    users,
)
from bot.handlers.admin.entry import cmd_admin

router = Router(name="admin_panel")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

# Порядок между модулями не важен: все префиксы колбэков заканчиваются на ":"
# ("panel:udev:" не поймает "panel:udevo:"), пересечений нет.
router.include_router(entry.router)
router.include_router(stats.router)
router.include_router(audit.router)
router.include_router(users.router)
router.include_router(user_items.router)
router.include_router(subscription.router)
router.include_router(broadcast.router)

__all__ = ["cmd_admin", "router"]
