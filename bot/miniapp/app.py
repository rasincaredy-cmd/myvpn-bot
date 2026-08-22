"""Сборка мини-приложения: страница и её API.

Живёт внутри того же процесса, что и бот, и слушает тот же локальный порт, что
приёмник уведомлений об оплате: наружу оба выставляет nginx, он же держит
TLS-сертификат. Telegram открывает только https, поэтому иначе мини-приложение
не запустится вовсе.
"""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from bot.miniapp import views_account as acc
from bot.miniapp import views_devices as dev

STATIC_DIR = Path(__file__).parent / "static"

# Версия страницы. Меняется при каждой правке вёрстки: Telegram кеширует файлы
# мини-приложения агрессивно, и без метки в адресе юзер неделю смотрел бы на
# старый экран.
ASSET_VERSION = "1"

# Заголовки безопасности страницы. Смысл: даже если в текст когда-нибудь
# просочится чужая разметка, ей неоткуда будет загрузить чужой код и некуда
# отправить данные. Скрипт Telegram — единственный внешний источник, без него
# мини-приложения не существует.
_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://telegram.org"
)


def _page_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": _CSP,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        # Страница показывает конфиги и ссылки — это ключи от VPN. Кешировать
        # их браузеру незачем.
        "Cache-Control": "no-store",
    }


async def _index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{v}}", ASSET_VERSION)
    return web.Response(text=html, content_type="text/html", headers=_page_headers())


async def _asset(request: web.Request) -> web.Response:
    """Отдаём только два известных файла и только их.

    Обычная раздача каталога на живом сервере — лишний риск: адрес светится в
    кабинете провайдера и его сканируют. Здесь физически нечего запросить,
    кроме стилей и скрипта.
    """
    name = request.match_info["name"]
    types = {"app.css": "text/css", "app.js": "application/javascript"}
    if name not in types:
        raise web.HTTPNotFound
    return web.Response(
        text=(STATIC_DIR / name).read_text(encoding="utf-8"),
        content_type=types[name],
        headers={"Cache-Control": "public, max-age=86400"},
    )


def add_routes(app: web.Application) -> None:
    app.router.add_get("/app", _index)
    app.router.add_get("/app/", _index)
    app.router.add_get("/app/{name}", _asset)

    app.router.add_get("/api/state", acc.state)
    app.router.add_get("/api/tariff", acc.tariff)
    app.router.add_post("/api/tariff/change", acc.tariff_change)
    app.router.add_post("/api/tariff/buy", acc.tariff_buy)
    app.router.add_post("/api/autopay", acc.autopay)
    app.router.add_get("/api/history", acc.history)
    app.router.add_post("/api/deposit", acc.deposit)
    app.router.add_get("/api/referral", acc.referral_info)

    app.router.add_get("/api/devices", dev.devices)
    app.router.add_post("/api/devices", dev.device_create)
    app.router.add_get("/api/devices/{id}", dev.device_card)
    app.router.add_post("/api/devices/{id}/rename", dev.device_rename)
    app.router.add_post("/api/devices/{id}/delete", dev.device_delete)

    app.router.add_get("/api/peers/{id}", dev.peer_config)
    app.router.add_get("/api/peers/{id}/qr", dev.peer_qr)
    app.router.add_post("/api/peers/{id}/send", dev.peer_send)

    app.router.add_get("/api/bypass", dev.bypass_list)
    app.router.add_post("/api/bypass", dev.bypass_create)
    app.router.add_post("/api/bypass/{id}/delete", dev.bypass_delete)
