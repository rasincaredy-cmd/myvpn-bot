#!/usr/bin/env python3
"""Живая проверка мини-приложения: страница отдаётся, API узнаёт человека.

Зачем отдельный скрипт: со стороны бота мини-приложение выглядит рабочим всегда
— оно живёт в том же процессе. Сломаться может то, что процессу не видно:
сертификат, правила nginx, адрес в настройке. Проверять это глазами через
телефон долго, а после каждой смены IP или перевыпуска сертификата — нужно.

Запуск на хосте бота:

    env -C /root/myvpn-bot python3.11 scripts/miniapp_smoke.py

Подпись собирается из BOT_TOKEN, как это делает Telegram, поэтому скрипт видит
ровно то же, что увидел бы человек, открывший приложение. Секретов не печатает.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode


def read_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    env_file = Path(__file__).resolve().parent.parent / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def init_data(token: str, tg_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": tg_id, "first_name": "Smoke"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def call(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    base = (read_env("MINIAPP_URL") or "").rstrip("/")
    if not base:
        print("MINIAPP_URL пуст — мини-приложение выключено настройкой")
        return 1
    if base.endswith("/app"):
        base = base[: -len("/app")]
    token = read_env("BOT_TOKEN")
    tg_id = int(sys.argv[1]) if len(sys.argv) > 1 else int(
        read_env("ADMIN_IDS").split(",")[0]
    )

    ok = True

    status, body = call(f"{base}/app/")
    page_ok = status == 200 and b"<title>" in body
    print(f"страница {base}/app/ → {status} {'ok' if page_ok else 'ПЛОХО'}")
    ok &= page_ok

    status, _ = call(f"{base}/api/state")
    print(f"API без подписи → {status} {'ok' if status == 401 else 'ПЛОХО'}")
    ok &= status == 401

    status, body = call(
        f"{base}/api/state",
        {"Authorization": "tma " + init_data(token, tg_id)},
    )
    data = json.loads(body) if status == 200 else {}
    good = status == 200 and data.get("ok")
    print(f"API с подписью (юзер {tg_id}) → {status} {'ok' if good else 'ПЛОХО'}")
    if good:
        sub = data["sub"]
        print(
            f"  подписка: {'активна' if sub['active'] else 'на паузе'}, "
            f"устройств {sub['devices_used']}/{sub['devices_max']}, "
            f"подключений {sub['bypass_used']}/{sub['bypass_max']}"
        )
    ok &= bool(good)

    print("ИТОГ:", "мини-приложение живо" if ok else "есть проблема")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
