# Platega: приём карт, СБП и крипты — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** третья кнопка пополнения баланса — «💳 Карта или СБП» через Platega, с зачислением по факту оплаты (кнопка «Проверить» + поллинг планировщика).

**Architecture:** повторяем путь, которым уже ходит CryptoBot: клиент провайдера в `bot/services/`, своя таблица счетов, зачисление через общий `billing.credit_deposit` (реф-проценты, журнал и автопродление получаются даром), опрос статусов в существующей секции планировщика. Вебхуков нет — статус спрашиваем сами.

**Tech Stack:** aiogram 3.x, SQLAlchemy 2.0 async (SQLite), aiohttp, pytest/pytest-asyncio, loguru.

Спека: `docs/superpowers/specs/2026-08-11-platega-integraciya-design.md`.

## Global Constraints

- **Деньги — только в копейках (int).** Никаких float в балансе и журнале. В API Platega сумма уходит рублями (`amount: 10.5`), конвертация — в одном месте, в клиенте провайдера.
- **Движение денег — только через `repo.add_balance_tx`** (журнал `balance_txs`).
- **Зачисление — только через `billing.credit_deposit`** с методом `"platega"` (бонус 0, подпись «картой» уже заведены в `bot/services/pricing.py`).
- **Идемпотентность обязательна:** зачисляем, только если строка платежа ещё не `paid`.
- **Чужие транзакции не зачисляем:** юзер и сумма берутся из своей строки в базе, а не из ответа провайдера.
- **Формулировки:** проект под платёжным провайдером, есть страж текстов `tests/test_wording.py` — слова про обход блокировок в юзерских текстах запрещены. Экраны пополнения писать нейтрально («оплата картой или через СБП»).
- **PRAGMA foreign_keys НЕ включать** (на выключенных FK держится стирание юзера).
- **Время:** в БД UTC, юзеру — МСК через `bot/utils/timefmt.py`.
- **Эндпоинты Platega (проверены живыми запросами 11.08):**
  - создание: `POST https://app.platega.io/v2/transaction/process`, тело **без** `paymentMethod` → форма с выбором способа; ответ `{"transactionId": str, "status": "PENDING", "url": str, "expiresIn": "00:30:00"}`
  - статус: `GET https://app.platega.io/transaction/{id}`, ответ `{"id", "status", "paymentDetails": {"amount", "currency"}, ...}`
  - заголовки обоих: `X-MerchantId`, `X-Secret`
  - статусы: `PENDING` / `CONFIRMED` / `CANCELED` / `CHARGEBACKED`
  - счёт живёт 30 минут

---

### Task 1: Клиент Platega и настройки

**Files:**
- Create: `bot/services/platega.py`
- Modify: `bot/config.py` (после блока Crypto Pay, строки 55–58)
- Create: `tests/test_platega.py`

**Interfaces:**
- Consumes: `bot.config.settings`
- Produces:
  - `platega.enabled() -> bool`
  - `platega.PlategaError(Exception)`
  - `platega.INVOICE_TTL_MINUTES: int = 30`
  - `async platega.create_payment(amount_kopeks: int, *, description: str, payload: str, return_url: str) -> dict` → `{"transaction_id": str, "url": str}`
  - `async platega.get_status(transaction_id: str) -> str` → строка статуса провайдера (`PENDING`/`CONFIRMED`/`CANCELED`/`CHARGEBACKED`)
  - `platega.amount_to_rub(amount_kopeks: int) -> float`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_platega.py`:

```python
"""Тесты клиента Platega: конвертация сумм и выключенность без ключей."""
from __future__ import annotations

import pytest

from bot.services import platega


class TestAmountConversion:
    def test_whole_rubles(self) -> None:
        assert platega.amount_to_rub(300_00) == 300.0

    def test_kopeks_survive(self) -> None:
        """90.50 ₽ обязаны уехать как 90.5, а не как 90 или 9050."""
        assert platega.amount_to_rub(90_50) == 90.5

    def test_no_float_drift(self) -> None:
        """Копейки считаем целыми и делим один раз — накопленной ошибки быть не может."""
        assert platega.amount_to_rub(10_01) == 10.01
        assert platega.amount_to_rub(1_000_000_00) == 1_000_000.0


class TestEnabled:
    def test_disabled_without_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "")
        monkeypatch.setattr(platega.settings, "platega_secret", "")
        assert platega.enabled() is False

    def test_needs_both_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Один ключ без второго — это не настроенная платёжка, а опечатка в .env."""
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "mid")
        monkeypatch.setattr(platega.settings, "platega_secret", "")
        assert platega.enabled() is False

    def test_enabled_with_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platega.settings, "platega_merchant_id", "mid")
        monkeypatch.setattr(platega.settings, "platega_secret", "sec")
        assert platega.enabled() is True
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.services.platega'`

- [ ] **Step 3: Добавить настройки**

В `bot/config.py` после строки `cryptopay_token: str = ""` (с её комментарием) вставить:

```python
    # ── Платёжный провайдер Platega (карта/СБП/крипта одной формой) ─────────
    # ID мерчанта и API-ключ из личного кабинета platega.io («Настройки
    # проекта»). Пусто (любое из двух) = способ выключен: кнопки в пополнении
    # нет, поллинг не ходит. Ключи боевые с первого дня — песочницы у них нет.
    platega_merchant_id: str = ""
    platega_secret: str = ""
```

- [ ] **Step 4: Написать клиент**

Создать `bot/services/platega.py`:

```python
"""Клиент Platega (app.platega.io) — пополнение баланса картой, СБП и криптой.

Счёт создаём БЕЗ указания способа оплаты: провайдер отдаёт форму, где юзер сам
выбирает СБП, карту или крипту. Это умеет только v2-эндпоинт; у старого
`/transaction/process` способ оплаты обязателен.

Вебхуков нет: статус добираем поллингом планировщика и кнопкой «Проверить»
(как у Crypto Pay). Приём вебхуков требует домена с валидным сертификатом —
отдельная работа.

Дока: https://docs.platega.io/ (примеры там неполные — эндпоинты и формат
ответов проверены живыми запросами 11.08.2026, см. спеку).
"""
from __future__ import annotations

from typing import Any

import aiohttp
from loguru import logger

from bot.config import settings

_API_BASE = "https://app.platega.io"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Сколько живёт неоплаченный счёт на стороне Platega (минуты). Провайдер
# возвращает это в expiresIn ("00:30:00"); держим константой, чтобы текст на
# экране не зависел от разбора чужой строки.
INVOICE_TTL_MINUTES = 30


class PlategaError(Exception):
    """Ошибка Platega: сеть, таймаут или ответ с кодом ошибки."""


def enabled() -> bool:
    """Настроена ли платёжка. Нужны ОБА ключа: с одним запрос получит 401."""
    return bool(settings.platega_merchant_id and settings.platega_secret)


def amount_to_rub(amount_kopeks: int) -> float:
    """Копейки → рубли для тела запроса. Единственное место, где деньги
    становятся дробными: внутри бота они всегда целые копейки."""
    return amount_kopeks / 100


def _headers() -> dict[str, str]:
    return {
        "X-MerchantId": settings.platega_merchant_id,
        "X-Secret": settings.platega_secret,
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, payload: dict | None = None) -> Any:
    """Запрос к API. Не-2xx или не-JSON → PlategaError."""
    if not enabled():
        raise PlategaError("PLATEGA_MERCHANT_ID/PLATEGA_SECRET не заданы")
    url = f"{_API_BASE}{path}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.request(
                method, url, json=payload, headers=_headers()
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    # message/code — служебные поля провайдера, секретов там нет.
                    raise PlategaError(
                        f"Platega {resp.status}: {data.get('message') or data}"
                    )
    except PlategaError:
        raise
    except Exception as exc:  # сеть/таймаут/не-JSON
        raise PlategaError(f"Platega недоступна: {exc}") from exc
    return data


async def create_payment(
    amount_kopeks: int, *, description: str, payload: str, return_url: str
) -> dict:
    """Создаёт счёт на сумму в копейках. Возвращает {transaction_id, url}.

    Способ оплаты НЕ передаём — юзер выберет его на форме провайдера.
    """
    data = await _request(
        "POST",
        "/v2/transaction/process",
        {
            "paymentDetails": {
                "amount": amount_to_rub(amount_kopeks),
                "currency": "RUB",
            },
            "description": description,
            "return": return_url,
            "failedUrl": return_url,
            "payload": payload,
        },
    )
    tx_id, url = data.get("transactionId"), data.get("url")
    if not tx_id or not url:
        raise PlategaError(f"Platega вернула ответ без счёта: {data}")
    logger.info("Platega payment {} created ({} kopeks)", tx_id, amount_kopeks)
    return {"transaction_id": str(tx_id), "url": str(url)}


async def get_status(transaction_id: str) -> str:
    """Статус счёта: PENDING | CONFIRMED | CANCELED | CHARGEBACKED."""
    data = await _request("GET", f"/transaction/{transaction_id}")
    status = data.get("status")
    if not status:
        raise PlategaError(f"Platega вернула ответ без статуса: {data}")
    return str(status)
```

- [ ] **Step 5: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v`
Expected: PASS (6 тестов)

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/platega.py bot/config.py tests/test_platega.py
git commit -m "Platega: клиент провайдера и настройки"
```

---

### Task 2: Таблица платежей и доступ к ней

**Files:**
- Modify: `bot/db/models.py` (после класса `CryptoInvoice`, строки 440–462)
- Modify: `bot/db/repo/billing.py`
- Modify: `bot/db/repo/__init__.py` (импорт и `__all__`)
- Modify: `tests/test_platega.py`

**Interfaces:**
- Consumes: `bot.db.models.Base`, `repo.add_balance_tx`
- Produces:
  - модель `PlategaPayment` (таблица `platega_payments`): `id: int`, `user_id: int`, `transaction_id: str` (unique), `amount_kopeks: int`, `status: str` (`pending`/`paid`/`canceled`), `url: str`, `created_at`, `paid_at`
  - `async repo.create_platega_payment(session, *, user_id: int, transaction_id: str, amount_kopeks: int, url: str) -> PlategaPayment`
  - `async repo.get_platega_payment(session, row_id: int) -> PlategaPayment | None`
  - `async repo.list_open_platega_payments(session, *, max_age_hours: int = 24) -> list[PlategaPayment]`

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_platega.py`:

```python
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo


async def _user(session: AsyncSession, tg_id: int = 501):
    return await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )


class TestPaymentRows:
    @pytest.mark.asyncio
    async def test_created_row_is_pending(self, session: AsyncSession) -> None:
        user = await _user(session)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-1",
            amount_kopeks=300_00, url="https://pay.platega.io?id=tx-1",
        )
        assert row.status == "pending"
        assert row.paid_at is None
        assert (await repo.get_platega_payment(session, row.id)).transaction_id == "tx-1"

    @pytest.mark.asyncio
    async def test_only_pending_are_polled(self, session: AsyncSession) -> None:
        """Оплаченные и отменённые счета опрашивать незачем — они финальны."""
        user = await _user(session)
        pending = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-open",
            amount_kopeks=100_00, url="u",
        )
        paid = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-paid",
            amount_kopeks=100_00, url="u",
        )
        paid.status = "paid"
        await session.flush()
        open_rows = await repo.list_open_platega_payments(session)
        assert [r.id for r in open_rows] == [pending.id]

    @pytest.mark.asyncio
    async def test_stale_rows_are_dropped(self, session: AsyncSession) -> None:
        """Счёт живёт 30 минут: вчерашние строки провайдер уже отменил сам,
        и гонять по ним запросы вечно не нужно."""
        from datetime import datetime, timedelta, timezone

        user = await _user(session)
        old = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-old",
            amount_kopeks=100_00, url="u",
        )
        old.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
        await session.flush()
        assert await repo.list_open_platega_payments(session) == []
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v -k PaymentRows`
Expected: FAIL — `AttributeError: module 'bot.db.repo' has no attribute 'create_platega_payment'`

- [ ] **Step 3: Добавить модель**

В `bot/db/models.py` сразу после класса `CryptoInvoice` (перед `class StarPayment`) вставить:

```python
class PlategaPayment(Base):
    """Счёт Platega на пополнение баланса (карта/СБП/крипта одной формой).

    Отдельно от CryptoInvoice: там id счёта числовой и свой набор статусов, а
    здесь id — UUID провайдера. Статус меняется поллингом планировщика или
    кнопкой «Проверить»; зачисление — строго через billing.apply_paid_platega
    (идемпотентно).

    Юзер и сумма берутся ИЗ ЭТОЙ строки, а не из ответа провайдера: по чужому
    id их API отдаёт чужие транзакции, и доверять ответу как источнику правды
    нельзя.
    """

    __tablename__ = "platega_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # UUID транзакции на стороне Platega.
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    amount_kopeks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(  # pending | paid | canceled
        String(16), default="pending", server_default="'pending'", nullable=False
    )
    url: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 4: Добавить функции доступа**

В `bot/db/repo/billing.py`: в импорт моделей добавить `PlategaPayment`
(строка `from bot.db.models import BalanceTx, CryptoInvoice, User` →
`from bot.db.models import BalanceTx, CryptoInvoice, PlategaPayment, User`),
затем дописать после `list_open_invoices`:

```python
async def create_platega_payment(
    session: AsyncSession, *, user_id: int, transaction_id: str,
    amount_kopeks: int, url: str,
) -> PlategaPayment:
    row = PlategaPayment(
        user_id=user_id, transaction_id=transaction_id,
        amount_kopeks=amount_kopeks, url=url,
    )
    session.add(row)
    await session.flush()
    return row


async def get_platega_payment(
    session: AsyncSession, row_id: int
) -> PlategaPayment | None:
    return await session.get(PlategaPayment, row_id)


async def list_open_platega_payments(
    session: AsyncSession, *, max_age_hours: int = 24
) -> list[PlategaPayment]:
    """Неоплаченные счета для поллинга планировщиком. Счёт Platega живёт 30
    минут, поэтому суток с запасом хватает: всё старше провайдер уже отменил."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return list((await session.execute(
        select(PlategaPayment)
        .where(PlategaPayment.status == "pending")
        .where(PlategaPayment.created_at >= cutoff)
    )).scalars())
```

- [ ] **Step 5: Экспортировать из repo**

В `bot/db/repo/__init__.py` в блок импорта из `.billing` добавить три имени
(`create_platega_payment`, `get_platega_payment`, `list_open_platega_payments`)
и те же три строки — в `__all__`, сохраняя алфавитный порядок соседей.

- [ ] **Step 6: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py tests/test_migrate.py -v`
Expected: PASS

- [ ] **Step 7: Коммит**

```bash
cd /root/myvpn-bot
git add bot/db/models.py bot/db/repo/billing.py bot/db/repo/__init__.py tests/test_platega.py
git commit -m "Platega: таблица счетов и доступ к ней"
```

---

### Task 3: Зачисление оплаченного счёта

**Files:**
- Modify: `bot/services/billing.py` (после `apply_paid_invoice`, строки 110–124)
- Modify: `tests/test_platega.py`

**Interfaces:**
- Consumes: `billing.credit_deposit`, `models.PlategaPayment`
- Produces: `async billing.apply_paid_platega(session, row: PlategaPayment) -> billing.DepositResult`

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_platega.py`:

```python
class TestCrediting:
    @pytest.mark.asyncio
    async def test_payment_credits_balance(self, session: AsyncSession) -> None:
        from bot.services import billing

        user = await _user(session, tg_id=601)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-pay",
            amount_kopeks=300_00, url="u",
        )
        dep = await billing.apply_paid_platega(session, row)
        await session.refresh(user)
        assert dep.credited is True
        assert user.balance_kopeks == 300_00
        assert row.status == "paid"
        assert row.paid_at is not None

    @pytest.mark.asyncio
    async def test_no_bonus_for_card(self, session: AsyncSession) -> None:
        """Карта и СБП — самый дорогой для сервиса способ, бонуса за него нет
        (решение 8.08). Зачисляем ровно сумму счёта."""
        from bot.services import billing

        user = await _user(session, tg_id=602)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-nobonus",
            amount_kopeks=100_00, url="u",
        )
        await billing.apply_paid_platega(session, row)
        await session.refresh(user)
        assert user.balance_kopeks == 100_00

    @pytest.mark.asyncio
    async def test_double_credit_impossible(self, session: AsyncSession) -> None:
        """Кнопка «Проверить» и тик планировщика могут увидеть оплату
        одновременно — баланс обязан вырасти один раз."""
        from bot.services import billing

        user = await _user(session, tg_id=603)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-twice",
            amount_kopeks=250_00, url="u",
        )
        first = await billing.apply_paid_platega(session, row)
        second = await billing.apply_paid_platega(session, row)
        await session.refresh(user)
        assert first.credited is True
        assert second.credited is False
        assert user.balance_kopeks == 250_00

    @pytest.mark.asyncio
    async def test_referrer_gets_percent(self, session: AsyncSession) -> None:
        from bot.config import settings
        from bot.services import billing

        inviter = await _user(session, tg_id=604)
        buyer = await _user(session, tg_id=605)
        buyer.referrer_id = inviter.id
        await session.flush()
        row = await repo.create_platega_payment(
            session, user_id=buyer.id, transaction_id="tx-ref",
            amount_kopeks=1000_00, url="u",
        )
        dep = await billing.apply_paid_platega(session, row)
        await session.refresh(inviter)
        assert dep.ref_reward_kopeks == 1000_00 * settings.referral_percent // 100
        assert inviter.balance_kopeks == dep.ref_reward_kopeks
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v -k Crediting`
Expected: FAIL — `AttributeError: module 'bot.services.billing' has no attribute 'apply_paid_platega'`

- [ ] **Step 3: Реализовать зачисление**

В `bot/services/billing.py` в импорт моделей добавить `PlategaPayment`
(строка `from bot.db.models import AuditAction, CryptoInvoice, User` →
`from bot.db.models import AuditAction, CryptoInvoice, PlategaPayment, User`),
затем сразу после `apply_paid_invoice` вставить:

```python
async def apply_paid_platega(
    session: AsyncSession, row: PlategaPayment
) -> DepositResult:
    """Зачисляет ОПЛАЧЕННЫЙ счёт Platega: баланс юзеру + реф-награда пригласившему.

    Идемпотентно: повторный вызов по уже paid-строке — no-op (кнопка «Проверить»
    и поллинг планировщика могут наперегонки увидеть одну оплату).

    Юзер и сумма берутся из строки, а не из ответа провайдера: их API по id
    отдаёт и чужие транзакции, доверять ему как источнику правды нельзя."""
    if row.status == "paid":
        return DepositResult(credited=False)
    row.status = "paid"
    row.paid_at = datetime.now(timezone.utc)
    return await credit_deposit(
        session, user_id=row.user_id, amount_kopeks=row.amount_kopeks,
        method="platega", note=f"Пополнение картой (счёт {row.transaction_id})",
    )
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py tests/test_billing.py -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/billing.py tests/test_platega.py
git commit -m "Platega: зачисление оплаченного счёта"
```

---

### Task 4: Экраны пополнения

**Files:**
- Modify: `bot/keyboards/inline/balance.py` (`deposit_methods_kb`, строки 31–48; новые `platega_amounts_kb`, `platega_invoice_kb`)
- Modify: `bot/keyboards/inline/__init__.py` (экспорт двух новых клавиатур)
- Modify: `bot/handlers/balance.py` (экран выбора способа, строки 147–168; новые хендлеры)
- Modify: `tests/test_platega.py`

**Interfaces:**
- Consumes: `platega.create_payment`, `platega.get_status`, `platega.INVOICE_TTL_MINUTES`, `billing.apply_paid_platega`, `repo.create_platega_payment`, `repo.get_platega_payment`
- Produces:
  - `deposit_methods_kb(bonus_percent: int, cryptobot: bool = True, platega: bool = True) -> InlineKeyboardMarkup`
  - `platega_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup`
  - `platega_invoice_kb(pay_url: str, row_id: int) -> InlineKeyboardMarkup`
  - callback-данные: `bal:dep:pg` (экран сумм), `bal:pg:<рубли>`, `bal:pg:custom`, `bal:pgchk:<row_id>`

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_platega.py`:

```python
class TestScreens:
    def test_method_button_present(self) -> None:
        """Кнопка карты/СБП есть на экране выбора способа."""
        from bot.keyboards.inline import deposit_methods_kb

        kb = deposit_methods_kb(4, cryptobot=True, platega=True)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Карта" in t for t in texts)

    def test_method_button_hidden_without_keys(self) -> None:
        """Ключей нет — кнопки нет: счёт всё равно не создать."""
        from bot.keyboards.inline import deposit_methods_kb

        kb = deposit_methods_kb(4, cryptobot=True, platega=False)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("Карта" in t for t in texts)

    def test_amounts_keyboard_routes_to_platega(self) -> None:
        from bot.keyboards.inline import platega_amounts_kb

        kb = platega_amounts_kb([(90, "90 ₽ — месяц"), (240, "240 ₽ — 3 мес")])
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "bal:pg:90" in data
        assert "bal:pg:custom" in data

    def test_invoice_keyboard_has_pay_and_check(self) -> None:
        from bot.keyboards.inline import platega_invoice_kb

        kb = platega_invoice_kb("https://pay.platega.io?id=x", 7)
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert any(b.url == "https://pay.platega.io?id=x" for b in buttons)
        assert any(b.callback_data == "bal:pgchk:7" for b in buttons)

    def test_ttl_named_in_minutes(self) -> None:
        """На экране счёта обязан стоять реальный срок жизни (30 минут), иначе
        юзер уйдёт пить чай и вернётся к мёртвому счёту."""
        from bot.services import platega

        assert platega.INVOICE_TTL_MINUTES == 30
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v -k Screens`
Expected: FAIL — `ImportError: cannot import name 'platega_amounts_kb'`

- [ ] **Step 3: Добавить клавиатуры**

В `bot/keyboards/inline/balance.py` заменить `deposit_methods_kb` на:

```python
def deposit_methods_kb(
    bonus_percent: int, cryptobot: bool = True, platega: bool = True
) -> InlineKeyboardMarkup:
    """Выбор способа пополнения (этап D). Бонус за способ — прямо на кнопке:
    выбирают из строк, а не из абзаца текста над ними.

    cryptobot=False / platega=False — ключи способа не настроены; звёздам
    настройка не нужна, поэтому пополнение живо даже без обоих."""
    kb = InlineKeyboardBuilder()
    if platega:
        kb.button(text="💳 Карта или СБП", callback_data=f"{CB_BAL}:dep:pg",
                  style="success")
    if cryptobot:
        kb.button(
            text=f"💎 CryptoBot  +{bonus_percent}%",
            callback_data=f"{CB_BAL}:dep:cb", style="success",
        )
    kb.button(text="⭐ Звёзды Telegram", callback_data=f"{CB_BAL}:dep:stars")
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()
```

и дописать после `star_amounts_kb`:

```python
def platega_amounts_kb(amounts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Суммы для оплаты картой/СБП — те же, что у остальных способов: юзер не
    должен видеть разный набор сумм в зависимости от кошелька."""
    kb = InlineKeyboardBuilder()
    for rub, label in amounts:
        kb.button(text=label, callback_data=f"{CB_BAL}:pg:{rub}")
    kb.button(text="✏️ Своя сумма", callback_data=f"{CB_BAL}:pg:custom")
    kb.button(text="« Назад", callback_data=f"{CB_BAL}:dep")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def platega_invoice_kb(pay_url: str, row_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", url=pay_url, style="success")
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"{CB_BAL}:pgchk:{row_id}")
    kb.button(text="« К балансу", callback_data=f"{CB_BAL}:my")
    kb.adjust(1)
    return kb.as_markup()
```

В `bot/keyboards/inline/__init__.py` добавить `platega_amounts_kb` и
`platega_invoice_kb` в импорт из `.balance` и в `__all__`.

- [ ] **Step 4: Добавить хендлеры**

В `bot/handlers/balance.py`:

1. В импорт клавиатур (строки 25–36) добавить `platega_amounts_kb`, `platega_invoice_kb`.
2. В импорт сервисов (строка 38) — `platega`: `from bot.services import billing, cryptopay, platega`.
3. В `cb_bal_deposit` (строка ~152) дописать в текст описание способа и передать флаг:

```python
    await call.message.edit_text(
        "➕ <b>Пополнение баланса</b>\n\n"
        "💳 <b>Карта или СБП</b> — оплата рублями с карты или по QR через "
        "приложение банка. Зачислим ровно ту сумму, которую выберешь.\n\n"
        f"💎 <b>@CryptoBot</b> — оплата в рублях, крипту можно купить с карты "
        f"прямо там. Начислим <b>+{DEPOSIT_BONUS_PERCENT['cryptobot']}%</b> "
        "сверху.\n\n"
        f"⭐ <b>Звёзды Telegram</b> — оплата в два касания, не выходя из "
        f"Telegram. Дороже на {settings.star_markup_percent}%: звёзды доходят "
        "до нас через вывод с комиссиями и трёхнедельной задержкой, наценка "
        "это и покрывает.\n"
        "<i>Отдельно: Apple и Google берут свою долю при покупке самих звёзд "
        "в приложении — это не наша комиссия, мы её не получаем. Дешевле "
        "покупать звёзды не через приложение.</i>",
        reply_markup=deposit_methods_kb(
            DEPOSIT_BONUS_PERCENT["cryptobot"],
            cryptobot=cryptopay.enabled(),
            platega=platega.enabled(),
        ),
    )
```

4. Дописать после `cb_bal_deposit_stars` (строка ~205):

```python
@router.callback_query(F.data == f"{CB_BAL}:dep:pg")
async def cb_bal_deposit_platega(call: CallbackQuery) -> None:
    if not platega.enabled():
        await call.answer("Этот способ временно недоступен.", show_alert=True)
        return
    await call.message.edit_text(
        "💳 <b>Оплата картой или через СБП</b>\n\n"
        "Открой ссылку, выбери удобный способ — банковская карта, СБП по QR "
        "или криптовалюта — и оплати. На баланс придёт ровно та сумма, "
        "которую выберешь: комиссию платим мы.\n\n"
        "Выбери сумму:\n"
        "<i>Суммы на кнопках — стоимость базового тарифа (1 устройство + "
        "1 резервное подключение) на месяц, 3 месяца, полгода и год.</i>",
        reply_markup=platega_amounts_kb(_deposit_amounts()),
    )
    await call.answer()


@router.callback_query(F.data == f"{CB_BAL}:pg:custom")
async def cb_bal_platega_custom(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_custom_amount(call, state, method="platega")


@router.callback_query(F.data.startswith(f"{CB_BAL}:pg:"))
async def cb_bal_platega_amount(call: CallbackQuery, session: AsyncSession) -> None:
    # Сюда падают только "pg:<число>" — pg:custom перехвачен выше.
    raw = call.data.rsplit(":", 1)[-1]
    # callback_data приходит от клиента и может быть подделана — держим сумму
    # в тех же рамках, что и ручной ввод.
    if not raw.isdigit() or not (_CUSTOM_MIN_RUB <= int(raw) <= _CUSTOM_MAX_RUB):
        await call.answer("Некорректная сумма.", show_alert=True)
        return
    user = await _get_user(session, call)
    await _create_and_show_platega(call.message.edit_text, session, user, int(raw) * 100)
    await call.answer()


async def _create_and_show_platega(
    send, session: AsyncSession, user, amount_kopeks: int
) -> None:
    """Создаёт счёт Platega и показывает его юзеру.

    Ссылку и id счёта сохраняем ДО показа: если юзер оплатит, а строки не
    окажется, зачислять будет нечего — а деньги провайдер уже возьмёт."""
    bot_username = await _get_bot_username()
    try:
        pay = await platega.create_payment(
            amount_kopeks,
            description=f"Пополнение баланса VPN на {fmt_rub(amount_kopeks)}",
            payload=f"user:{user.id}",
            return_url=f"https://t.me/{bot_username}",
        )
    except platega.PlategaError as exc:
        logger.warning("Platega create_payment failed: {}", exc)
        await send(
            "❌ Не получилось создать счёт — попробуй позже или выбери другой "
            "способ пополнения.",
            reply_markup=balance_kb(True),
        )
        return
    row = await repo.create_platega_payment(
        session, user_id=user.id, transaction_id=pay["transaction_id"],
        amount_kopeks=amount_kopeks, url=pay["url"],
    )
    await session.commit()
    await send(
        f"💳 Счёт на <b>{fmt_rub(amount_kopeks)}</b> создан "
        f"(действует {platega.INVOICE_TTL_MINUTES} минут).\n\n"
        "Нажми «Перейти к оплате», выбери способ и оплати. Потом вернись сюда "
        "и жми «Я оплатил» — обычно баланс зачисляется за пару секунд. Если "
        "закроешь экран — не страшно, бот сам увидит оплату в течение "
        "~5 минут.",
        reply_markup=platega_invoice_kb(pay["url"], row.id),
    )


@router.callback_query(F.data.startswith(f"{CB_BAL}:pgchk:"))
async def cb_bal_platega_check(call: CallbackQuery, session: AsyncSession) -> None:
    row_id = int(call.data.rsplit(":", 1)[-1])
    row = await repo.get_platega_payment(session, row_id)
    user = await _get_user(session, call)
    if row is None or row.user_id != user.id:
        await call.answer("Счёт не найден", show_alert=True)
        return
    if row.status == "paid":
        await _render_balance(call.message.edit_text, session, user)
        await call.answer("Уже зачислено ✅")
        return
    try:
        status = await platega.get_status(row.transaction_id)
    except platega.PlategaError as exc:
        logger.warning("Platega check failed: {}", exc)
        await call.answer("Платёжка не отвечает, попробуй чуть позже.", show_alert=True)
        return
    if status == "CONFIRMED":
        dep = await billing.apply_paid_platega(session, row)
        await session.commit()
        await notify_deposit(dep)
        await session.refresh(user)
        # Подписка уже истекла, автопродление включено? Продлеваем сразу на
        # свежие деньги — не заставляем ждать тика планировщика.
        ap = await billing.autopay_if_expired(session, user)
        if ap is not None:
            await session.commit()
            await notify_autopay(user, ap)
            await session.refresh(user)
        await _render_balance(call.message.edit_text, session, user)
        await call.answer("Зачислено ✅")
        return
    if status in ("CANCELED", "CHARGEBACKED"):
        row.status = "canceled"
        await session.commit()
        await call.message.edit_text(
            f"⌛ Счёт больше не действует (он живёт "
            f"{platega.INVOICE_TTL_MINUTES} минут). Создай новый.",
            reply_markup=balance_kb(True),
        )
        await call.answer()
        return
    await call.answer(
        "Оплата пока не видна. Если платёж уже отправлен — подожди пару секунд "
        "и жми ещё раз.",
        show_alert=True,
    )
```

5. В `step_bal_custom_amount` (строка ~243) добавить ветку способа перед CryptoBot:

```python
    if method == "platega":
        await _create_and_show_platega(message.answer, session, user, int(raw) * 100)
        return
```

- [ ] **Step 5: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py tests/test_wording.py tests/test_pricing_screens.py tests/test_no_undefined_names.py -v`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add bot/keyboards/inline/balance.py bot/keyboards/inline/__init__.py bot/handlers/balance.py tests/test_platega.py
git commit -m "Platega: экраны пополнения и проверка оплаты"
```

---

### Task 5: Поллинг счетов планировщиком

**Files:**
- Modify: `bot/services/scheduler.py` (рядом с `_poll_crypto_invoices`, строки 70–95 и вызов в секции 0, строка ~165)
- Modify: `tests/test_platega.py`

**Interfaces:**
- Consumes: `platega.get_status`, `billing.apply_paid_platega`, `repo.list_open_platega_payments`
- Produces: `async scheduler._poll_platega_payments(session) -> None`

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_platega.py`:

```python
class TestPolling:
    @pytest.mark.asyncio
    async def test_confirmed_is_credited(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Юзер закрыл экран, не нажав «Проверить» — деньги обязан найти бот."""
        from bot.services import platega, scheduler

        user = await _user(session, tg_id=701)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-poll",
            amount_kopeks=500_00, url="u",
        )
        await session.commit()

        async def fake_status(transaction_id: str) -> str:
            assert transaction_id == "tx-poll"
            return "CONFIRMED"

        monkeypatch.setattr(platega, "enabled", lambda: True)
        monkeypatch.setattr(platega, "get_status", fake_status)
        monkeypatch.setattr(
            "bot.handlers.balance.notify_deposit", _noop_notify, raising=False
        )
        await scheduler._poll_platega_payments(session)
        await session.refresh(user)
        await session.refresh(row)
        assert row.status == "paid"
        assert user.balance_kopeks == 500_00

    @pytest.mark.asyncio
    async def test_canceled_is_closed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bot.services import platega, scheduler

        user = await _user(session, tg_id=702)
        row = await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-dead",
            amount_kopeks=100_00, url="u",
        )
        await session.commit()

        async def fake_status(transaction_id: str) -> str:
            return "CANCELED"

        monkeypatch.setattr(platega, "enabled", lambda: True)
        monkeypatch.setattr(platega, "get_status", fake_status)
        await scheduler._poll_platega_payments(session)
        await session.refresh(row)
        await session.refresh(user)
        assert row.status == "canceled"
        assert user.balance_kopeks == 0

    @pytest.mark.asyncio
    async def test_api_failure_does_not_break_tick(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Платёжка легла — тик планировщика обязан пережить это молча."""
        from bot.services import platega, scheduler

        user = await _user(session, tg_id=703)
        await repo.create_platega_payment(
            session, user_id=user.id, transaction_id="tx-boom",
            amount_kopeks=100_00, url="u",
        )
        await session.commit()

        async def boom(transaction_id: str) -> str:
            raise platega.PlategaError("сеть легла")

        monkeypatch.setattr(platega, "enabled", lambda: True)
        monkeypatch.setattr(platega, "get_status", boom)
        await scheduler._poll_platega_payments(session)  # не должно бросить
```

Там же, выше класса, добавить заглушку уведомления:

```python
async def _noop_notify(dep) -> None:
    """Уведомления в Telegram в тестах не шлём — бот не поднят."""
    return None
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v -k Polling`
Expected: FAIL — `AttributeError: module 'bot.services.scheduler' has no attribute '_poll_platega_payments'`

- [ ] **Step 3: Реализовать поллинг**

В `bot/services/scheduler.py` сразу после `_poll_crypto_invoices` вставить:

```python
async def _poll_platega_payments(session) -> None:
    """Сверяет неоплаченные счета Platega: CONFIRMED → зачислить (идемпотентно),
    CANCELED/CHARGEBACKED → закрыть. Ошибки API не валят тик.

    Статусы спрашиваем по одному: пакетного запроса у провайдера нет, а счетов
    в работе одновременно единицы (счёт живёт 30 минут)."""
    from bot.services import billing, platega

    if not platega.enabled():
        return
    rows = await repo.list_open_platega_payments(session)
    if not rows:
        return
    changed = False
    for row in rows:
        try:
            status = await platega.get_status(row.transaction_id)
        except platega.PlategaError as exc:
            logger.warning("Platega poll failed for {}: {}", row.transaction_id, exc)
            continue
        if status == "CONFIRMED":
            dep = await billing.apply_paid_platega(session, row)
            changed = True
            from bot.handlers.balance import notify_deposit
            await notify_deposit(dep)
        elif status == "CANCELED":
            row.status = "canceled"
            changed = True
        elif status == "CHARGEBACKED":
            # Деньги вернули плательщику. Баланс НЕ трогаем: юзер мог их уже
            # потратить, и автоматический минус сделает хуже. Разбирает админ.
            row.status = "canceled"
            changed = True
            logger.error(
                "Platega chargeback: payment {} (user {}, {} kopeks)",
                row.transaction_id, row.user_id, row.amount_kopeks,
            )
            await _alert_admins_chargeback(row)
    if changed:
        await session.commit()


async def _alert_admins_chargeback(row) -> None:
    """Возврат по платежу — админам в личку. Ошибки Telegram глотаем: тик
    планировщика важнее доставки одного сообщения."""
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
```

Затем в секции 0 тика, сразу после `await _poll_crypto_invoices(session)` (строка ~165), добавить вызов в своём try, чтобы падение одного провайдера не мешало другому:

```python
            try:
                await _poll_platega_payments(session)
            except Exception:
                logger.exception("Scheduler section 0 (platega poll) failed")
```

Если `settings` в модуле ещё не импортирован — добавить `from bot.config import settings` к импортам файла.

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_platega.py -v`
Expected: PASS

- [ ] **Step 5: Прогнать весь набор**

Run: `cd /root/myvpn-bot && python -m pytest -q`
Expected: PASS, падений нет

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/scheduler.py tests/test_platega.py
git commit -m "Platega: поллинг счетов и тревога о возврате"
```

---

### Task 6: Документация и выкат

**Files:**
- Modify: `README.md` (пункт «Баланс, оплата и рефералка»)
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-08-11-platega-integraciya-design.md` (отметка о выполнении)

- [ ] **Step 1: Дописать README**

В пункте «**Баланс, оплата и рефералка**» после слов про два способа добавить
третий: «карта/СБП/крипта через Platega (`PLATEGA_MERCHANT_ID` и
`PLATEGA_SECRET` в `.env`, пусто = способ выключен; счёт живёт 30 минут, статус
добирается кнопкой «Проверить» и поллингом планировщика — вебхуков нет)» и
поправить «двумя способами» на «тремя способами».

- [ ] **Step 2: Дописать .env.example**

```bash
cd /root/myvpn-bot
cat >> .env.example <<'EOF'

# Платёжный провайдер Platega (карта/СБП/крипта). Ключи — в личном кабинете
# platega.io, раздел «Настройки проекта». Пусто = способ выключен.
PLATEGA_MERCHANT_ID=
PLATEGA_SECRET=
EOF
```

- [ ] **Step 3: Коммит и пуш**

```bash
cd /root/myvpn-bot
git add README.md .env.example docs/superpowers/specs/2026-08-11-platega-integraciya-design.md
git commit -m "Platega: README и пример настроек"
git push
```

- [ ] **Step 4: Выкатить на сервер**

Ключи в `.env` бота на klopas (значения — из личного кабинета, в репозиторий не попадают):

```bash
ssh klopas 'cd /root/myvpn-bot && git pull --ff-only'
ssh klopas 'grep -q "^PLATEGA_MERCHANT_ID=" /root/myvpn-bot/.env || printf "\nPLATEGA_MERCHANT_ID=91780dec-7c7f-4d65-9263-838adce3d414\nPLATEGA_SECRET=<ключ из ЛК>\n" >> /root/myvpn-bot/.env'
ssh klopas 'systemctl restart myvpn-bot && sleep 5 && systemctl is-active myvpn-bot'
```

- [ ] **Step 5: Проверить логи**

```bash
ssh klopas 'journalctl -u myvpn-bot -n 40 --no-pager'
```

Expected: старт без ошибок, миграция таблицы `platega_payments` прошла, в логе
нет `PlategaError`.

- [ ] **Step 6: Живая проверка**

В боте: «💰 Баланс» → «➕ Пополнить» → «💳 Карта или СБП» → сумма → открыть
ссылку. Убедиться, что на форме три способа и сумма совпадает с названной ботом
(если больше — комиссия в кабинете всё ещё на клиенте). Оплатить минимальную
сумму, нажать «Я оплатил» и увидеть зачисление; проверить строку в «📜 История».

---

## Self-Review

**Покрытие спеки.** Клиент и настройки — задача 1. Хранение счетов — 2.
Зачисление, идемпотентность, рефералка — 3. Экран пополнения, кнопка, суммы,
своя сумма, проверка оплаты, срок 30 минут — 4. Поллинг, чарджбэк-тревога — 5.
README, `.env`, выкат, живая проверка — 6. Вебхуки, подписки, возвраты по API и
баланс шлюза в админке спека выносит за границы работы — задач под них нет
осознанно.

**Заглушек нет:** весь код приведён целиком, тесты — с телами, команды — с
точными путями. Единственное подставляемое значение — секретный ключ в `.env` на
сервере, он не хранится в репозитории.

**Согласованность имён:** `create_platega_payment` / `get_platega_payment` /
`list_open_platega_payments` (задача 2) используются в задачах 4–5 в том же
написании; `apply_paid_platega` (3) — в 4 и 5; `platega_amounts_kb` /
`platega_invoice_kb` (4) — в тестах той же задачи; `INVOICE_TTL_MINUTES` (1) —
в 4; статусы строки (`pending`/`paid`/`canceled`) одинаковы везде.
