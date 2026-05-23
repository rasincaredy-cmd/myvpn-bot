from __future__ import annotations

import asyncio
import sys

from loguru import logger

from bot.config import settings
from bot.db.base import init_db
from bot.handlers import register_handlers
from bot.loader import bot, dp
from bot.middlewares import setup_middlewares
from bot.utils.menu_commands import set_bot_commands


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
        ),
    )


async def _on_startup() -> None:
    await init_db()
    setup_middlewares(dp)
    register_handlers(dp)
    await set_bot_commands(bot)
    me = await bot.get_me()
    logger.info("Bot started: @{} ({})", me.username, me.id)


async def main() -> None:
    _setup_logging()
    await _on_startup()
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
