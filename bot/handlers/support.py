"""Блок «Сапорт-чат»: поддержка внутри ЭТОГО же бота, без отдельного сапорт-бота.

Как это работает:
  • Юзер: «🆘 Поддержка» → «✍️ Написать в поддержку» → режим диалога (FSM).
    Каждое его сообщение (текст/фото/видео/файл) копируется всем админам.
  • Админ: отвечает обычным РЕПЛАЕМ на пришедшую копию — бот доставляет ответ
    юзеру (реплаем на его исходный вопрос).
  • Юзер может продолжить, просто реплаем на ответ поддержки — режим диалога
    для этого включать не нужно (маршрут хранится в БД и переживает рестарт).

Маршрутизация — таблица support_msgs: пары (сообщение у юзера ↔ сообщение у
админа) в обе стороны. FSM тут только удобство первого сообщения; вся
долгоживущая логика — реплаи по БД.

ВАЖНО: роутер регистрируется ПОСЛЕДНИМ (см. handlers/__init__.py) — реплай-
хендлер без state-фильтра не должен перехватывать сообщения FSM-сценариев.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyParameters
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.keyboards.inline import CB_SUPPORT, support_dialog_kb
from bot.loader import bot as tg_bot
from bot.states.install import SupportStates
from bot.texts import t

router = Router(name="support")

# Заголовок + текст должны влезать в лимит Telegram (4096); длиннее — шлём копией.
_INLINE_TEXT_LIMIT = 3800


def _user_header(message: Message) -> str:
    u = message.from_user
    uname = f" (@{u.username})" if u.username else ""
    name = html.escape(u.full_name or "Без имени")
    return f"💬 <b>{name}</b>{uname} · id <code>{u.id}</code>"


async def _deliver_to_admins(message: Message, session: AsyncSession) -> bool:
    """Копирует сообщение юзера всем админам и пишет маршруты для реплаев.

    Текст шлём одним сообщением (заголовок + текст) — реплай на него и есть
    ответ. Медиа — заголовок + copy_message; маршрут пишем на ОБА сообщения,
    чтобы реплай хоть на заголовок, хоть на копию нашёл юзера.
    Возвращает True, если доставлено хотя бы одному админу.
    """
    user = await repo.get_or_create_user(
        session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    header = _user_header(message)
    as_text = message.text is not None and len(message.text) <= _INLINE_TEXT_LIMIT

    delivered = 0
    for admin_id in settings.admin_ids:
        try:
            admin_msg_ids: list[int] = []
            if as_text:
                sent = await tg_bot.send_message(
                    admin_id, f"{header}\n\n{html.escape(message.text)}"
                )
                admin_msg_ids.append(sent.message_id)
            else:
                head = await tg_bot.send_message(admin_id, header)
                copy = await tg_bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                admin_msg_ids += [head.message_id, copy.message_id]
            for mid in admin_msg_ids:
                await repo.add_support_route(
                    session,
                    user_id=user.id,
                    user_tg_id=message.from_user.id,
                    user_msg_id=message.message_id,
                    admin_tg_id=admin_id,
                    admin_msg_id=mid,
                )
            delivered += 1
        except Exception as exc:
            # Админ мог ни разу не открыть чат с ботом — Telegram не даст писать.
            logger.warning("Support: delivery to admin {} failed: {}", admin_id, exc)
    return delivered > 0


# --- Юзер: вход в диалог ------------------------------------------------------

@router.callback_query(F.data == f"{CB_SUPPORT}:start")
async def cb_support_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportStates.dialog)
    await call.message.edit_text(t.support_intro, reply_markup=support_dialog_kb())
    await call.answer()


@router.message(SupportStates.dialog)
async def step_support_message(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    if not await _deliver_to_admins(message, session):
        await message.answer(t.support_failed, reply_markup=support_dialog_kb())
        return
    # Остаёмся в диалоге: можно дописать детали следующим сообщением.
    await message.answer(t.support_sent, reply_markup=support_dialog_kb())


# --- Реплаи вне режима диалога ------------------------------------------------

async def _admin_reply_to_user(message: Message, session: AsyncSession) -> None:
    """Ответ админа реплаем на копию вопроса → доставить юзеру."""
    route = await repo.find_support_route_by_admin_msg(
        session, message.from_user.id, message.reply_to_message.message_id
    )
    if route is None:
        await message.reply(t.support_route_lost)
        return
    reply_params = ReplyParameters(
        message_id=route.user_msg_id, allow_sending_without_reply=True
    )
    try:
        if message.text is not None and len(message.text) <= _INLINE_TEXT_LIMIT:
            sent = await tg_bot.send_message(
                route.user_tg_id,
                f"{t.support_answer_header}\n\n{html.escape(message.text)}",
                reply_parameters=reply_params,
            )
        else:
            sent = await tg_bot.copy_message(
                chat_id=route.user_tg_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_parameters=reply_params,
            )
    except Exception as exc:
        logger.warning("Support: answer to user {} failed: {}", route.user_tg_id, exc)
        await message.reply(t.support_answer_failed)
        return
    # Обратный маршрут: юзер реплаит на наш ответ → диалог продолжается.
    await repo.add_support_route(
        session,
        user_id=route.user_id,
        user_tg_id=route.user_tg_id,
        user_msg_id=sent.message_id,
        admin_tg_id=message.from_user.id,
        admin_msg_id=message.message_id,
    )
    await message.reply(t.support_answer_delivered)


@router.message(F.reply_to_message)
async def msg_reply_router(message: Message, session: AsyncSession) -> None:
    """Реплаи, не забранные FSM-сценариями (роутер стоит последним):
    ответ админа юзеру или продолжение переписки от юзера. Чужие реплаи
    (не на сообщения бота / не по нашим маршрутам) молча игнорируем."""
    reply_to = message.reply_to_message
    if reply_to.from_user is None or reply_to.from_user.id != tg_bot.id:
        return

    if message.from_user.id in settings.admin_ids:
        await _admin_reply_to_user(message, session)
        return

    # Реплай юзера на ответ поддержки → переслать админам как продолжение.
    if await repo.is_support_reply_from_user(
        session, message.from_user.id, reply_to.message_id
    ):
        if await _deliver_to_admins(message, session):
            await message.answer(t.support_sent_short)
        else:
            await message.answer(t.support_failed)
