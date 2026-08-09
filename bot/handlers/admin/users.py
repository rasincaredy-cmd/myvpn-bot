"""Список юзеров, карточка, «друг», блокировка и стирание из БД."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import AuditAction
from bot.handlers.admin.common import PER_PAGE, user_card_text
from bot.keyboards.inline import (
    CB_PANEL,
    back_to_users_kb,
    user_card_kb,
    users_list_kb,
)

router = Router(name="admin_users")


@router.callback_query(F.data.startswith(f"{CB_PANEL}:users:"))
async def cb_panel_users(call: CallbackQuery, session: AsyncSession) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    total = await repo.count_users(session)
    users = await repo.list_all_users(session, offset=page * PER_PAGE, limit=PER_PAGE)

    await call.message.edit_text(
        f"👤 <b>Пользователи</b>  (всего: {total})\n"
        f"💎 платная · 🎁 триал · 💤 без подписки · 🔴 бан\n"
        f"Страница {page + 1} из {max(1, -(-total // PER_PAGE))}",
        reply_markup=users_list_kb(
            users,
            page,
            has_prev=page > 0,
            has_next=(page + 1) * PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:user:"))
async def cb_panel_user_open(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])

    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.message.edit_text(
        await user_card_text(session, user),
        reply_markup=user_card_kb(
            user.id, user.is_blocked, page,
            is_vip=user.is_vip, is_staff=user.is_staff,
        ),
    )
    await call.answer()


# --- «Друг» (доступ к приватным серверам, Блок «Ревизия») ---------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:vip:"))
async def cb_panel_toggle_vip(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    user.is_vip = not user.is_vip
    await session.commit()
    await call.answer(
        "⭐ Теперь «друг»: видит и получает конфиги с приватных серверов."
        if user.is_vip else "Больше не «друг»: приватные серверы недоступны "
        "(уже выданные конфиги продолжают работать).",
        show_alert=True,
    )
    await call.message.edit_text(
        await user_card_text(session, user),
        reply_markup=user_card_kb(
            user.id, user.is_blocked, page,
            is_vip=user.is_vip, is_staff=user.is_staff,
        ),
    )


# --- «Служебный» (не считать в статистике, этап D) ---------------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:staff:"))
async def cb_panel_toggle_staff(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    user.is_staff = not user.is_staff
    await session.commit()
    # Для самого юзера не меняется ничего: он о пометке не знает и видит бота
    # ровно таким же. Это только про то, кого считать в статистике.
    await call.answer(
        "🧰 Служебный: покупки и пополнения этого аккаунта больше не попадают "
        "в конверсию и в деньги за 30 дней."
        if user.is_staff else
        "Обычный юзер: снова считается в статистике наравне со всеми.",
        show_alert=True,
    )
    await call.message.edit_text(
        await user_card_text(session, user),
        reply_markup=user_card_kb(
            user.id, user.is_blocked, page,
            is_vip=user.is_vip, is_staff=user.is_staff,
        ),
    )


# --- Блокировка --------------------------------------------------------------

@router.callback_query(
    F.data.startswith(f"{CB_PANEL}:block:") | F.data.startswith(f"{CB_PANEL}:unblock:")
)
async def cb_panel_toggle_block(call: CallbackQuery, session: AsyncSession) -> None:
    parts = call.data.split(":")
    action, user_id, page = parts[1], int(parts[2]), int(parts[3])
    block = action == "block"

    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Не найдено", show_alert=True)
        return
    # user.is_admin в БД синкается только при /start-подобных хендлерах — свежий
    # админ из .env мог ещё не написать боту, проверяем и по settings.
    if user.is_admin or user.tg_id in settings.admin_ids:
        await call.answer("Нельзя заблокировать другого админа.", show_alert=True)
        return

    await repo.set_user_blocked(session, user.id, block)
    await repo.log_action(
        session,
        AuditAction.USER_BLOCKED if block else AuditAction.USER_UNBLOCKED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=user.id,
        target_type="user",
        target_id=user.id,
        details="Заблокирован админом" if block else "Разблокирован админом",
    )
    await session.commit()
    await session.refresh(user)

    await call.message.edit_text(
        await user_card_text(session, user),
        reply_markup=user_card_kb(
            user.id, block, page, is_vip=user.is_vip, is_staff=user.is_staff,
        ),
    )
    await call.answer("✅ Готово")


# --- Уничтожение юзера (Блок «Ревизия») ---------------------------------------

@router.callback_query(F.data.startswith(f"{CB_PANEL}:udel:"))
async def cb_panel_user_delete_ask(call: CallbackQuery, session: AsyncSession) -> None:
    from bot.keyboards.inline import user_wipe_confirm_kb

    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Уже удалён", show_alert=True)
        return
    if user.is_admin or user.tg_id in settings.admin_ids:
        await call.answer("Нельзя удалить админа.", show_alert=True)
        return
    devices = await repo.count_active_devices(session, user.id)
    bypass = await repo.count_active_wdtt_for_user(session, user.id)
    from bot.services.pricing import fmt_rub
    await call.message.edit_text(
        f"🗑 <b>Стереть юзера {user.full_name or user.tg_id} из БД?</b>\n\n"
        "Будет удалено безвозвратно:\n"
        f"• устройств: <b>{devices}</b>, обходов: <b>{bypass}</b> "
        "(конфиги отзываются с серверов сразу)\n"
        f"• баланс <b>{fmt_rub(user.balance_kopeks)}</b> и вся история операций\n"
        "• история поддержки, неоплаченные счета\n"
        "• его записи в журнале действий (в общей ленте останется только "
        "само стирание)\n\n"
        "⚠️ Если он снова напишет боту — создастся заново как новый юзер "
        "и ПОЛУЧИТ НОВЫЙ ТРИАЛ. Для наказания используй «🚫 Заблокировать», "
        "удаление — для мусорных/тестовых аккаунтов и «сотрите мои данные».",
        reply_markup=user_wipe_confirm_kb(user.id, page),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:udelc:"))
async def cb_panel_user_delete_confirm(call: CallbackQuery, session: AsyncSession) -> None:
    from bot.services import user_wipe

    parts = call.data.split(":")
    user_id, page = int(parts[2]), int(parts[3])
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        await call.answer("Уже удалён", show_alert=True)
        return
    if user.is_admin or user.tg_id in settings.admin_ids:
        await call.answer("Нельзя удалить админа.", show_alert=True)
        return
    await call.answer("⏳ Стираю...")
    # Пишем ДО стирания: вместе с юзером уйдут и его строки, а событие должно
    # остаться в общей ленте. target_user_id=None здесь не косметика — стирание
    # чистит журнал именно по target_user_id, и с проставленной ссылкой это
    # событие снесло бы само себя. Держать ссылку и нельзя: id стёртого юзера
    # SQLite отдаст следующему зарегистрировавшемуся (см. purge_user_records),
    # и запись про чужое стирание всплыла бы в карточке новичка.
    await repo.log_action(
        session, AuditAction.USER_WIPED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=None,
        target_type="user",
        target_id=user.id,
        details=f"Стёрт юзер tg_id {user.tg_id} (@{user.username or '—'})",
    )
    res = await user_wipe.wipe_user(session, user)
    await session.commit()
    lines = [
        f"🗑 Юзер <code>{res.tg_id}</code> стёрт из БД.",
        f"• Конфигов отозвано и снято с серверов: {res.revoked_items}",
        f"• Удалено записей: платежи {res.purged.get('balance_txs', 0)}, "
        f"счета {res.purged.get('invoices', 0)}, "
        f"поддержка {res.purged.get('support_msgs', 0)}, "
        f"журнал {res.history_rows}",
    ]
    if res.purged.get("referrals_unlinked"):
        lines.append(f"• Отвязано рефералов: {res.purged['referrals_unlinked']}")
    lines.append(
        "<i>Строки конфигов помечены отозванными и доудалятся ретеншном за 30 "
        "дней (SSH-снятие при этом повторится, если сейчас не прошло).</i>"
    )
    await call.message.edit_text(
        "\n".join(lines), reply_markup=back_to_users_kb(page)
    )
