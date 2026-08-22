"""API мини-приложения: то же, что умеет бот, но по HTTP.

Проверяем ровно те рубежи, которых у бота нет по построению: у кнопки в чате
отправитель известен Telegram'у, а у HTTP-запроса — только подписи. Поэтому
здесь: чужой номер устройства, запрос без подписи, заблокированный юзер,
частота запросов и то, что деньги и лимиты считаются теми же правилами, что в
боте.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.db import repo
from bot.db.base import Base
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services.amnezia import AmneziaParams
from bot.handlers import configs
from bot.miniapp import http as mini_http
from bot.miniapp.app import add_routes
from bot.services import bypass_issue
from bot.services.crypto import encrypt
from bot.services.pricing import (
    PRESETS,
    best_value_key,
    fmt_rub,
    monthly_price_kopeks,
    stars_for_kopeks,
)
from tests.test_miniapp_auth import TOKEN, sign, user_field

TG_ID = 700100

# Настоящий приватный ключ X25519 в base64: из него выводится публичный, когда
# конфиг превращается в ссылку vpn://.
PRIV_KEY = "aFsHkOZBs6Z4pF7oe0v0xJb1sHRoiwrIRlXbGV3bMFY="


def init_data(tg_id: int = TG_ID) -> str:
    return sign({"auth_date": str(int(time.time())), "user": user_field(tg_id)})


class Env:
    """Поднятый сервер приложения плюс доступ к той же базе, что видит API."""

    def __init__(self, client: TestClient, factory) -> None:
        self.client = client
        self.factory = factory

    async def get(self, path: str, *, auth: str | None = None, expect: int = 200):
        return await self._call("GET", path, None, auth, expect)

    async def post(self, path: str, body: dict | None = None, *,
                   auth: str | None = None, expect: int = 200):
        return await self._call("POST", path, body or {}, auth, expect)

    async def _call(self, method, path, body, auth, expect):
        # Ограничение частоты общее на процесс: между шагами теста ждать
        # по две секунды незачем.
        mini_http._recent.clear()
        headers = {}
        token = init_data() if auth is None else auth
        if token:
            headers["Authorization"] = "tma " + token
        res = await self.client.request(method, path, headers=headers, json=body)
        assert res.status == expect, (path, res.status, await res.text())
        if res.content_type == "application/json":
            return await res.json()
        return await res.read()


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/miniapp.sqlite3")
    async with engine.begin() as conn:
        from bot.db import models  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(mini_http, "SessionMaker", factory)
    monkeypatch.setattr(settings, "miniapp_url", "https://example.test/app/")

    app = web.Application()
    add_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield Env(client, factory)
    finally:
        await client.close()
        await engine.dispose()


async def make_user(env: Env, *, tg_id: int = TG_ID, devices: int = 2, bypass: int = 1,
                    balance: int = 0, days: int | None = 30, trial: bool = False):
    async with env.factory() as session:
        user = await repo.get_or_create_user(
            session, tg_id=tg_id, username="u", full_name="U"
        )
        user.sub_max_devices = devices
        user.sub_max_bypass = bypass
        user.balance_kopeks = balance
        user.is_trial = trial
        user.sub_expires_at = (
            datetime.now(timezone.utc) + timedelta(days=days) if days else None
        )
        await session.commit()
        return user.id


async def make_server(env: Env, *, location: str = "🇳🇱 Нидерланды"):
    async with env.factory() as session:
        server = await repo.create_server(
            session, name="nl", host="1.1.1.1", wg_port=585, owner_tg_id=1,
            status=ServerStatus.READY, location=location,
            server_public_key="pub", server_endpoint="1.1.1.1:585",
            # Параметры обфускации заполняет установщик; у READY-сервера они
            # есть всегда, и без них конфиг собрать нельзя — это не WireGuard.
            awg_params_json=AmneziaParams(
                Jc=5, Jmin=50, Jmax=1000, S1=50, S2=80, H1=10, H2=20, H3=30, H4=40
            ).to_json(),
        )
        await session.commit()
        return server.id


def mute_provision(monkeypatch) -> list:
    """Выдача пиров без SSH: тесты про API, а не про AmneziaWG."""
    made = []

    async def fake(session, server, user, label, *, device_id=None,
                   expires_at=None, log_issue=True):
        peer = Peer(
            server_id=server.id, user_id=user.id, device_id=device_id,
            label=label, ip=f"10.8.0.{len(made) + 2}", public_key=f"pk{len(made)}",
            private_key_enc=encrypt(PRIV_KEY), status=PeerStatus.ACTIVE,
        )
        session.add(peer)
        await session.flush()
        made.append(peer.id)
        return peer, "[Interface]\n"

    monkeypatch.setattr(configs, "_create_peer_for_user", fake)
    return made


class TestDoor:
    async def test_request_without_signature_is_refused(self, env: Env) -> None:
        res = await env.get("/api/state", auth="", expect=401)
        assert res["error"] == "auth"

    async def test_forged_signature_is_refused(self, env: Env) -> None:
        forged = init_data().replace(str(TG_ID), str(TG_ID + 1))
        await env.get("/api/state", auth=forged, expect=401)

    async def test_blocked_user_is_refused(self, env: Env) -> None:
        await make_user(env)
        async with env.factory() as session:
            user = await repo.get_user_by_tg_id(session, TG_ID)
            user.is_blocked = True
            await session.commit()
        res = await env.get("/api/state", expect=403)
        assert res["error"] == "blocked"

    async def test_actions_are_rate_limited(self, env: Env, monkeypatch) -> None:
        """Создание устройства ходит по SSH: десяток нажатий подряд — это
        десяток сессий к серверу."""
        await make_user(env)
        mini_http._recent.clear()
        headers = {"Authorization": "tma " + init_data()}
        first = await env.client.post(
            "/api/devices", headers=headers, json={"label": "Тел"}
        )
        second = await env.client.post(
            "/api/devices", headers=headers, json={"label": "Тел"}
        )
        assert second.status == 429, (first.status, second.status)


class TestState:
    async def test_state_repeats_what_the_bot_shows(self, env: Env) -> None:
        await make_user(env, devices=3, bypass=2, balance=12345, days=10)
        res = await env.get("/api/state")
        assert res["sub"]["devices_max"] == 3
        assert res["sub"]["bypass_max"] == 2
        assert res["sub"]["days_left"] == 9  # неполные сутки не считаются
        assert res["sub"]["active"] is True
        assert res["balance"]["text"] == "123.45 ₽"

    async def test_expired_subscription_is_visible(self, env: Env) -> None:
        await make_user(env, days=-1)
        res = await env.get("/api/state")
        assert res["sub"]["active"] is False


class TestDevices:
    async def test_create_list_rename_delete(self, env: Env, monkeypatch) -> None:
        await make_user(env)
        await make_server(env)
        mute_provision(monkeypatch)

        created = await env.post("/api/devices", {"label": "Телефон"})
        assert created["configs"] == 1

        listed = await env.get("/api/devices")
        assert [d["label"] for d in listed["items"]] == ["Телефон"]
        assert listed["used"] == 1 and listed["can_add"] is True

        await env.post(f"/api/devices/{created['id']}/rename", {"label": "Ноут"})
        card = await env.get(f"/api/devices/{created['id']}")
        assert card["label"] == "Ноут"
        assert card["configs"][0]["location"]

        await env.post(f"/api/devices/{created['id']}/delete")
        assert (await env.get("/api/devices"))["items"] == []

    async def test_limit_is_enforced_on_the_server_side(
        self, env: Env, monkeypatch
    ) -> None:
        """Страницу можно переписать в отладчике браузера — лимит обязан жить
        здесь, а не в кнопке."""
        await make_user(env, devices=1)
        await make_server(env)
        mute_provision(monkeypatch)
        await env.post("/api/devices", {"label": "Первое"})
        res = await env.post("/api/devices", {"label": "Второе"}, expect=400)
        assert res["error"] == "limit"

    async def test_expired_subscription_cannot_add(self, env: Env, monkeypatch) -> None:
        await make_user(env, days=-1)
        await make_server(env)
        mute_provision(monkeypatch)
        res = await env.post("/api/devices", {"label": "Телефон"}, expect=400)
        assert res["error"] == "expired"

    async def test_bad_label_is_refused(self, env: Env) -> None:
        await make_user(env)
        res = await env.post("/api/devices", {"label": "!" * 50}, expect=400)
        assert res["error"] == "bad_label"

    async def test_foreign_device_looks_like_a_missing_one(
        self, env: Env, monkeypatch
    ) -> None:
        """Ответ на чужой номер обязан совпадать с ответом на выдуманный:
        иначе по разнице перебирают чужие устройства."""
        await make_user(env)
        stranger = await make_user(env, tg_id=TG_ID + 5)
        async with env.factory() as session:
            device = await repo.create_device(
                session, user_id=stranger, label="Чужое"
            )
            await session.commit()
            foreign_id = device.id

        mine = await env.get(f"/api/devices/{foreign_id}", expect=404)
        absent = await env.get("/api/devices/999999", expect=404)
        assert mine["message"] == absent["message"]


class TestConfigs:
    async def _peer(self, env: Env, monkeypatch) -> int:
        await make_user(env)
        await make_server(env)
        mute_provision(monkeypatch)
        created = await env.post("/api/devices", {"label": "Телефон"})
        card = await env.get(f"/api/devices/{created['id']}")
        return card["configs"][0]["peer_id"]

    async def test_owner_gets_conf_and_link(self, env: Env, monkeypatch) -> None:
        peer_id = await self._peer(env, monkeypatch)
        res = await env.get(f"/api/peers/{peer_id}")
        assert "[Interface]" in res["conf"]
        assert res["link"].startswith("vpn://")

    async def test_stranger_gets_nothing(self, env: Env, monkeypatch) -> None:
        peer_id = await self._peer(env, monkeypatch)
        await make_user(env, tg_id=TG_ID + 7)
        await env.get(
            f"/api/peers/{peer_id}", auth=init_data(TG_ID + 7), expect=404
        )

    async def test_qr_is_a_png(self, env: Env, monkeypatch) -> None:
        # Рисование QR проверяет свой тест; здесь важно, что картинка отдаётся
        # только владельцу и не кешируется.
        monkeypatch.setattr(
            "bot.miniapp.views_devices.conf_to_qr_png", lambda conf: b"\x89PNG..."
        )
        peer_id = await self._peer(env, monkeypatch)
        mini_http._recent.clear()
        res = await env.client.get(
            f"/api/peers/{peer_id}/qr",
            headers={"Authorization": "tma " + init_data()},
        )
        assert res.status == 200
        assert res.headers["Content-Type"] == "image/png"
        assert res.headers["Cache-Control"] == "no-store"

    async def test_send_to_chat_uses_the_bot(self, env: Env, monkeypatch) -> None:
        peer_id = await self._peer(env, monkeypatch)
        sent = []

        class FakeBot:
            async def send_document(self, chat_id, **kw):
                sent.append(("doc", chat_id))

            async def send_photo(self, chat_id, **kw):
                sent.append(("photo", chat_id))

            async def send_message(self, chat_id, *a, **kw):
                sent.append(("text", chat_id))

        monkeypatch.setattr("bot.loader.bot", FakeBot())
        await env.post(f"/api/peers/{peer_id}/send", {"kind": "file"})
        assert sent == [("doc", TG_ID)]

    async def test_unknown_format_is_refused(self, env: Env, monkeypatch) -> None:
        peer_id = await self._peer(env, monkeypatch)
        await env.post(f"/api/peers/{peer_id}/send", {"kind": "torrent"}, expect=400)


class TestMoney:
    async def test_tariff_preview_counts_like_the_bot(self, env: Env) -> None:
        await make_user(env, devices=1, bypass=1)
        res = await env.get("/api/tariff?devices=2&bypass=1")
        assert res["monthly"] == "160 ₽"
        months = {t["months"]: t["price"] for t in res["terms"]}
        assert months[1] == "160 ₽" and months[12] == "1440 ₽"

    async def test_buying_without_money_names_the_shortfall(self, env: Env) -> None:
        await make_user(env, balance=0)
        res = await env.post(
            "/api/tariff/buy", {"devices": 1, "bypass": 1, "months": 1}, expect=400
        )
        assert res["error"] == "no_money"
        assert "120" in res["message"]

    async def test_buying_extends_the_subscription(self, env: Env, monkeypatch) -> None:
        await make_user(env, balance=100_00, days=5)
        receipts = []

        class FakeBot:
            async def send_message(self, chat_id, text, *a, **kw):
                receipts.append((chat_id, text))

        monkeypatch.setattr("bot.loader.bot", FakeBot())
        res = await env.post(
            "/api/tariff/buy", {"devices": 1, "bypass": 0, "months": 1}
        )
        # Экран приложения закрывается без следа, а списание денег обязано
        # оставить след в переписке.
        assert receipts and receipts[0][0] == TG_ID
        assert "90" in receipts[0][1]
        assert "90" in res["message"]
        state = await env.get("/api/state")
        assert state["sub"]["days_left"] >= 34
        assert state["balance"]["text"] == "10 ₽"

    async def test_tariff_below_usage_is_refused(self, env: Env, monkeypatch) -> None:
        """Понижение при занятых устройствах было бы способом получить больше
        за меньше: лимит проверяется только при добавлении."""
        await make_user(env, devices=2, balance=10_000_00)
        await make_server(env)
        mute_provision(monkeypatch)
        await env.post("/api/devices", {"label": "A"})
        await env.post("/api/devices", {"label": "B"})
        res = await env.post(
            "/api/tariff/buy", {"devices": 1, "bypass": 0, "months": 1}, expect=400
        )
        assert res["error"] == "in_use"

    async def test_autopay_toggles(self, env: Env) -> None:
        await make_user(env)
        assert (await env.post("/api/autopay", {"on": False}))["autopay"] is False
        assert (await env.get("/api/state"))["sub"]["autopay"] is False

    async def test_deposit_amount_is_bounded(self, env: Env) -> None:
        await make_user(env)
        await env.post("/api/deposit", {"method": "stars", "rub": 1}, expect=400)
        await env.post("/api/deposit", {"method": "stars", "rub": 10 ** 9}, expect=400)

    async def test_card_is_off_without_keys(self, env: Env) -> None:
        await make_user(env)
        res = await env.post(
            "/api/deposit", {"method": "card", "rub": 100}, expect=400
        )
        assert res["error"] == "off"

    async def test_referral_link_is_personal(self, env: Env, monkeypatch) -> None:
        await make_user(env)

        class Me:
            username = "MoschataVPN_bot"

        class FakeBot:
            async def get_me(self):
                return Me()

        monkeypatch.setattr("bot.loader.bot", FakeBot())
        res = await env.get("/api/referral")
        assert res["link"].startswith("https://t.me/MoschataVPN_bot?start=ref_")
        assert res["percent"] == settings.referral_percent


class TestPage:
    async def test_page_is_served_with_locked_down_headers(self, env: Env) -> None:
        res = await env.client.get("/app/")
        assert res.status == 200
        csp = res.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "https://telegram.org" in csp
        assert res.headers["Cache-Control"] == "no-store"

    async def test_only_known_assets_are_served(self, env: Env) -> None:
        assert (await env.client.get("/app/app.js")).status == 200
        assert (await env.client.get("/app/app.css")).status == 200
        assert (await env.client.get("/app/../config.py")).status == 404
        assert (await env.client.get("/app/secret.txt")).status == 404


class TestBypass:
    """Резервное подключение выдаётся тем же сервисом, что и в боте, — здесь
    проверяем гейты приложения и то, что ссылка доезжает до человека."""

    @staticmethod
    def _mute(monkeypatch) -> list:
        made = []

        class FakeSSH:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        async def fake_create(ssh, *, days, label, vk_hashes, ports, binary):
            made.append({"days": days, "label": label, "vk": vk_hashes})
            return {"password": "PASS", "link": "wdtt://9.9.9.9:1:2:3:PASS:hx"}

        monkeypatch.setattr(bypass_issue, "SSHClient", lambda creds: FakeSSH())
        monkeypatch.setattr(bypass_issue.repo, "creds_from_server", lambda s: None)
        monkeypatch.setattr(bypass_issue.wdtt_svc, "create_access", fake_create)
        monkeypatch.setattr(settings, "wdtt_vk_hashes", "vk.com/call/svc")
        return made

    async def _server(self, env: Env) -> int:
        async with env.factory() as session:
            server = await repo.create_server(
                session, name="nl", host="1.1.1.1", wg_port=585, owner_tg_id=1,
                status=ServerStatus.READY, location="🇳🇱 Нидерланды",
                server_public_key="pub", server_endpoint="1.1.1.1:585",
                wdtt_enabled=True,
            )
            await session.commit()
            return server.id

    async def test_create_shows_link_and_appears_in_the_list(
        self, env: Env, monkeypatch
    ) -> None:
        made = self._mute(monkeypatch)
        await make_user(env, bypass=2)
        await self._server(env)

        before = await env.get("/api/bypass")
        assert before["can_add"] is True
        assert [loc["key"] for loc in before["locations"]] == ["🇳🇱 Нидерланды"]

        res = await env.post("/api/bypass", {
            "location": "🇳🇱 Нидерланды", "platform": "android", "device_id": None,
        })
        # Адрес в ссылке — из карточки сервера, а не тот, что назвал демон.
        assert res["link"].startswith("wdtt://1.1.1.1:")
        assert made[0]["vk"] == "vk.com/call/svc"

        after = await env.get("/api/bypass")
        assert after["used"] == 1
        assert after["items"][0]["link"].startswith("wdtt://1.1.1.1:")

    async def test_limit_is_enforced(self, env: Env, monkeypatch) -> None:
        self._mute(monkeypatch)
        await make_user(env, bypass=0)
        await self._server(env)
        res = await env.post(
            "/api/bypass", {"location": "🇳🇱 Нидерланды", "platform": "android"},
            expect=400,
        )
        assert res["error"] == "limit"

    async def test_own_vk_link_is_checked(self, env: Env, monkeypatch) -> None:
        self._mute(monkeypatch)
        await make_user(env, bypass=1)
        await self._server(env)
        res = await env.post("/api/bypass", {
            "location": "🇳🇱 Нидерланды", "platform": "android",
            "vk": "https://example.com/nonsense",
        }, expect=400)
        assert res["error"] == "bad_vk"

    async def test_unknown_platform_is_refused(self, env: Env, monkeypatch) -> None:
        self._mute(monkeypatch)
        await make_user(env, bypass=1)
        await self._server(env)
        await env.post(
            "/api/bypass", {"location": "🇳🇱 Нидерланды", "platform": "toaster"},
            expect=400,
        )

    async def test_delete_removes_it(self, env: Env, monkeypatch) -> None:
        self._mute(monkeypatch)
        monkeypatch.setattr(
            "bot.services.teardown._remove_bypass_on_server",
            lambda session, access: _true(),
        )
        await make_user(env, bypass=1)
        await self._server(env)
        made = await env.post(
            "/api/bypass", {"location": "🇳🇱 Нидерланды", "platform": "pc"}
        )
        await env.post(f"/api/bypass/{made['id']}/delete")
        assert (await env.get("/api/bypass"))["items"] == []


async def _true() -> bool:
    return True


class TestStarsDeposit:
    async def test_invoice_goes_to_the_chat(self, env: Env, monkeypatch) -> None:
        """Оплата звёздами живёт внутри Telegram: страница её открыть не может,
        поэтому счёт уезжает сообщением в чат с ботом."""
        await make_user(env)
        sent = []

        class FakeBot:
            async def send_invoice(self, chat_id, **kw):
                sent.append((chat_id, kw["currency"], kw["prices"][0].amount))

        monkeypatch.setattr("bot.loader.bot", FakeBot())
        res = await env.post("/api/deposit", {"method": "stars", "rub": 120})
        assert res["kind"] == "chat"
        chat_id, currency, stars = sent[0]
        assert chat_id == TG_ID and currency == "XTR"
        # Наценка за способ — из настроек, а не из кода страницы.
        assert stars == stars_for_kopeks(120 * 100)


class TestConsent:
    """Тот же гейт, что в боте: без принятых условий внутрь не пускаем, и
    записи в базе до согласия не заводим."""

    async def test_newcomer_is_sent_to_the_bot(self, env: Env, monkeypatch) -> None:
        monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/terms")
        res = await env.get("/api/state", expect=403)
        assert res["error"] == "consent"
        async with env.factory() as session:
            assert await repo.get_user_by_tg_id(session, TG_ID) is None

    async def test_accepted_terms_open_the_door(self, env: Env, monkeypatch) -> None:
        monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/terms")
        await make_user(env)
        async with env.factory() as session:
            user = await repo.get_user_by_tg_id(session, TG_ID)
            user.terms_accepted_at = datetime.now(timezone.utc)
            await session.commit()
        assert (await env.get("/api/state"))["ok"] is True


class TestTariffShopApi:
    """Витрина и цена «в месяц» приезжают с сервера — не считаются на странице.

    Вторая формула цены в javascript однажды разошлась бы с той, по которой
    списывают деньги, и спорить с юзером пришлось бы о его же экране.
    """

    async def test_state_carries_ready_made_tariffs(self, env: Env) -> None:
        await make_user(env)
        presets = (await env.get("/api/state"))["presets"]
        assert [p["key"] for p in presets] == [p.key for p in PRESETS]
        solo = next(p for p in presets if p["key"] == "solo")
        assert solo["monthly"] == fmt_rub(monthly_price_kopeks(1, 1))
        # Метка выгоды ровно одна и стоит на самом дешёвом за устройство.
        assert sum(1 for p in presets if p["best"]) == 1
        assert next(p for p in presets if p["best"])["key"] == best_value_key()

    async def test_terms_say_how_much_a_month_costs(self, env: Env) -> None:
        await make_user(env)
        terms = (await env.get("/api/tariff?devices=1&bypass=1"))["terms"]
        year = next(t for t in terms if t["months"] == 12)
        assert year["price"] == "1080 ₽"
        assert year["per_month"] == "90 ₽"
        # Ради этой цифры всё и делалось: год выгоднее месяца в рублях за месяц.
        month = next(t for t in terms if t["months"] == 1)
        assert month["per_month"] == month["price"]

    async def test_no_money_answer_carries_the_sum_to_top_up(self, env: Env) -> None:
        await make_user(env, balance=50_00)
        res = await env.post(
            "/api/tariff/buy", {"devices": 1, "bypass": 1, "months": 1}, expect=400
        )
        assert res["error"] == "no_money"
        # 120 − 50 = 70 ₽, округление вверх до десятки.
        assert res["missing_rub"] == 70
        assert res["missing"] == "70 ₽"
        assert res["price"] == "120 ₽"
