from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings

bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

# MemoryStorage специально — SSH-пароли могут осесть в state.
# При рестарте бота state теряется (пользователь пройдёт сценарий заново),
# но это безопаснее, чем хранить креды в Redis/файле.
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
