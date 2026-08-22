"""HTTP-приёмник уведомлений об оплате (пока только Platega).

Слушает ТОЛЬКО на localhost: наружу торчит nginx, он же держит TLS-сертификат
(Platega принимает лишь https с сертификатом от доверенного центра). Такое
разделение позволяет обновлять сертификат раз в несколько дней, не трогая
бота.

Адрес для кабинета провайдера: https://<адрес сервера>/platega/webhook

Отвечаем 200 на всё, что дошло до обработчика с верными ключами, — даже если
платёж чужой или уже закрыт. Иначе провайдер сочтёт доставку неудачной и
повторит её дважды (через 5 и 10 минут), а смысла в повторе нет.
"""
from __future__ import annotations

import json

from aiohttp import web
from loguru import logger

from bot.config import settings
from bot.db.base import session_scope
from bot.services import platega_webhook

_WEBHOOK_PATH = "/platega/webhook"


async def _handle_platega(request: web.Request) -> web.Response:
    if not platega_webhook.headers_ok(request.headers):
        # Сюда попадают и сканеры интернета, которым адрес просто угадался.
        logger.warning("Platega webhook: чужие ключи, {}", request.remote)
        return web.Response(status=401, text="unauthorized")

    raw = await request.text()
    if not raw.strip():
        # Пустой POST — проверка адреса при сохранении в личном кабинете.
        logger.info("Platega webhook: проверка адреса пройдена")
        return web.Response(text="ok")

    try:
        body = json.loads(raw)
    except ValueError:
        logger.warning("Platega webhook: тело не JSON: {}", raw[:200])
        return web.Response(text="ok")
    if not isinstance(body, dict):
        logger.warning("Platega webhook: неожиданное тело: {}", raw[:200])
        return web.Response(text="ok")

    async with session_scope() as session:
        dep = await platega_webhook.handle_payload(session, body)
        chargeback = platega_webhook.is_chargeback(body)
        row = await platega_webhook.find_payment(session, body) if chargeback else None
        await session.commit()

    # Уведомления шлём ПОСЛЕ коммита: деньги уже на балансе, и падение
    # отправки сообщения не должно откатывать зачисление.
    if dep is not None and dep.credited:
        from bot.handlers.balance import notify_deposit

        await notify_deposit(dep)
        await _autopay_after_deposit(dep)
    if chargeback and row is not None:
        await _alert_admins_chargeback(row)
    return web.Response(text="ok")


async def _autopay_after_deposit(dep) -> None:
    """Подписка истекла, автопродление включено — продлеваем сразу, как это
    делает кнопка «Я оплатил»: юзер не должен ждать тика планировщика."""
    from bot.db import repo
    from bot.services import billing

    if dep.user is None:
        return
    async with session_scope() as session:
        user = await repo.get_user_by_id(session, dep.user.id)
        if user is None:
            return
        res = await billing.autopay_if_expired(session, user)
        if res is None:
            return
        await session.commit()
        from bot.handlers.balance import notify_autopay

        await notify_autopay(user, res)


async def _alert_admins_chargeback(row) -> None:
    """Возврат по платежу — админам в личку; ошибки Telegram глотаем."""
    from bot.loader import bot
    from bot.services.pricing import fmt_rub

    text = (
        "⚠️ <b>Возврат платежа Platega</b>\n"
        f"Счёт: <code>{row.transaction_id}</code>\n"
        f"Юзер: {row.user_id}, сумма: {fmt_rub(row.amount_kopeks)}\n"
        "Баланс юзера не тронут — разбери вручную."
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post(_WEBHOOK_PATH, _handle_platega)
    if settings.miniapp_url:
        # Мини-приложение живёт на этом же порту: наружу его выставляет тот же
        # nginx, и второй слушатель означал бы второй сертификат и второй порт
        # в брандмауэре ради той же самой страницы.
        from bot.miniapp import add_routes

        add_routes(app)
    return app


async def run() -> None:
    """Поднимает локальный веб-сервер, если он включён настройкой.

    На нём висят приём уведомлений об оплате и мини-приложение. Падение сервера
    не должно ронять бота: платежи в этом случае доберёт поллинг, а без
    мини-приложения бот остаётся полностью рабочим — все его разделы на месте.
    """
    if not settings.webhook_port:
        return
    try:
        runner = web.AppRunner(build_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", settings.webhook_port)
        await site.start()
    except Exception:
        logger.exception("Приёмник уведомлений не поднялся")
        return
    logger.info(
        "Локальный веб-сервер на 127.0.0.1:{} — приём оплат{}",
        settings.webhook_port,
        " и мини-приложение" if settings.miniapp_url else "",
    )
