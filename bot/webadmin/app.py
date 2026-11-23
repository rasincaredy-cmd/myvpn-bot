"""Сборка веб-панели администратора: маршруты, статика и API."""
from __future__ import annotations

from pathlib import Path
from aiohttp import web
from loguru import logger

from bot.webadmin import auth, views_api

STATIC_DIR = Path(__file__).parent / "static"


async def handle_admin_index(request: web.Request) -> web.Response:
    # 1. Проверяем magic-токен из URL (переход из Telegram-бота по кнопке)
    magic_token = request.query.get("token") or request.query.get("auth")
    if magic_token:
        admin_id = auth.verify_magic_login_token(magic_token)
        if admin_id:
            session_token = auth.create_session(admin_id)
            response = web.HTTPFound(location="/admin")
            response.set_cookie(
                auth._COOKIE_NAME,
                session_token,
                max_age=86400,
                httponly=True,
                samesite="Lax",
            )
            return response

    # 2. Проверяем текущую сессию
    current_admin = auth.get_current_admin(request)
    if current_admin is None:
        # Не авторизован — показываем страницу с инструкцией получить ссылку в боте
        unauth_html = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Вход в панель администратора</title>
  <style>
    body { background: #08090d; color: #f8fafc; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #12141c; border: 1px solid #1e2230; border-radius: 12px; padding: 32px; max-width: 420px; text-align: center; }
    h2 { margin-bottom: 12px; font-size: 18px; }
    p { color: #94a3b8; font-size: 14px; line-height: 1.5; margin-bottom: 20px; }
    code { background: #000; padding: 3px 8px; border-radius: 4px; color: #f59e0b; font-family: monospace; }
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size: 40px; margin-bottom: 16px;">🔒</div>
    <h2>Доступ ограничен</h2>
    <p>Для входа в веб-панель управления отправьте команду <code>/admin_web</code> в вашем Telegram-боте и перейдите по персональной ссылке.</p>
  </div>
</body>
</html>"""
        return web.Response(text=unauth_html, content_type="text/html", charset="utf-8", status=401)

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store, no-cache"},
    )


async def handle_admin_static(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    types = {
        "admin.css": "text/css",
        "admin.js": "application/javascript",
    }
    if name not in types:
        raise web.HTTPNotFound
    content = (STATIC_DIR / name).read_text(encoding="utf-8")
    return web.Response(
        text=content,
        content_type=types[name],
        headers={"Cache-Control": "no-cache"},
    )


def add_webadmin_routes(app: web.Application) -> None:
    # Главная страница админки
    app.router.add_get("/admin", handle_admin_index)
    app.router.add_get("/admin/", handle_admin_index)
    app.router.add_get("/admin/static/{name}", handle_admin_static)

    # API админки (все защищены require_admin)
    app.router.add_get("/admin/api/overview", auth.require_admin(views_api.api_overview))
    app.router.add_get("/admin/api/servers", auth.require_admin(views_api.api_servers_list))
    app.router.add_post("/admin/api/servers/{id}", auth.require_admin(views_api.api_server_update))
    app.router.add_get("/admin/api/users", auth.require_admin(views_api.api_users_list))
    app.router.add_get("/admin/api/users/{id}", auth.require_admin(views_api.api_user_detail))
    app.router.add_post("/admin/api/users/{id}/subscription", auth.require_admin(views_api.api_user_subscription))
    app.router.add_post("/admin/api/users/{id}/actions", auth.require_admin(views_api.api_user_action))
    app.router.add_post("/admin/api/users/{id}/wipe", auth.require_admin(views_api.api_user_wipe))
    app.router.add_get("/admin/api/tariffs", auth.require_admin(views_api.api_tariffs_get))
    app.router.add_get("/admin/api/audit", auth.require_admin(views_api.api_audit_list))
    app.router.add_post("/admin/api/broadcast", auth.require_admin(views_api.api_broadcast_send))

    # Устройства и обходы юзеров
    app.router.add_post("/admin/api/users/{id}/devices/{device_id}/send_config", auth.require_admin(views_api.api_user_device_send_config))
    app.router.add_post("/admin/api/users/{id}/devices/{device_id}/revoke", auth.require_admin(views_api.api_user_device_revoke))
    app.router.add_post("/admin/api/users/{id}/bypass/{access_id}/revoke", auth.require_admin(views_api.api_user_bypass_revoke))

    # Обслуживание серверов
    app.router.add_get("/admin/api/servers/{id}/harden_check", auth.require_admin(views_api.api_server_harden_check))
    app.router.add_post("/admin/api/servers/{id}/sync_peers", auth.require_admin(views_api.api_server_sync_peers))
    app.router.add_post("/admin/api/servers/{id}/wdtt_update", auth.require_admin(views_api.api_server_wdtt_update))
    app.router.add_get("/admin/api/servers/{id}/wdtt_probe", auth.require_admin(views_api.api_server_wdtt_probe))
