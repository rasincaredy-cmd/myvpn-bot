# Журнал действий — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ: используйте
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans, чтобы выполнять план задача за задачей.
> Шаги размечены чекбоксами (`- [ ]`).

**Цель:** записывать каждое важное действие (деньги, доступ, админские
операции, падения серверов) в базу и показывать это админу лентой в панели
и историей в карточке юзера.

**Архитектура:** новая таблица `audit_logs` + тонкий репозиторий-обёртка
`bot/db/repo/audit.py`. Запись идёт из тех же функций, где событие уже
происходит, — рядом с существующими `logger.info`, в той же транзакции, что и
само действие. Отдельный модуль-экран `bot/handlers/admin/audit.py` рисует
ленту и историю. Планировщик чистит старые записи в своей секции.

**Стек:** Python 3, aiogram 3, SQLAlchemy 2 (async), SQLite, loguru, pytest
(asyncio_mode=auto).

## Глобальные ограничения

- Все денежные суммы — **в копейках**, `int`. Никаких `float` у денег.
- Всё, что видит человек, — **московское время**, через `bot.utils.timefmt.fmt_msk`.
- **PRAGMA foreign_keys НЕ включать** — на выключенных внешних ключах держится
  стирание юзера (`user_wipe` намеренно оставляет REVOKED-строки).
- Миграции добавляют только **nullable-колонки или колонки с DEFAULT** —
  `ADD COLUMN NOT NULL` без DEFAULT на непустой таблице невозможен.
- Комментарии, докстринги и тексты для пользователя — **на русском**.
- Файл, переросший ~500 строк, дробится на пакет — так уже сделано с
  `repo.py`, `menu.py`, `admin_panel.py`, `inline.py`.
- Тесты гоняются командой `python -m pytest` из корня репозитория.
  В окружении Termux используется питон, у которого есть `cryptography`.

---

## Структура файлов

- **Создать** `bot/db/repo/audit.py` — запись и выборка записей журнала.
- **Создать** `bot/handlers/admin/audit.py` — экран «Журнал» и «История юзера».
- **Создать** `tests/test_audit.py` — тесты репозитория и ретеншна.
- **Изменить** `bot/db/models.py` — модель `AuditLog` и перечисление `AuditAction`.
- **Изменить** `bot/db/repo/__init__.py` — экспорт новых функций.
- **Изменить** `bot/keyboards/inline/admin.py` — кнопки «Журнал», «История»,
  пагинация ленты.
- **Изменить** `bot/handlers/admin/__init__.py` — подключить роутер `audit`.
- **Изменить** `bot/services/billing.py` — запись денежных событий.
- **Изменить** `bot/handlers/balance.py`, `bot/handlers/configs.py`,
  `bot/handlers/devices.py`, `bot/handlers/admin/subscription.py`,
  `bot/handlers/admin/users.py` — запись событий доступа и админских действий.
- **Изменить** `bot/services/scheduler.py` — секция очистки старых записей.
- **Изменить** `bot/config.py` — `audit_retention_days`.

---

## Задача 1: Модель журнала

**Файлы:**
- Изменить: `bot/db/models.py` (в конец файла, после `CryptoInvoice`)
- Тест: `tests/test_audit.py` (создать)

**Интерфейсы:**
- Отдаёт: класс `AuditLog` (таблица `audit_logs`) и `StrEnum AuditAction`
  со значениями, перечисленными ниже. Все последующие задачи пишут в журнал
  только через коды из `AuditAction`.

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_audit.py`:

```python
"""Тесты журнала действий."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AuditAction, AuditLog


class TestAuditModel:
    async def test_row_is_stored_with_all_fields(self, session: AsyncSession) -> None:
        session.add(AuditLog(
            actor_tg_id=111,
            actor_is_admin=True,
            action=AuditAction.ADMIN_CREDIT,
            target_user_id=7,
            target_type="user",
            target_id=7,
            amount_kopeks=13000,
            details="Начислено вручную",
        ))
        await session.flush()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.id is not None
        assert row.created_at is not None
        assert row.action == AuditAction.ADMIN_CREDIT
        assert row.amount_kopeks == 13000
        assert row.target_user_id == 7

    async def test_system_event_has_no_actor(self, session: AsyncSession) -> None:
        """События планировщика пишутся без человека-инициатора."""
        session.add(AuditLog(action=AuditAction.SERVER_DOWN, target_type="server", target_id=1))
        await session.flush()

        row = (await session.execute(select(AuditLog))).scalar_one()
        assert row.actor_tg_id is None
        assert row.actor_is_admin is False
        assert row.amount_kopeks is None
```

- [ ] **Шаг 2: Запустить тест, убедиться что падает**

Запустить: `python -m pytest tests/test_audit.py -v`
Ожидается: FAIL — `ImportError: cannot import name 'AuditAction'`.

- [ ] **Шаг 3: Добавить модель**

В `bot/db/models.py`, в конец файла:

```python
class AuditAction(StrEnum):
    """Коды событий журнала. Значение пишется в базу — не переименовывать
    задним числом, иначе старые записи потеряют смысл."""

    # Деньги
    BALANCE_TOPUP = "balance_topup"        # пополнение (крипта/платёжка)
    BALANCE_CHARGE = "balance_charge"      # списание за подписку
    REFERRAL_REWARD = "referral_reward"    # начисление пригласившему
    ADMIN_CREDIT = "admin_credit"          # ручное начисление админом

    # Доступ
    CONFIG_ISSUED = "config_issued"        # выдан конфиг
    CONFIG_REVOKED = "config_revoked"      # конфиг отозван
    CONFIG_REVIVED = "config_revived"      # конфиг ожил после оплаты
    SUB_GRANTED = "sub_granted"            # подписка выдана админом
    USER_BLOCKED = "user_blocked"
    USER_UNBLOCKED = "user_unblocked"

    # Админское
    TARIFF_CHANGED = "tariff_changed"      # админ поменял тариф юзеру
    USER_WIPED = "user_wiped"              # стирание юзера

    # Серверное
    SERVER_DOWN = "server_down"
    SERVER_UP = "server_up"


class AuditLog(Base):
    """Журнал важных действий: деньги, доступ, админские операции, серверы.

    Пишется в той же транзакции, что и само действие, — если действие
    откатилось, записи о нём не остаётся. Читает только админ.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    # Кто сделал. NULL — сделал бот сам (планировщик, автопродление).
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    actor_is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    action: Mapped[AuditAction] = mapped_column(String(32), index=True)

    # Над кем. Хранится User.id (не tg_id) — чтобы история собиралась одним
    # запросом по карточке юзера в админке.
    target_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Над чем: "peer" | "device" | "wdtt" | "server" | "user" | NULL.
    target_type: Mapped[str | None] = mapped_column(String(16))
    target_id: Mapped[int | None] = mapped_column(Integer)

    # Заполняется только у денежных событий. В копейках, как и везде.
    amount_kopeks: Mapped[int | None] = mapped_column(Integer)

    # Человекочитаемое пояснение для админа («Подписка 3 мес», метка устройства).
    details: Mapped[str | None] = mapped_column(Text)
```

Проверить, что `StrEnum`, `BigInteger`, `Boolean`, `Text`, `func` уже импортированы
в шапке `models.py` — они там есть, так как используются существующими моделями.

- [ ] **Шаг 4: Запустить тест, убедиться что проходит**

Запустить: `python -m pytest tests/test_audit.py -v`
Ожидается: PASS, 2 теста.

- [ ] **Шаг 5: Проверить, что миграция подхватит таблицу на живой базе**

Запустить: `python -m pytest tests/test_migrate.py -v`
Ожидается: PASS. Новая таблица создаётся через `create_all` (она отсутствует
целиком), поэтому `run_migrations` её пропускает — это штатное поведение,
описанное в `bot/db/migrate.py:64`.

- [ ] **Шаг 6: Коммит**

```bash
git add bot/db/models.py tests/test_audit.py
git commit -m "Журнал: модель audit_logs и коды событий"
```

---

## Задача 2: Репозиторий журнала

**Файлы:**
- Создать: `bot/db/repo/audit.py`
- Изменить: `bot/db/repo/__init__.py`
- Тест: `tests/test_audit.py` (дополнить)

**Интерфейсы:**
- Использует: `AuditLog`, `AuditAction` из задачи 1.
- Отдаёт:
  - `async def log_action(session, action, *, actor_tg_id=None, actor_is_admin=False, target_user_id=None, target_type=None, target_id=None, amount_kopeks=None, details=None) -> None`
  - `async def list_audit(session, *, limit=10, offset=0) -> list[AuditLog]`
  - `async def count_audit(session) -> int`
  - `async def list_audit_for_user(session, user_id, *, limit=10, offset=0) -> list[AuditLog]`
  - `async def count_audit_for_user(session, user_id) -> int`
  - `async def delete_audit_older_than(session, days: int) -> int`

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_audit.py`:

```python
from datetime import datetime, timedelta, timezone

from bot.db import repo


class TestAuditRepo:
    async def test_log_action_writes_row(self, session: AsyncSession) -> None:
        await repo.log_action(
            session, AuditAction.CONFIG_ISSUED,
            actor_tg_id=555, target_user_id=3, target_type="peer", target_id=9,
            details="Телефон",
        )
        rows = await repo.list_audit(session)
        assert len(rows) == 1
        assert rows[0].action == AuditAction.CONFIG_ISSUED
        assert rows[0].details == "Телефон"

    async def test_list_is_newest_first(self, session: AsyncSession) -> None:
        for i in range(3):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        rows = await repo.list_audit(session)
        assert [r.details for r in rows] == ["#2", "#1", "#0"]

    async def test_paging(self, session: AsyncSession) -> None:
        for i in range(5):
            await repo.log_action(session, AuditAction.CONFIG_ISSUED, details=f"#{i}")
        page2 = await repo.list_audit(session, limit=2, offset=2)
        assert [r.details for r in page2] == ["#2", "#1"]
        assert await repo.count_audit(session) == 5

    async def test_history_filters_by_user(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=1)
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, target_user_id=2)
        mine = await repo.list_audit_for_user(session, 1)
        assert len(mine) == 1
        assert mine[0].target_user_id == 1
        assert await repo.count_audit_for_user(session, 1) == 1

    async def test_retention_deletes_only_old(self, session: AsyncSession) -> None:
        await repo.log_action(session, AuditAction.CONFIG_ISSUED, details="свежая")
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            details="старая",
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
        )
        session.add(old)
        await session.flush()

        removed = await repo.delete_audit_older_than(session, days=90)
        assert removed == 1
        left = await repo.list_audit(session)
        assert [r.details for r in left] == ["свежая"]

    async def test_retention_zero_days_keeps_everything(self, session: AsyncSession) -> None:
        """0 = ретеншн выключен: журнал не чистится вообще."""
        old = AuditLog(
            action=AuditAction.CONFIG_ISSUED,
            created_at=datetime.now(timezone.utc) - timedelta(days=999),
        )
        session.add(old)
        await session.flush()

        assert await repo.delete_audit_older_than(session, days=0) == 0
        assert len(await repo.list_audit(session)) == 1
```

- [ ] **Шаг 2: Запустить тест, убедиться что падает**

Запустить: `python -m pytest tests/test_audit.py::TestAuditRepo -v`
Ожидается: FAIL — `AttributeError: module 'bot.db.repo' has no attribute 'log_action'`.

- [ ] **Шаг 3: Написать репозиторий**

Создать `bot/db/repo/audit.py`:

```python
"""Журнал действий: запись событий и выборки для админки.

Пишем в той же сессии, что и само действие, — коммит на вызывающем. Так
запись о событии не появится, если само событие откатилось.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AuditAction, AuditLog


async def log_action(
    session: AsyncSession,
    action: AuditAction,
    *,
    actor_tg_id: int | None = None,
    actor_is_admin: bool = False,
    target_user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    amount_kopeks: int | None = None,
    details: str | None = None,
) -> None:
    """Записывает событие. Коммит — на вызывающем."""
    session.add(AuditLog(
        action=action,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=target_user_id,
        target_type=target_type,
        target_id=target_id,
        amount_kopeks=amount_kopeks,
        details=details,
    ))
    await session.flush()


async def list_audit(
    session: AsyncSession, *, limit: int = 10, offset: int = 0
) -> list[AuditLog]:
    """Лента журнала, свежие сверху. Вторичная сортировка по id — у SQLite
    несколько записей в одной транзакции получают одинаковый created_at."""
    res = await session.execute(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res.scalars().all())


async def count_audit(session: AsyncSession) -> int:
    res = await session.execute(select(func.count(AuditLog.id)))
    return int(res.scalar_one())


async def list_audit_for_user(
    session: AsyncSession, user_id: int, *, limit: int = 10, offset: int = 0
) -> list[AuditLog]:
    res = await session.execute(
        select(AuditLog)
        .where(AuditLog.target_user_id == user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res.scalars().all())


async def count_audit_for_user(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(
        select(func.count(AuditLog.id)).where(AuditLog.target_user_id == user_id)
    )
    return int(res.scalar_one())


async def delete_audit_older_than(session: AsyncSession, days: int) -> int:
    """Чистка старых записей. days <= 0 — ретеншн выключен, не трогаем ничего."""
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    await session.flush()
    return int(res.rowcount or 0)
```

- [ ] **Шаг 4: Подключить в пакет репозитория**

В `bot/db/repo/__init__.py` добавить блок импорта (в алфавитном порядке — сразу
после блока `from bot.db.repo.billing import (...)`):

```python
from bot.db.repo.audit import (
    count_audit,
    count_audit_for_user,
    delete_audit_older_than,
    list_audit,
    list_audit_for_user,
    log_action,
)
```

Если в файле есть список `__all__`, дописать в него те же шесть имён.

- [ ] **Шаг 5: Запустить тесты, убедиться что проходят**

Запустить: `python -m pytest tests/test_audit.py -v`
Ожидается: PASS, 8 тестов.

- [ ] **Шаг 6: Коммит**

```bash
git add bot/db/repo/audit.py bot/db/repo/__init__.py tests/test_audit.py
git commit -m "Журнал: репозиторий записи и выборок"
```

---

## Задача 3: Запись денежных событий

**Файлы:**
- Изменить: `bot/services/billing.py` (функции `charge_and_extend`, `grant_term`)
- Изменить: `bot/handlers/balance.py` (зачисление пополнения и реферальной награды)
- Тест: `tests/test_audit.py` (дополнить)

**Интерфейсы:**
- Использует: `repo.log_action`, `AuditAction` из задач 1–2.
- Отдаёт: ничего нового — врезки в существующие функции.

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_audit.py`:

```python
from bot.db.models import Server, ServerStatus, User
from bot.services import billing


class TestAuditMoney:
    async def test_purchase_is_logged(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1001, username="buyer", full_name="Buyer"
        )
        user.balance_kopeks = 100_000
        user.sub_max_devices = 2
        user.sub_max_bypass = 1
        await session.flush()

        res = await billing.charge_and_extend(session, user, months=1, devices=2, bypass=1)
        assert res.ok

        rows = await repo.list_audit_for_user(session, user.id)
        charges = [r for r in rows if r.action == AuditAction.BALANCE_CHARGE]
        assert len(charges) == 1
        assert charges[0].amount_kopeks == res.price_kopeks
        assert charges[0].actor_tg_id == user.tg_id

    async def test_admin_grant_is_logged_as_admin_action(
        self, session: AsyncSession
    ) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1002, username="gifted", full_name="Gifted"
        )
        user.sub_max_devices = 1
        user.sub_max_bypass = 1
        await session.flush()

        await billing.grant_term(session, user, months=3, actor_tg_id=111)

        rows = await repo.list_audit_for_user(session, user.id)
        grants = [r for r in rows if r.action == AuditAction.SUB_GRANTED]
        assert len(grants) == 1
        assert grants[0].actor_is_admin is True
        assert grants[0].actor_tg_id == 111
        # Подарок — не списание: денежной суммы у события нет.
        assert grants[0].amount_kopeks is None
```

- [ ] **Шаг 2: Запустить тест, убедиться что падает**

Запустить: `python -m pytest tests/test_audit.py::TestAuditMoney -v`
Ожидается: FAIL — журнал пуст (`len(charges) == 0`), а `grant_term` не
принимает `actor_tg_id` (`TypeError: unexpected keyword argument`).

- [ ] **Шаг 3: Врезать запись в billing.py**

В `bot/services/billing.py`, в `charge_and_extend`, сразу после существующего
`logger.info("Sub charged: ...")` (рядом со строкой 159) добавить:

```python
    await repo.log_action(
        session, AuditAction.BALANCE_CHARGE,
        actor_tg_id=user.tg_id,
        target_user_id=user.id,
        amount_kopeks=price,
        details=f"Подписка {months} мес (устройств: {devices}, обходов: {bypass})",
    )
```

В `grant_term` изменить сигнатуру, добавив параметр (значение по умолчанию
`None` — чтобы существующие вызовы не сломались):

```python
async def grant_term(
    session: AsyncSession, user: User, months: int, *, actor_tg_id: int | None = None
) -> ChargeResult:
```

и после существующего `logger.info("Sub granted by admin: ...")` добавить:

```python
    await repo.log_action(
        session, AuditAction.SUB_GRANTED,
        actor_tg_id=actor_tg_id,
        actor_is_admin=True,
        target_user_id=user.id,
        details=f"Подписка на {months} мес выдана админом",
    )
```

В шапке `billing.py` добавить `AuditAction` в существующий импорт из
`bot.db.models`.

- [ ] **Шаг 4: Передать админа в вызов grant_term**

В `bot/handlers/admin/subscription.py` найти вызов `grant_term(` и добавить
аргумент `actor_tg_id=call.from_user.id` (в хендлере, который выдаёт подписку).

- [ ] **Шаг 5: Врезать запись пополнения и реферальной награды**

В `bot/handlers/balance.py` найти место, где начисляется пополнение (рядом с
`repo.add_balance_tx(... kind="deposit" ...)`), и после него добавить:

```python
        await repo.log_action(
            session, AuditAction.BALANCE_TOPUP,
            actor_tg_id=user.tg_id,
            target_user_id=user.id,
            amount_kopeks=dep.amount_kopeks,
            details="Пополнение баланса",
        )
```

Рядом с начислением реферальной награды (`kind="ref"`) добавить:

```python
        await repo.log_action(
            session, AuditAction.REFERRAL_REWARD,
            target_user_id=referrer.id,
            amount_kopeks=dep.ref_reward_kopeks,
            details=f"{settings.referral_percent}% с пополнения реферала",
        )
```

Имена переменных (`dep`, `referrer`) взять те, что уже используются в этом
месте файла, — не вводить новых. `AuditAction` добавить в импорты из
`bot.db.models`.

- [ ] **Шаг 6: Запустить тесты**

Запустить: `python -m pytest tests/test_audit.py tests/test_billing.py -v`
Ожидается: PASS. Тесты биллинга не должны сломаться — `grant_term` получил
параметр со значением по умолчанию.

- [ ] **Шаг 7: Коммит**

```bash
git add bot/services/billing.py bot/handlers/balance.py \
        bot/handlers/admin/subscription.py tests/test_audit.py
git commit -m "Журнал: денежные события (покупка, пополнение, реферал, подарок)"
```

---

## Задача 4: Запись событий доступа

**Файлы:**
- Изменить: `bot/handlers/configs.py` (`_create_peer_for_user` — выдача конфига)
- Изменить: `bot/handlers/devices.py` (удаление устройства юзером)
- Изменить: `bot/services/revive.py` (оживление конфигов после оплаты)
- Изменить: `bot/services/scheduler.py` (отзыв по истечению)
- Тест: `tests/test_audit.py` (дополнить)

**Интерфейсы:**
- Использует: `repo.log_action`, `AuditAction`.
- Отдаёт: ничего нового.

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_audit.py`:

```python
class TestAuditAccess:
    async def test_revive_is_logged(self, session: AsyncSession) -> None:
        """Оживление конфигов после оплаты — событие доступа без суммы."""
        user = await repo.get_or_create_user(
            session, tg_id=1003, username="revived", full_name="Revived"
        )
        await session.flush()

        await repo.log_action(
            session, AuditAction.CONFIG_REVIVED,
            target_user_id=user.id, target_type="peer", target_id=42,
            details="Конфиг оживлён после оплаты",
        )

        rows = await repo.list_audit_for_user(session, user.id)
        assert rows[0].action == AuditAction.CONFIG_REVIVED
        assert rows[0].amount_kopeks is None
        assert rows[0].target_type == "peer"
```

Этот тест проверяет форму записи события доступа. Сами врезки в хендлеры
проверяются вручную по чек-листу в шаге 5 — поднимать в тестах живой SSH
нельзя, а мокать весь путь выдачи пира дороже, чем он стоит.

- [ ] **Шаг 2: Запустить тест, убедиться что проходит**

Запустить: `python -m pytest tests/test_audit.py::TestAuditAccess -v`
Ожидается: PASS (репозиторий из задачи 2 уже умеет это писать).

- [ ] **Шаг 3: Врезать выдачу конфига**

В `bot/handlers/configs.py`, в `_create_peer_for_user`, после
`await amnezia.add_peer_on_server(...)` и выхода из блока `async with
_server_ip_lock(...)`, добавить:

```python
    await repo.log_action(
        session, AuditAction.CONFIG_ISSUED,
        actor_tg_id=user.tg_id,
        target_user_id=user.id,
        target_type="peer",
        target_id=peer.id,
        details=f"{label} на сервере «{server.name}»",
    )
```

- [ ] **Шаг 4: Врезать отзыв и оживление**

В `bot/handlers/devices.py`, рядом с существующим
`logger.info("User {} deleted device {} ({})", ...)`:

```python
    await repo.log_action(
        session, AuditAction.CONFIG_REVOKED,
        actor_tg_id=user.tg_id,
        target_user_id=user.id,
        target_type="device",
        target_id=device_id,
        details=f"Устройство «{label}» удалено юзером",
    )
```

В `bot/services/revive.py`, там где конфиг переводится в активное состояние,
добавить запись с `AuditAction.CONFIG_REVIVED`, `actor_tg_id=None` (оживляет
бот), `target_user_id=<id юзера>`, `target_type="peer"`, `target_id=<id пира>`.

В `bot/services/scheduler.py`, в секции отзыва по истечению подписки, рядом с
существующим логированием отзыва добавить запись с
`AuditAction.CONFIG_REVOKED`, `actor_tg_id=None`,
`details="Отозван по истечению подписки"`.

Во всех четырёх файлах добавить `AuditAction` в импорт из `bot.db.models` и
`repo` из `bot.db`, если их там ещё нет.

- [ ] **Шаг 5: Проверить руками в живом боте**

Запустить бота и пройти сценарий: создать устройство → получить конфиг →
удалить устройство. Затем в базе:

```bash
sqlite3 data/vpn_bot.sqlite3 \
  "SELECT created_at, action, details FROM audit_logs ORDER BY id DESC LIMIT 5;"
```

Ожидается: строки `config_issued` и `config_revoked` с осмысленными `details`.

- [ ] **Шаг 6: Запустить весь набор тестов**

Запустить: `python -m pytest`
Ожидается: PASS целиком — врезки не должны сломать существующие тесты.

- [ ] **Шаг 7: Коммит**

```bash
git add bot/handlers/configs.py bot/handlers/devices.py bot/services/revive.py \
        bot/services/scheduler.py tests/test_audit.py
git commit -m "Журнал: события доступа (выдача, отзыв, оживление)"
```

---

## Задача 5: Запись админских действий

**Файлы:**
- Изменить: `bot/handlers/admin/users.py` (блокировка, стирание юзера)
- Изменить: `bot/handlers/admin/subscription.py` (смена тарифа)
- Тест: `tests/test_audit.py` (дополнить)

**Интерфейсы:**
- Использует: `repo.log_action`, `AuditAction`.

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_audit.py`:

```python
class TestAuditAdmin:
    async def test_admin_actions_are_marked(self, session: AsyncSession) -> None:
        user = await repo.get_or_create_user(
            session, tg_id=1004, username="target", full_name="Target"
        )
        await session.flush()

        await repo.log_action(
            session, AuditAction.USER_BLOCKED,
            actor_tg_id=111, actor_is_admin=True,
            target_user_id=user.id, target_type="user", target_id=user.id,
            details="Заблокирован админом",
        )

        rows = await repo.list_audit_for_user(session, user.id)
        assert rows[0].actor_is_admin is True
        assert rows[0].actor_tg_id == 111
```

- [ ] **Шаг 2: Запустить тест**

Запустить: `python -m pytest tests/test_audit.py::TestAuditAdmin -v`
Ожидается: PASS.

- [ ] **Шаг 3: Врезать блокировку**

В `bot/handlers/admin/users.py`, в `cb_panel_toggle_block` (около строки 87),
после того как флаг блокировки уже изменён и до отправки ответа:

```python
    await repo.log_action(
        session,
        AuditAction.USER_BLOCKED if block else AuditAction.USER_UNBLOCKED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=user.id,
        target_type="user",
        target_id=user.id,
        details="Заблокирован админом" if block else "Разблокирован админом",
    )
```

Имя переменной `block` взять то, что уже используется в этом хендлере
(строка 108 передаёт `block` в `user_card_kb`).

- [ ] **Шаг 4: Врезать стирание юзера**

В `bot/handlers/admin/users.py`, в `cb_panel_user_delete_confirm` (около строки
147), **до** вызова стирания — иначе запись удалится вместе с юзером:

```python
    await repo.log_action(
        session, AuditAction.USER_WIPED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=None,  # юзера сейчас не станет — ссылку не держим
        target_type="user",
        target_id=user.id,
        details=f"Стёрт юзер tg_id {user.tg_id} (@{user.username or '—'})",
    )
```

`target_user_id` здесь намеренно `None`: юзер удаляется, и запись в его
истории всё равно никто не откроет — а в общей ленте событие останется.

- [ ] **Шаг 5: Врезать смену тарифа**

В `bot/handlers/admin/subscription.py`, там где админ меняет лимиты устройств
или обходов, добавить:

```python
    await repo.log_action(
        session, AuditAction.TARIFF_CHANGED,
        actor_tg_id=call.from_user.id,
        actor_is_admin=True,
        target_user_id=user.id,
        details=f"Тариф: устройств {user.sub_max_devices}, обходов {user.sub_max_bypass}",
    )
```

- [ ] **Шаг 6: Запустить весь набор тестов**

Запустить: `python -m pytest`
Ожидается: PASS целиком, в том числе `tests/test_user_wipe.py`.

- [ ] **Шаг 7: Коммит**

```bash
git add bot/handlers/admin/users.py bot/handlers/admin/subscription.py tests/test_audit.py
git commit -m "Журнал: админские действия (блокировка, тариф, стирание)"
```

---

## Задача 6: Экран «Журнал» в админке

**Файлы:**
- Создать: `bot/handlers/admin/audit.py`
- Изменить: `bot/keyboards/inline/admin.py`
- Изменить: `bot/handlers/admin/__init__.py`

**Интерфейсы:**
- Использует: `repo.list_audit`, `repo.count_audit` из задачи 2.
- Отдаёт: колбэк `{CB_PANEL}:audit:<page>` — лента журнала;
  функцию `audit_list_kb(page, has_prev, has_next)` в клавиатурах;
  функцию `fmt_audit_row(row) -> str` в `bot/handlers/admin/audit.py`,
  которую переиспользует задача 7.

- [ ] **Шаг 1: Добавить кнопку в меню админки**

В `bot/keyboards/inline/admin.py`, в `admin_panel_menu()`, после кнопки
«📊 Статистика» (строка 14):

```python
    kb.button(text="📝 Журнал",       callback_data=f"{CB_PANEL}:audit:0")
```

Там же, ниже, добавить клавиатуру ленты:

```python
def audit_list_kb(page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    """Лента журнала: листалка + возврат в панель."""
    kb = InlineKeyboardBuilder()
    if has_prev:
        kb.button(text="« Новее", callback_data=f"{CB_PANEL}:audit:{page - 1}")
    if has_next:
        kb.button(text="Старее »", callback_data=f"{CB_PANEL}:audit:{page + 1}")
    kb.button(text="« Админ-панель", callback_data=f"{CB_PANEL}:main")
    kb.adjust(2, 1)
    return kb.as_markup()
```

Экспортировать `audit_list_kb` из `bot/keyboards/inline/__init__.py` тем же
способом, каким там уже экспортируется `admin_panel_menu`.

- [ ] **Шаг 2: Написать экран**

Создать `bot/handlers/admin/audit.py`:

```python
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
from bot.keyboards.inline import CB_PANEL, audit_list_kb
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
    AuditAction.USER_BLOCKED: "⛔ Юзер заблокирован",
    AuditAction.USER_UNBLOCKED: "✅ Юзер разблокирован",
    AuditAction.TARIFF_CHANGED: "⚙️ Тариф изменён",
    AuditAction.USER_WIPED: "🗑 Юзер стёрт",
    AuditAction.SERVER_DOWN: "🔴 Сервер упал",
    AuditAction.SERVER_UP: "🟢 Сервер поднялся",
}

router = Router(name="admin_audit")


def fmt_audit_row(row: AuditLog) -> str:
    """Одна строка журнала для админа. Время — МСК, деньги — рублями."""
    title = _TITLES.get(row.action, row.action)
    parts = [f"<b>{title}</b> — {fmt_msk(row.created_at)} МСК"]
    if row.amount_kopeks is not None:
        parts.append(f"Сумма: {fmt_rub(row.amount_kopeks)}")
    if row.actor_tg_id is not None:
        who = "админ" if row.actor_is_admin else "юзер"
        parts.append(f"Кто: {who} <code>{row.actor_tg_id}</code>")
    else:
        parts.append("Кто: бот")
    if row.details:
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
```

- [ ] **Шаг 3: Подключить роутер**

В `bot/handlers/admin/__init__.py` добавить `audit` в импорт модулей и строку
`router.include_router(audit.router)` рядом с остальными.

- [ ] **Шаг 4: Проверить, что бот стартует и экран открывается**

Запустить: `python -m pytest` (регрессия) и затем поднять бота, открыть
`/admin` → «📝 Журнал». Ожидается: лента с событиями, листалка работает,
«« Админ-панель» возвращает назад.

- [ ] **Шаг 5: Коммит**

```bash
git add bot/handlers/admin/audit.py bot/handlers/admin/__init__.py \
        bot/keyboards/inline/admin.py bot/keyboards/inline/__init__.py
git commit -m "Журнал: экран ленты в админ-панели"
```

---

## Задача 7: История в карточке юзера

**Файлы:**
- Изменить: `bot/keyboards/inline/admin.py` (`user_card_kb`, строка 145)
- Изменить: `bot/handlers/admin/audit.py` (второй хендлер)

**Интерфейсы:**
- Использует: `fmt_audit_row` из задачи 6, `repo.list_audit_for_user`,
  `repo.count_audit_for_user` из задачи 2.
- Отдаёт: колбэк `{CB_PANEL}:uhist:<user_id>:<page>`.

- [ ] **Шаг 1: Добавить кнопку в карточку юзера**

В `bot/keyboards/inline/admin.py`, в `user_card_kb` (строка 145), добавить
кнопку рядом с остальными действиями над юзером:

```python
    kb.button(text="🕘 История", callback_data=f"{CB_PANEL}:uhist:{user_id}:{page}:0")
```

Здесь три аргумента: id юзера, страница списка юзеров (чтобы вернуться туда,
откуда пришли) и страница самой истории.

Там же добавить клавиатуру истории:

```python
def user_history_kb(
    user_id: int, page: int, hpage: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    """История юзера: листалка + возврат в его карточку."""
    kb = InlineKeyboardBuilder()
    if has_prev:
        kb.button(text="« Новее", callback_data=f"{CB_PANEL}:uhist:{user_id}:{page}:{hpage - 1}")
    if has_next:
        kb.button(text="Старее »", callback_data=f"{CB_PANEL}:uhist:{user_id}:{page}:{hpage + 1}")
    kb.button(text="« К пользователю", callback_data=f"{CB_PANEL}:user:{user_id}:{page}")
    kb.adjust(2, 1)
    return kb.as_markup()
```

Экспортировать `user_history_kb` из `bot/keyboards/inline/__init__.py`.

- [ ] **Шаг 2: Написать хендлер**

В `bot/handlers/admin/audit.py` добавить (импорт `user_history_kb` дописать
к существующему импорту из `bot.keyboards.inline`):

```python
@router.callback_query(F.data.startswith(f"{CB_PANEL}:uhist:"))
async def cb_user_history(call: CallbackQuery, session: AsyncSession) -> None:
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
```

- [ ] **Шаг 3: Проверить, что колбэк не пересекается с соседями**

Запустить: `grep -n "uhist\|:user:" bot/keyboards/inline/admin.py`
Ожидается: префикс `pnl:uhist:` не является префиксом никакого другого
колбэка. Существующие `pnl:udev:`, `pnl:ubp:`, `pnl:user:` начинаются иначе,
пересечения нет.

- [ ] **Шаг 4: Проверить в живом боте**

`/admin` → «👤 Пользователи» → выбрать юзера → «🕘 История». Ожидается: список
его событий, листалка, возврат в карточку тем же путём, каким пришли.

- [ ] **Шаг 5: Запустить весь набор тестов**

Запустить: `python -m pytest`
Ожидается: PASS.

- [ ] **Шаг 6: Коммит**

```bash
git add bot/keyboards/inline/admin.py bot/keyboards/inline/__init__.py \
        bot/handlers/admin/audit.py
git commit -m "Журнал: история в карточке юзера"
```

---

## Задача 8: Автоочистка старых записей

**Файлы:**
- Изменить: `bot/config.py`
- Изменить: `bot/services/scheduler.py`
- Тест: `tests/test_audit.py` (уже покрыт в задаче 2 —
  `test_retention_deletes_only_old`, `test_retention_zero_days_keeps_everything`)

**Интерфейсы:**
- Использует: `repo.delete_audit_older_than` из задачи 2.
- Отдаёт: настройку `settings.audit_retention_days`.

- [ ] **Шаг 1: Добавить настройку**

В `bot/config.py`, рядом с `linkcheck_interval_days` (строка 65):

```python
    # Сколько дней держим записи журнала действий. 0 = не чистить никогда.
    # 90 дней хватает, чтобы разобрать спор по оплате, и база не пухнет.
    audit_retention_days: int = 90
```

- [ ] **Шаг 2: Добавить секцию в планировщик**

В `bot/services/scheduler.py` добавить очистку в том же стиле, в каком там уже
живут остальные секции (каждая секция изолирована — падение одной не роняет
остальные, см. блок «Живучесть»):

```python
async def _cleanup_audit(session: AsyncSession) -> None:
    """Чистка журнала по ретеншну. Идёт последней: если упадёт, важные секции
    (отзыв, автопродление) уже отработали."""
    removed = await repo.delete_audit_older_than(session, settings.audit_retention_days)
    if removed:
        logger.info("Ретеншн журнала: удалено записей {}", removed)
```

Вызвать `_cleanup_audit` из основного тика планировщика — в том же месте и тем
же способом изоляции, что и соседние секции (обёртка с перехватом исключения).

- [ ] **Шаг 3: Запустить тесты ретеншна**

Запустить: `python -m pytest tests/test_audit.py -v -k retention`
Ожидается: PASS, 2 теста.

- [ ] **Шаг 4: Запустить весь набор тестов**

Запустить: `python -m pytest`
Ожидается: PASS целиком.

- [ ] **Шаг 5: Обновить README**

В `README.md` добавить `AUDIT_RETENTION_DAYS` в список переменных окружения с
пояснением «сколько дней хранится журнал действий, 0 — хранить вечно», рядом
с остальными настройками планировщика. Так же добавить строку в `.env.example`.

- [ ] **Шаг 6: Коммит**

```bash
git add bot/config.py bot/services/scheduler.py README.md .env.example
git commit -m "Журнал: автоочистка старых записей по ретеншну"
```

---

## Самопроверка плана

**Покрытие спеки.** Раздел «A2. Журнал действий» требует записи денег
(задача 3), доступа (задача 4), админских действий (задача 5), серверных
событий, ленты «Журнал» (задача 6), «Истории» в карточке юзера (задача 7),
автоочистки (задача 8).

**Известный пробел:** коды `SERVER_DOWN` / `SERVER_UP` заведены в задаче 1 и
показываются в задаче 6, но врезка в код, который замечает падение сервера,
ни в одну задачу не попала. Причина — этого кода в проекте сейчас нет:
`ServerStatus` меняется при установке, а регулярной проверки живости серверов
не существует. Заводить её здесь — это отдельная фича, а не журнал. Коды
оставлены на будущее, лента их отрисует, как только появится тот, кто их пишет.

**Плейсхолдеры:** в шагах, где нужно найти существующее место врезки
(`balance.py`, `revive.py`, `scheduler.py`, `subscription.py`), указан якорь
для поиска — соседний `logger.info` или вызов `repo.add_balance_tx` — и точный
код, который надо добавить. Имена переменных велено брать из окружающего кода,
а не выдумывать.

**Согласованность имён:** `log_action`, `list_audit`, `count_audit`,
`list_audit_for_user`, `count_audit_for_user`, `delete_audit_older_than`
определены в задаче 2 и используются под теми же именами в задачах 3–8.
`fmt_audit_row` определена в задаче 6 и переиспользована в задаче 7.
`audit_list_kb` и `user_history_kb` определены в задачах 6 и 7 соответственно.
