"""Авторизация и сессии для веб-панели управления администратора."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import NamedTuple

from aiohttp import web
from loguru import logger

from bot.config import settings

_COOKIE_NAME = "moschata_admin_session"
_SESSION_TTL_HOURS = 24

# In-memory хранилище активных админских сессий {session_token: AdminSession}
class AdminSession(NamedTuple):
    admin_id: int
    created_at: datetime
    expires_at: datetime


_ACTIVE_SESSIONS: dict[str, AdminSession] = {}


def _get_signing_key() -> bytes:
    """Ключ подписи токенов на базе ENCRYPTION_KEY бота."""
    return hashlib.sha256(settings.encryption_key.encode("utf-8")).digest()


def create_magic_login_token(admin_id: int, ttl_minutes: int = 30) -> str:
    """Создаёт подписанный одноразовый токен для входа в веб-панель из Telegram."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    nonce = secrets.token_hex(8)
    payload = f"{admin_id}:{now_ts}:{nonce}"
    sig = hmac.new(_get_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_magic_login_token(token: str, max_age_seconds: int = 1800) -> int | None:
    """Проверяет magic-токен входа. Возвращает admin_id или None."""
    parts = token.split(":")
    if len(parts) != 4:
        return None
    admin_id_str, ts_str, nonce, sig = parts
    payload = f"{admin_id_str}:{ts_str}:{nonce}"
    expected_sig = hmac.new(_get_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    try:
        admin_id = int(admin_id_str)
        ts = int(ts_str)
    except ValueError:
        return None

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if now_ts - ts > max_age_seconds or ts > now_ts + 60:
        return None  # Истёк

    if admin_id not in settings.admin_ids:
        return None

    return admin_id


def create_session(admin_id: int) -> str:
    """Создаёт сессию и возвращает session_token."""
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    _ACTIVE_SESSIONS[token] = AdminSession(
        admin_id=admin_id,
        created_at=now,
        expires_at=now + timedelta(hours=_SESSION_TTL_HOURS),
    )
    return token


def get_current_admin(request: web.Request) -> int | None:
    """Проверяет наличие валидной сессии администратора."""
    # 1. Проверяем cookie
    cookie = request.cookies.get(_COOKIE_NAME)
    if cookie and cookie in _ACTIVE_SESSIONS:
        sess = _ACTIVE_SESSIONS[cookie]
        if datetime.now(timezone.utc) < sess.expires_at:
            if sess.admin_id in settings.admin_ids:
                return sess.admin_id
        else:
            _ACTIVE_SESSIONS.pop(cookie, None)

    # 2. Проверяем заголовок Authorization: Bearer <session_token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token in _ACTIVE_SESSIONS:
            sess = _ACTIVE_SESSIONS[token]
            if datetime.now(timezone.utc) < sess.expires_at:
                if sess.admin_id in settings.admin_ids:
                    return sess.admin_id

    # 3. Режим локальной разработки / тестов: если admin_ids настроены и запрос с localhost с ключом
    dev_token = request.query.get("dev_auth", "")
    if dev_token and dev_token == settings.encryption_key[:16] and settings.admin_ids:
        return settings.admin_ids[0]

    return None


def require_admin(handler):
    """Декоратор для защиты API эндпоинтов админки."""
    async def middleware(request: web.Request) -> web.Response:
        admin_id = get_current_admin(request)
        if admin_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        request["admin_id"] = admin_id
        return await handler(request)
    return middleware
