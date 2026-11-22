"""HTTP-эндпоинты для отдачи ссылок на подписки (/sub/{token})."""
from __future__ import annotations

import html
import io
import qrcode
from aiohttp import web
from loguru import logger

from bot.db.base import session_scope
from bot.db import repo
from bot.services import subscriptions


async def handle_subscription_feed(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "").strip()
    if not token:
        raise web.HTTPNotFound(text="Subscription token missing")

    async with session_scope() as session:
        user = await repo.get_user_by_sub_token(session, token)
        if user is None:
            logger.warning("Запрос подписки с неверным токеном: {}", token[:8])
            raise web.HTTPNotFound(text="Subscription not found or expired")

        data = await subscriptions.build_subscription_data(session, user)

    # Определение формата:
    # 1. Явный query param ?format=json|raw|base64|text
    # 2. По заголовкам Accept / User-Agent
    fmt = request.query.get("format", "").lower()
    if not fmt:
        ua = request.headers.get("User-Agent", "").lower()
        accept = request.headers.get("Accept", "").lower()
        if "amnezia" in ua or "moschata" in ua or "application/json" in accept:
            fmt = "json"
        elif "v2ray" in ua or "clash" in ua or "sing-box" in ua:
            fmt = "base64"
        else:
            fmt = "json"

    body, content_type, headers = subscriptions.format_subscription_response(
        data, requested_format=fmt
    )
    return web.Response(text=body, content_type=content_type, charset="utf-8", headers=headers)


async def handle_subscription_info_page(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "").strip()
    if not token:
        raise web.HTTPNotFound

    async with session_scope() as session:
        user = await repo.get_user_by_sub_token(session, token)
        if user is None:
            raise web.HTTPNotFound(text="Подписка не найдена")
        data = await subscriptions.build_subscription_data(session, user)

    status_color = "#10B981" if data["is_active"] else "#EF4444"
    status_text = "Активна" if data["is_active"] else ("Заблокирована" if data["is_blocked"] else "Истекла")

    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    full_url = str(request.url.with_scheme(proto).with_path(f"/sub/{token}").with_query(""))

    html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Moschata VPN — Подписка</title>
  <style>
    :root {{
      --bg: #090a0f;
      --card: #13151f;
      --border: #232738;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #f59e0b;
      --emerald: #10b981;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      max-width: 480px;
      width: 100%;
      padding: 32px 24px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.6);
    }}
    .header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 9999px;
      background: rgba(16, 185, 129, 0.12);
      color: {status_color};
      border: 1px solid {status_color}40;
    }}
    .title {{ font-size: 22px; font-weight: 700; }}
    .status-box {{
      background: #0d0e15;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      font-size: 14px;
    }}
    .row .label {{ color: var(--muted); }}
    .row .val {{ font-weight: 600; font-family: ui-monospace, monospace; }}
    .link-box {{
      background: #0b0c13;
      border: 1px dashed var(--border);
      border-radius: 10px;
      padding: 12px;
      font-family: ui-monospace, monospace;
      font-size: 13px;
      word-break: break-all;
      color: var(--accent);
      margin-bottom: 16px;
      user-select: all;
    }}
    .btn {{
      display: block;
      width: 100%;
      text-align: center;
      padding: 12px 16px;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: all 0.15s ease;
      text-decoration: none;
    }}
    .btn-primary {{
      background: #f59e0b;
      color: #000;
      margin-bottom: 10px;
    }}
    .btn-primary:hover {{ background: #d97706; }}
    .btn-secondary {{
      background: #1f2333;
      color: var(--text);
    }}
    .btn-secondary:hover {{ background: #2b3147; }}
    .hint {{
      font-size: 12px;
      color: var(--muted);
      text-align: center;
      margin-top: 16px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div style="font-size: 32px;">🦫</div>
      <div>
        <div class="title">Moschata VPN</div>
        <div class="badge">● {status_text}</div>
      </div>
    </div>

    <div class="status-box">
      <div class="row">
        <span class="label">Срок действия</span>
        <span class="val">{html.escape(data['description'])}</span>
      </div>
      <div class="row">
        <span class="label">Использовано</span>
        <span class="val">{data['trafficUsedMb']} МБ / {f"{data['trafficLimitMb']} МБ" if data['trafficLimitMb'] > 0 else "Безлимит"}</span>
      </div>
      <div class="row">
        <span class="label">Локаций VPN</span>
        <span class="val">{len(data['servers'])}</span>
      </div>
      <div class="row">
        <span class="label">Резервные подключения</span>
        <span class="val">{len(data['profiles'])}</span>
      </div>
    </div>

    <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--muted);">Ссылка подписки:</div>
    <div class="link-box" id="subUrl">{full_url}</div>

    <button class="btn btn-primary" onclick="copyLink()">📋 Скопировать ссылку</button>
    <a href="{full_url}?format=json" class="btn btn-secondary">📥 Скачать конфигурацию (.json)</a>

    <div class="hint">
      Вставьте эту ссылку в приложении <b>Moschata VPN</b> (Профили → + → Из ссылки) или в <b>Amnezia Clean</b>. Конфигурация серверов и резервных подключений обновится автоматически.
    </div>
  </div>

  <script>
    function copyLink() {{
      const text = document.getElementById('subUrl').innerText;
      navigator.clipboard.writeText(text).then(() => {{
        alert('Ссылка на подписку скопирована в буфер обмена!');
      }});
    }}
  </script>
</body>
</html>
"""
    return web.Response(text=html_content, content_type="text/html", charset="utf-8")


def add_sub_routes(app: web.Application) -> None:
    app.router.add_get("/sub/{token}", handle_subscription_feed)
    app.router.add_get("/sub/{token}/", handle_subscription_feed)
    app.router.add_get("/sub/{token}/info", handle_subscription_info_page)
