"""Экран «Журнал»: лента важных событий и история конкретного юзера.

Читает только админ — роутер уже под AdminFilter родителя (см. admin/__init__).
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, AuditLog
from bot.keyboards.inline import CB_PANEL, audit_list_kb, user_history_kb
from bot.services.pricing import fmt_rub
from bot.utils.timefmt import fmt_msk

PAGE_SIZE = 10

# Человеческие названия событий. Ключ — код из базы: старые записи должны
# читаться и после переименования кнопок в интерфейсе.
_TITLES: dict[str, str] = {
    AuditAction.BALANCE_TOPUP: "💰 Пополнение",
    AuditAction.BALANCE_CHARGE: "🧾 Списание за подписку",
    AuditAction.REFERRAL_REWARD: "👥 Реферальная награда",
    AuditAction.ADMIN_CREDIT: "🎁 Начисление админом",
    AuditAction.CONFIG_ISSUED: "📄 Выдан конфиг",
    AuditAction.CONFIG_REVOKED: "🚫 Конфиг отозван",
    AuditAction.CONFIG_REVIVED: "♻️ Конфиг оживлён",
    AuditAction.SUB_GRANTED: "🎁 Подписка выдана",
    AuditAction.SUB_REVOKED: "⏹ Подписка отключена",
    AuditAction.USER_BLOCKED: "⛔ Юзер заблокирован",
    AuditAction.USER_UNBLOCKED: "✅ Юзер разблокирован",
    AuditAction.TARIFF_CHANGED: "⚙️ Тариф изменён",
    AuditAction.USER_WIPED: "🗑 Юзер стёрт",
    AuditAction.SERVER_DOWN: "🔴 Сервер упал",
    AuditAction.SERVER_UP: "🟢 Сервер поднялся",
}

# Направление денег определяется КОДОМ события, а не знаком суммы: и пополнение,
# и списание за подписку пишутся в журнал положительным числом (конвенция
# зафиксирована у AuditLog.amount_kopeks). По знаку их не различить, а админу
# надо видеть с одного взгляда, пришли деньги человеку или ушли.
_MONEY_IN = frozenset({AuditAction.BALANCE_TOPUP, AuditAction.REFERRAL_REWARD})
_MONEY_OUT = frozenset({AuditAction.BALANCE_CHARGE})

router = Router(name="admin_audit")


def _fmt_amount(action: str, amount_kopeks: int) -> str:
    """Сумма с явным направлением. ADMIN_CREDIT — единственное событие со
    значащим знаком: админ и начисляет, и списывает одним и тем же кодом,
    поэтому его направление читается из знака, а не из таблиц выше."""
    if action == AuditAction.ADMIN_CREDIT:
        incoming = amount_kopeks >= 0
    elif action in _MONEY_OUT:
        incoming = False
    elif action in _MONEY_IN:
        incoming = True
    else:
        # Неденежное событие вдруг с суммой: направление выдумывать нельзя,
        # показываем как есть.
        return f"Сумма: <b>{fmt_rub(amount_kopeks)}</b>"
    # Минус — тот же U+2212, что в fmt_rub, чтобы суммы в ленте не разъезжались
    # по виду. Знак дублируется словом: одного «−» в потоке строк мало.
    sign = "+" if incoming else "−"
    where = "зачислено юзеру" if incoming else "списано с юзера"
    return f"Сумма: <b>{sign}{fmt_rub(abs(amount_kopeks))}</b> — {where}"


def fmt_audit_row(row: AuditLog) -> str:
    """Одна строка журнала для админа. Время — МСК, деньги — рублями."""
    title = _TITLES.get(row.action, row.action)
    parts = [f"<b>{title}</b> — {fmt_msk(row.created_at)} МСК"]
    if row.amount_kopeks is not None:
        parts.append(_fmt_amount(row.action, row.amount_kopeks))
    if row.actor_tg_id is not None:
        who = "админ" if row.actor_is_admin else "юзер"
        parts.append(f"Кто: {who} <code>{row.actor_tg_id}</code>")
    else:
        parts.append("Кто: бот")
    if row.details:
        # details может содержать метку устройства, которую придумал юзер —
        # без экранирования «<b>» из метки поехало бы в разметку сообщения.
        parts.append(html.escape(row.details))
    return "\n".join(parts)


@router.callback_query(F.data.startswith(f"{CB_PANEL}:audit:"))
async def cb_audit_list(call: CallbackQuery, session: AsyncSession) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    total = await repo.count_audit(session)
    rows = await repo.list_audit(session, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    if not rows:
        text = "📝 <b>Журнал</b>\n\nПока пусто."
    else:
        shown_to = page * PAGE_SIZE + len(rows)
        body = "\n\n".join(fmt_audit_row(r) for r in rows)
        text = (
            f"📝 <b>Журнал</b> — показано {page * PAGE_SIZE + 1}–{shown_to} из {total}"
            f"\n\n{body}"
        )

    await call.message.edit_text(
        text,
        reply_markup=audit_list_kb(
            page,
            has_prev=page > 0,
            has_next=(page + 1) * PAGE_SIZE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_PANEL}:uhist:"))
async def cb_user_history(call: CallbackQuery, session: AsyncSession) -> None:
    """История одного юзера. Кроме страницы самой истории тащим страницу списка
    юзеров: кнопка возврата ведёт в карточку, а из неё — на ту же страницу
    списка, откуда админ пришёл."""
    _, _, rest = call.data.partition(f"{CB_PANEL}:uhist:")
    user_id_s, page_s, hpage_s = rest.split(":")
    user_id, page, hpage = int(user_id_s), int(page_s), int(hpage_s)

    total = await repo.count_audit_for_user(session, user_id)
    rows = await repo.list_audit_for_user(
        session, user_id, limit=PAGE_SIZE, offset=hpage * PAGE_SIZE
    )

    if not rows:
        text = "🕘 <b>История юзера</b>\n\nСобытий пока нет."
    else:
        body = "\n\n".join(fmt_audit_row(r) for r in rows)
        text = f"🕘 <b>История юзера</b> — всего {total}\n\n{body}"

    await call.message.edit_text(
        text,
        reply_markup=user_history_kb(
            user_id, page, hpage,
            has_prev=hpage > 0,
            has_next=(hpage + 1) * PAGE_SIZE < total,
        ),
    )
    await call.answer()
