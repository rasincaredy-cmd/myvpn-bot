"""Вход в мини-приложение: кто открыл страницу и можно ли ему верить.

Страница сообщает о себе сама, поэтому подпись Telegram — единственное, что
отделяет владельца аккаунта от того, кто просто подставил чужой номер в запрос.
Тесты держат этот рубеж: мутируй любую букву в данных — вход обязан отвалиться.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from bot.miniapp import auth

TOKEN = "1234567890:dummy_token_for_tests_xxxxxxxxxxxxxxxxxxxxxxxx"


def sign(fields: dict, *, token: str = TOKEN, drop_signature: bool = False) -> str:
    """Собирает initData ровно так, как это делает Telegram."""
    pairs = sorted(fields.items())
    skip = {"hash", "signature"} if drop_signature else {"hash"}
    check = "\n".join(f"{k}={v}" for k, v in pairs if k not in skip)
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def user_field(tg_id: int = 501, **extra) -> str:
    return json.dumps({"id": tg_id, "first_name": "Влад", **extra})


def fresh(**extra) -> dict:
    return {"auth_date": str(int(time.time())), "user": user_field(), **extra}


class TestSignature:
    def test_valid_data_lets_the_person_in(self) -> None:
        who = auth.check(sign(fresh()), token=TOKEN)
        assert who.tg_id == 501
        assert who.full_name == "Влад"

    def test_changed_user_id_is_rejected(self) -> None:
        """Главный сценарий атаки: подпись чужая, а номер в данных свой."""
        data = sign(fresh())
        forged = data.replace("501", "502")
        assert forged != data
        with pytest.raises(auth.AuthError):
            auth.check(forged, token=TOKEN)

    def test_other_bot_token_is_rejected(self) -> None:
        data = sign(fresh(), token="999:another_bot_token_zzzzzzzzzzzzzzzzzzz")
        with pytest.raises(auth.AuthError):
            auth.check(data, token=TOKEN)

    def test_data_without_hash_is_rejected(self) -> None:
        with pytest.raises(auth.AuthError):
            auth.check(urlencode(fresh()), token=TOKEN)

    def test_signature_field_may_stay_outside_the_check_string(self) -> None:
        """Поле `signature` появилось позже самого механизма, и в документации
        оно исключается из строки проверки только для сторонних сервисов.
        Клиент может прислать любой из двух вариантов — принимаем оба."""
        fields = fresh(signature="ed25519-blob")
        assert auth.check(sign(fields), token=TOKEN).tg_id == 501
        assert auth.check(
            sign(fields, drop_signature=True), token=TOKEN
        ).tg_id == 501


class TestFreshness:
    def test_old_signature_is_rejected(self) -> None:
        """Украденная строка не должна работать вечно: она не привязана ни к
        сессии, ни к устройству."""
        old = {"auth_date": str(int(time.time()) - 48 * 3600), "user": user_field()}
        with pytest.raises(auth.AuthError):
            auth.check(sign(old), token=TOKEN)

    def test_yesterday_still_works(self) -> None:
        """Страницу держат открытой часами — сутки живём."""
        recent = {"auth_date": str(int(time.time()) - 3600), "user": user_field()}
        assert auth.check(sign(recent), token=TOKEN).tg_id == 501

    def test_missing_auth_date_is_rejected(self) -> None:
        with pytest.raises(auth.AuthError):
            auth.check(sign({"user": user_field()}), token=TOKEN)


class TestUser:
    def test_data_without_user_is_rejected(self) -> None:
        """Так открывают приложение из инлайн-режима: подпись верная, а кто
        именно её открыл — неизвестно. Пускать некого."""
        with pytest.raises(auth.AuthError):
            auth.check(sign({"auth_date": str(int(time.time()))}), token=TOKEN)

    def test_full_name_is_glued_from_both_parts(self) -> None:
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 7, "first_name": "Иван", "last_name": "Петров"}),
        }
        assert auth.check(sign(fields), token=TOKEN).full_name == "Иван Петров"

    def test_nameless_account_falls_back_to_username(self) -> None:
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 7, "username": "vlad"}),
        }
        who = auth.check(sign(fields), token=TOKEN)
        assert who.full_name == "vlad" and who.username == "vlad"
