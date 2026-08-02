# Этап B: мелочи UX и админки — план реализации

> **Для агентов:** ОБЯЗАТЕЛЬНЫЙ СУБ-СКИЛЛ: используй superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans, чтобы выполнять план задача за задачей. Шаги помечены чекбоксами (`- [ ]`).

**Цель:** Юзер сам выбирает, в каком виде получить конфиг, вместо трёх сообщений на весь экран; админ видит в карточке обхода БС, когда им пользовались в последний раз, и может из карточки юзера провалиться в серверную карточку пира или обхода.

**Архитектура:** Сборка текста конфига из БД дублируется сейчас в трёх местах — выносится в один хелпер, и на нём строится новый модуль доставки конфига: одно сообщение с выбором формата и три отправителя (файл / QR / `vpn://`-ссылка). «Последний раз использовался» у обхода берётся не с сервера обхода (он такой цифры не отдаёт вовсе), а из прироста счётчиков трафика, которые планировщик и так опрашивает каждые 5 минут.

**Стек:** Python 3.13, aiogram 3, SQLAlchemy 2 (async), SQLite (aiosqlite), pytest (asyncio_mode=auto), loguru.

## Глобальные ограничения

- Прогон: `python -m pytest` из `/root/myvpn-bot` (termux-питон). Базовая линия — **238 passed, 2 failed**; оба падения в `tests/test_qrgen.py` (`ModuleNotFoundError: No module named 'PIL'`), это предсуществующая поломка окружения, чинить её не надо и трогать эти тесты нельзя.
- Функции репозитория делают `flush()`, но НЕ `commit()`. Коммит — на вызывающем.
- Событие журнала пишется в той же транзакции, что и само действие.
- `target_user_id` в журнале — это `User.id`, не `tg_id`.
- Время в интерфейсе — московское, через `bot.utils.timefmt.fmt_msk`. Naive/aware: SQLite отдаёт `DateTime(timezone=True)` БЕЗ tzinfo, перед арифметикой в Python гнать через `bot.utils.timefmt.as_utc`.
- Комментарии и сообщения коммитов — на русском, объясняют «почему», а не «что».
- Новых кодов `AuditAction` в этом плане не заводится.
- Никаких новых зависимостей.
- Тексты для юзера — человеческим языком, без техножаргона; сырой текст исключений юзеру не показывается.

## Решения, принятые Владом до начала работ

- Третья кнопка выбора формата — **`vpn://`-ссылка**, не полный текст конфига. Называется «🔗 Ссылкой».
- «Последний хендшейк» обхода считается **по приросту трафика** на тике планировщика (точность 5 минут), сервер обхода не трогаем. В интерфейсе называется «последний трафик», а не «хендшейк», — потому что это и есть правда.

## Раскладка файлов

| Файл | Что делает |
|---|---|
| `bot/handlers/config_delivery.py` (создать, ~200 строк) | Единственное место, где конфиг превращается в сообщения: сборка текста конфига из пира, экран выбора формата, три отправителя, проверка прав. |
| `bot/keyboards/inline/devices.py` (править) | `config_format_kb` — клавиатура выбора формата. |
| `bot/keyboards/inline/prefixes.py` (править) | Новый префикс `CB_CFG`. |
| `bot/handlers/configs.py` (править) | `_create_peer_for_user` возвращает пир; `_send_peer_artifacts` удаляется; сборка conf берётся из хелпера. |
| `bot/handlers/devices.py` (править) | Четыре места выдачи переводятся на экран выбора. |
| `bot/handlers/admin/user_items.py` (править) | Выдача конфига админу — на экран выбора; кнопки перехода к пиру и обходу. |
| `bot/keyboards/inline/admin.py` (править) | Кнопки «К пиру» / «К обходу» в карточках устройства и обхода. |
| `bot/db/models.py` (править) | `WdttAccess.last_seen_at`. |
| `bot/services/scheduler.py` (править) | Проставление `last_seen_at` при приросте трафика обхода. |
| `bot/handlers/servers/bypass.py` (править) | «Последний трафик» в серверной карточке обхода. |
| `bot/handlers/__init__.py` (править) | Регистрация нового роутера. |
| `tests/test_config_delivery.py` (создать) | Права на конфиг, сборка conf, три формата. |
| `tests/test_bypass_lastseen.py` (создать) | Прирост трафика → `last_seen_at`. |

---

### Задача 1: Хелпер сборки конфига из пира

Сейчас один и тот же блок «взять сервер, расшифровать ключ, собрать conf» скопирован в трёх местах (`bot/handlers/devices.py:306-318`, `bot/handlers/devices.py:340-350`, `bot/handlers/admin/user_items.py:101-110`). Экран выбора формата будет собирать конфиг заново по `peer_id` в момент нажатия кнопки, поэтому хелпер нужен до всего остального.

**Файлы:**
- Создать: `bot/handlers/config_delivery.py`
- Изменить: `bot/handlers/configs.py:63-131` (возврат `_create_peer_for_user`), `bot/handlers/configs.py:155` (единственный вызывающий)
- Тест: `tests/test_config_delivery.py`

**Интерфейсы:**
- Отдаёт наружу:
  - `async def build_conf_for_peer(session: AsyncSession, peer: Peer) -> tuple[Server, str] | None` — возвращает `(server, conf_text)` либо `None`, если сервер удалён.
  - `_create_peer_for_user` теперь возвращает `tuple[Peer, str]` — `(peer, conf)`. Строки `ip` и `label` из старого 3-кортежа не использовал никто (`configs.py:155` брал только `conf`).

- [ ] **Шаг 1: Написать падающий тест**

В новый файл `tests/test_config_delivery.py`:

```python
"""Доставка конфига юзеру: сборка текста, права, выбор формата."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, ServerStatus
from bot.services.crypto import encrypt


async def _user_with_peer(session: AsyncSession, *, tg_id: int):
    """Юзер с активной подпиской, устройством и одним пиром на READY-сервере."""
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 2
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="SRVPUB", server_endpoint="1.1.1.1:585",
    )
    server.location = "🇳🇱 Нидерланды"
    device = await repo.create_device(session, user_id=user.id, label="Телефон")
    await session.flush()
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device.id,
        label="Телефон", ip="10.8.0.2", public_key="PP",
        private_key_enc=encrypt("PRIVKEY"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    return user, server, device, peer


class TestBuildConf:
    async def test_conf_has_keys_and_endpoint(self, session: AsyncSession) -> None:
        """Собранный конфиг обязан содержать приватный ключ пира, его адрес и
        endpoint сервера — без любого из трёх он не подключится."""
        from bot.handlers.config_delivery import build_conf_for_peer

        _, server, _, peer = await _user_with_peer(session, tg_id=3001)

        got = await build_conf_for_peer(session, peer)

        assert got is not None
        srv, conf = got
        assert srv.id == server.id
        assert "PRIVKEY" in conf
        assert "10.8.0.2" in conf
        assert "SRVPUB" in conf
        assert "1.1.1.1:585" in conf

    async def test_missing_server_gives_none(self, session: AsyncSession) -> None:
        """Сервер удалили, а строка пира осталась: собирать нечего, и молча
        подсунуть пустой конфиг хуже, чем честно сказать «нет»."""
        from bot.handlers.config_delivery import build_conf_for_peer

        _, server, _, peer = await _user_with_peer(session, tg_id=3002)
        peer.server_id = 999999
        await session.flush()

        assert await build_conf_for_peer(session, peer) is None
```

- [ ] **Шаг 2: Прогнать тест и убедиться, что он падает**

Команда: `python -m pytest tests/test_config_delivery.py -v`
Ожидание: FAIL с `ModuleNotFoundError: No module named 'bot.handlers.config_delivery'`.

- [ ] **Шаг 3: Создать модуль с хелпером**

`bot/handlers/config_delivery.py`:

```python
"""Доставка конфига юзеру: сборка текста и выбор формата.

Раньше бот на каждый конфиг слал три сообщения подряд — файл, картинку QR и
ссылку, — и они занимали весь экран. Теперь юзер сначала выбирает, что ему
нужно, и получает только это.

Здесь же живёт единственная сборка текста конфига из строки пира: она нужна и
экранам выдачи, и кнопкам выбора формата, которые собирают конфиг заново уже
в момент нажатия.
"""
from __future__ import annotations

from aiogram import Router
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, Server
from bot.services import amnezia
from bot.services.crypto import decrypt

router = Router(name="config_delivery")


async def build_conf_for_peer(
    session: AsyncSession, peer: Peer
) -> tuple[Server, str] | None:
    """Собирает текст .conf по строке пира. None — сервер пира удалён.

    Конфиг не хранится: он выводится из приватного ключа пира и параметров
    сервера. Поэтому пересобрать его можно в любой момент, и передавать текст
    через callback_data (где 64 байта на всё) не требуется.
    """
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        return None
    params = amnezia.AmneziaParams.from_json(server.awg_params_json)
    conf = amnezia.build_peer_conf(
        peer_private_key=decrypt(peer.private_key_enc),
        peer_ip=peer.ip,
        server_public_key=server.server_public_key,
        endpoint=server.server_endpoint,
        params=params,
        dns=server.dns,
    )
    return server, conf
```

- [ ] **Шаг 4: Прогнать тест и убедиться, что он проходит**

Команда: `python -m pytest tests/test_config_delivery.py -v`
Ожидание: PASS, 2 теста.

- [ ] **Шаг 5: Вернуть пир из `_create_peer_for_user`**

В `bot/handlers/configs.py` поменять сигнатуру и возврат:

```python
) -> tuple[Peer, str]:
    """Создаёт peer на сервере и в БД. Возвращает (peer, conf).

    Пир возвращается целиком, а не его поля: вызывающему нужен `peer.id`, чтобы
    предложить юзеру выбрать формат конфига — экран выбора пересобирает конфиг
    по id уже в момент нажатия кнопки.
    ...
```

Хвост функции (было `return conf, ip, label`):

```python
    _server, conf = await build_conf_for_peer(session, peer)  # type: ignore[misc]
    return peer, conf
```

Импорт наверху `configs.py`:

```python
from bot.handlers.config_delivery import build_conf_for_peer
```

**Осторожно:** `build_conf_for_peer` здесь заведомо не вернёт `None` — сервер только что читали из БД в этой же функции. Разворачивать кортеж прямо в присваивании допустимо; проверка на `None` тут была бы мёртвым кодом.

Единственный вызывающий, `configs.py:155`, меняется с

```python
                conf, _ip, _ = await _create_peer_for_user(
```

на

```python
                peer, conf = await _create_peer_for_user(
```

и накопитель ниже — с `made.append((server, conf))` на `made.append((server, peer))`. Заодно правится аннотация и докстринг `provision_device_peers`:

```python
async def provision_device_peers(
    session: AsyncSession, user: User, device: "object"
) -> list[tuple[Server, Peer]]:
```

в докстринге последняя строка — `Возвращает [(server, peer), ...].`, и объявление накопителя — `made: list[tuple[Server, Peer]] = []`.

- [ ] **Шаг 6: Прогнать весь набор**

Команда: `python -m pytest --tb=short -p no:cacheprovider`
Ожидание: падений станет больше — вызывающие `provision_device_peers` в `devices.py` пока распаковывают `(server, conf)`. Это ожидаемо и чинится в задаче 3; здесь достаточно убедиться, что новых падений НЕТ в `tests/test_config_delivery.py` и что список падений ограничен `devices`-путями и `test_qrgen`. Зафиксируй список в отчёте.

**Если** падений вне `test_qrgen.py` не появилось — значит эти пути не покрыты тестами, так и напиши, не выдумывай.

- [ ] **Шаг 7: Коммит**

```bash
git add bot/handlers/config_delivery.py bot/handlers/configs.py tests/test_config_delivery.py
git commit -m "Конфиг: единая сборка текста из строки пира

Один и тот же блок «взять сервер, расшифровать ключ, собрать conf» лежал
скопированным в трёх местах. Экрану выбора формата он нужен четвёртым — и
пересобирать конфиг придётся уже в момент нажатия кнопки, потому что в
callback_data текст не помещается.

_create_peer_for_user отдаёт пир целиком: вызывающему нужен его id, а ip и
label из старого кортежа не читал никто."
```

---

### Задача 2: Экран выбора формата и три отправителя

**Файлы:**
- Изменить: `bot/handlers/config_delivery.py` (дописать), `bot/keyboards/inline/prefixes.py`, `bot/keyboards/inline/devices.py`, `bot/handlers/__init__.py`
- Тест: `tests/test_config_delivery.py`

**Интерфейсы:**
- Использует из задачи 1: `build_conf_for_peer(session, peer) -> tuple[Server, str] | None`.
- Отдаёт наружу:
  - `async def ask_config_format(chat_id: int, session: AsyncSession, peer: Peer) -> None` — шлёт одно сообщение с выбором.
  - `def config_format_kb(peer_id: int) -> InlineKeyboardMarkup` (в `bot/keyboards/inline/devices.py`).
  - `CB_CFG = "cfg"` (в `bot/keyboards/inline/prefixes.py`).
  - Хендлеры на `cfg:file:<peer_id>`, `cfg:qr:<peer_id>`, `cfg:link:<peer_id>`.

**Права.** Кнопку нажимает кто угодно, кто увидел сообщение, а `peer_id` в `callback_data` подделывается тривиально. Поэтому каждый из трёх хендлеров обязан проверить: пир принадлежит нажавшему ЛИБО нажавший — админ. Без этой проверки любой юзер вытянет чужой конфиг подстановкой чужого id. Это ровно тот риск, который спека этапа A1 называет первым в разделе про авторизацию выдачи.

- [ ] **Шаг 1: Написать падающие тесты**

Дописать в `tests/test_config_delivery.py`:

```python
class _FakeBot:
    """Ловит отправленное вместо реального Telegram."""

    def __init__(self) -> None:
        self.documents: list[tuple[int, bytes, str]] = []
        self.photos: list[tuple[int, bytes]] = []
        self.messages: list[tuple[int, str]] = []

    async def send_document(self, chat_id, document, caption=None, **kw) -> None:
        self.documents.append((chat_id, document.data, document.filename))

    async def send_photo(self, chat_id, photo, caption=None, **kw) -> None:
        self.photos.append((chat_id, photo.data))

    async def send_message(self, chat_id, text, **kw) -> None:
        self.messages.append((chat_id, text))


class _FakeFrom:
    def __init__(self, uid: int) -> None:
        self.id = uid


class _FakeMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = type("C", (), {"id": chat_id})()
        self.texts: list[str] = []

    async def answer(self, text: str, **kw) -> None:
        self.texts.append(text)


class _FakeCall:
    def __init__(self, data: str, uid: int, chat_id: int = 555) -> None:
        self.data = data
        self.from_user = _FakeFrom(uid)
        self.message = _FakeMessage(chat_id)
        self.answers: list[str] = []
        self.alerts: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        (self.alerts if show_alert else self.answers).append(text)


class TestConfigFormatAuth:
    async def test_stranger_cannot_pull_someones_config(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Подстановка чужого peer_id в кнопку не должна отдавать чужой конфиг."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        _, _, _, peer = await _user_with_peer(session, tg_id=3010)
        stranger = await repo.get_or_create_user(
            session, tg_id=3011, username="bad", full_name="Bad"
        )
        await session.flush()

        call = _FakeCall(f"cfg:file:{peer.id}", stranger.tg_id)
        await cd.cb_config_format(call, session)

        assert fake.documents == []
        assert call.alerts, "юзеру должно прийти явное «не найдено»"

    async def test_owner_gets_the_file(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3012)

        call = _FakeCall(f"cfg:file:{peer.id}", user.tg_id)
        await cd.cb_config_format(call, session)

        assert len(fake.documents) == 1
        _chat, data, filename = fake.documents[0]
        assert b"PRIVKEY" in data
        assert filename.endswith(".conf")

    async def test_admin_may_pull_any_config(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Админу конфиг юзера нужен для разбора жалоб — ему можно."""
        from bot.config import settings
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        _, _, _, peer = await _user_with_peer(session, tg_id=3013)
        admin_id = 999001
        monkeypatch.setattr(settings, "admin_ids", [admin_id])

        call = _FakeCall(f"cfg:file:{peer.id}", admin_id)
        await cd.cb_config_format(call, session)

        assert len(fake.documents) == 1


class TestConfigFormats:
    async def test_only_the_chosen_thing_is_sent(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Смысл всей задачи: одно нажатие — одно сообщение, а не три."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3014)

        await cd.cb_config_format(_FakeCall(f"cfg:link:{peer.id}", user.tg_id), session)

        assert fake.documents == []
        assert fake.photos == []
        assert len(fake.messages) == 1
        assert "vpn://" in fake.messages[0][1]

    async def test_revoked_peer_is_refused(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Отозванный конфиг на сервере уже не работает — отдавать его значит
        отправить юзера настраивать заведомо мёртвое подключение."""
        from bot.handlers import config_delivery as cd

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3015)
        peer.status = PeerStatus.REVOKED
        await session.flush()

        call = _FakeCall(f"cfg:file:{peer.id}", user.tg_id)
        await cd.cb_config_format(call, session)

        assert fake.documents == []
        assert call.alerts
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Команда: `python -m pytest tests/test_config_delivery.py -v`
Ожидание: FAIL с `AttributeError: module 'bot.handlers.config_delivery' has no attribute 'cb_config_format'` (и `bot`).

- [ ] **Шаг 3: Добавить префикс и клавиатуру**

В `bot/keyboards/inline/prefixes.py` после `CB_DEVICE`:

```python
CB_CFG = "cfg"     # выбор формата конфига (Этап B)
```

В `bot/keyboards/inline/devices.py` (импорт `CB_CFG` добавить к существующему импорту префиксов):

```python
def config_format_kb(peer_id: int) -> InlineKeyboardMarkup:
    """Чем прислать конфиг. Файл первой кнопкой — он нужен чаще всего и
    работает на любой платформе; QR и ссылка закрывают частные случаи
    (другое устройство рядом / этот же телефон)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Файлом",    callback_data=f"{CB_CFG}:file:{peer_id}")
    kb.button(text="📱 QR-кодом",  callback_data=f"{CB_CFG}:qr:{peer_id}")
    kb.button(text="🔗 Ссылкой",   callback_data=f"{CB_CFG}:link:{peer_id}")
    kb.adjust(1)
    return kb.as_markup()
```

Пакет `bot/keyboards/inline/__init__.py` собирает все имена обратно к себе, чтобы `from bot.keyboards.inline import ...` работал по всему проекту одинаково. Добавить туда `config_format_kb` в блок импорта из `bot.keyboards.inline.devices` (список отсортирован по алфавиту — вставить на место) и `CB_CFG` в блок импорта префиксов. Если в файле есть `__all__` — вписать оба имени и туда.

- [ ] **Шаг 4: Дописать модуль доставки**

В `bot/handlers/config_delivery.py` — импорты добавить к существующим:

```python
import re

from aiogram import F
from aiogram.types import BufferedInputFile, CallbackQuery

from bot.config import settings
from bot.db.models import PeerStatus
from bot.keyboards.inline import CB_CFG, config_format_kb
from bot.loader import bot
from bot.services.qrgen import conf_to_qr_png
from bot.texts import t
```

И тело:

```python
def _safe_filename_base(name: str) -> str:
    """Имя файла без эмодзи и флагов: «🇳🇱 Нидерланды» → «Нидерланды». Amnezia
    при импорте .conf называет конфиг по имени файла, поэтому файл — витрина."""
    cleaned = re.sub(r"[^\w\s.-]", "", name).strip()
    return cleaned or "vpn"


def _conf_filename(server: Server, label: str) -> str:
    from bot.handlers.configs import config_display_base

    base = _safe_filename_base(config_display_base(server))
    return f"{base}-{label}.conf".replace(" ", "_")


async def ask_config_format(
    chat_id: int, session: AsyncSession, peer: Peer
) -> None:
    """Одно сообщение с выбором вместо трёх сообщений подряд.

    Раньше бот вываливал файл, картинку QR и ссылку сразу — на телефоне это
    занимало весь экран, и юзер листал вверх, чтобы понять, что вообще
    произошло. Спрашиваем один раз, шлём только выбранное.
    """
    from bot.handlers.configs import config_display_base

    server = await repo.get_server(session, peer.server_id)
    where = config_display_base(server) if server else "?"
    await bot.send_message(
        chat_id,
        f"📦 <b>Конфиг «{peer.label}» · {where}</b>\n\n"
        "Как тебе его прислать?\n\n"
        "📄 <b>Файлом</b> — универсально: открой файл в AmneziaVPN.\n"
        "📱 <b>QR-кодом</b> — если настраиваешь <b>другое</b> устройство.\n"
        "🔗 <b>Ссылкой</b> — если настраиваешь <b>этот же</b> телефон.",
        reply_markup=config_format_kb(peer.id),
    )


@router.callback_query(F.data.startswith(f"{CB_CFG}:"))
async def cb_config_format(call: CallbackQuery, session: AsyncSession) -> None:
    """Присылает конфиг в выбранном виде.

    Права проверяем здесь, а не полагаемся на то, что кнопку видит только
    владелец: peer_id в callback_data подделывается тривиально, и без проверки
    любой юзер вытянул бы чужой конфиг подстановкой чужого номера.
    """
    _, kind, raw_id = call.data.split(":")
    peer = await repo.get_peer(session, int(raw_id))
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    is_admin = call.from_user.id in settings.admin_ids
    if not is_admin and (user is None or peer.user_id != user.id):
        # Тот же текст, что и у несуществующего пира: по разнице ответов чужой
        # id не должен отличаться от несуществующего.
        await call.answer("Не найдено", show_alert=True)
        return
    if peer.status != PeerStatus.ACTIVE:
        await call.answer(
            "Конфиг отозван — продли подписку, и он оживёт сам.", show_alert=True
        )
        return

    built = await build_conf_for_peer(session, peer)
    if built is None:
        await call.answer("Сервер недоступен", show_alert=True)
        return
    server, conf = built
    chat_id = call.message.chat.id

    if kind == "file":
        filename = _conf_filename(server, peer.label)
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(conf.encode("utf-8"), filename=filename),
            caption=(
                f"📄 <code>{filename}</code> — файл с настройками VPN. "
                "Открой AmneziaVPN → «＋» → выбери этот файл."
            ),
        )
    elif kind == "qr":
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(
                conf_to_qr_png(conf), filename=f"{peer.label}.png"
            ),
            caption=(
                "📱 Открой AmneziaVPN на <b>другом</b> устройстве → «＋» → "
                "«Сканировать QR-код» и наведи камеру на этот экран.\n"
                "<i>Настраиваешь этот же телефон? Возьми «🔗 Ссылкой».</i>"
            ),
        )
    else:
        from bot.handlers.configs import make_vpn_link

        link = await make_vpn_link(session, server, peer.label, conf)
        await bot.send_message(chat_id, t.vpn_link_msg.format(link=link))

    await call.answer("Отправил")
```

**Осторожно:** импорты `config_display_base` и `make_vpn_link` делаются внутри функций намеренно — `bot/handlers/configs.py` сам импортирует `build_conf_for_peer` из этого модуля, и импорт на верхнем уровне дал бы цикл.

- [ ] **Шаг 5: Зарегистрировать роутер**

В `bot/handlers/__init__.py` добавить `config_delivery` рядом с остальными роутерами, в том же стиле, что уже используется в файле. Порядок значения не имеет: префикс `cfg:` ни с чем не пересекается.

- [ ] **Шаг 6: Прогнать тесты**

Команда: `python -m pytest tests/test_config_delivery.py -v`
Ожидание: PASS, 7 тестов.

- [ ] **Шаг 7: Коммит**

```bash
git add bot/handlers/config_delivery.py bot/keyboards/inline/prefixes.py bot/keyboards/inline/devices.py bot/handlers/__init__.py tests/test_config_delivery.py
git commit -m "Конфиг: экран выбора формата вместо трёх сообщений

Бот вываливал файл, QR и ссылку сразу — на телефоне это занимало весь экран,
и юзер листал вверх, чтобы понять, что произошло. Теперь спрашиваем один раз.

Права проверяются в самом хендлере: peer_id в кнопке подделывается тривиально,
и без проверки чужой конфиг доставался бы подстановкой чужого номера. Ответ на
чужой id дословно совпадает с ответом на несуществующий."
```

---

### Задача 3: Перевести все выдачи конфига на экран выбора

**Файлы:**
- Изменить: `bot/handlers/configs.py` (удалить `_send_peer_artifacts`, поправить единственный внутренний вызов), `bot/handlers/devices.py` (четыре места), `bot/handlers/admin/user_items.py` (одно место), `bot/texts/ru.py` (текст `device_created`)
- Тест: `tests/test_config_delivery.py`

**Интерфейсы:**
- Использует из задачи 2: `ask_config_format(chat_id, session, peer)`.
- После этой задачи функции `_send_peer_artifacts` в проекте не существует.

Места выдачи (все найдены через `grep -rn "_send_peer_artifacts" bot/`):

| Место | Что было | Что станет |
|---|---|---|
| `configs.py:543` | админ создал пир юзеру | `ask_config_format` по созданному пиру |
| `devices.py:224` | создано устройство, N локаций | по одному экрану выбора на локацию |
| `devices.py:250` | открытие устройства дозалило локацию | то же |
| `devices.py:319` | кнопка «конфиг локации» | `ask_config_format` |
| `devices.py:352` | кнопка «прислать все» | по экрану на каждый активный пир |
| `admin/user_items.py:111` | админ шлёт себе конфиг юзера | `ask_config_format` |

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_config_delivery.py`:

```python
class TestNoMoreTripleSend:
    async def test_device_config_button_asks_instead_of_dumping(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Кнопка «конфиг» у устройства больше не вываливает три сообщения:
        приходит один вопрос с кнопками."""
        from bot.handlers import config_delivery as cd
        from bot.handlers import devices as devices_h

        fake = _FakeBot()
        monkeypatch.setattr(cd, "bot", fake)
        user, _, _, peer = await _user_with_peer(session, tg_id=3020)

        call = _FakeCall(f"dev:cfg:{peer.id}", user.tg_id)
        await devices_h.cb_dev_cfg(call, session)

        assert fake.documents == []
        assert fake.photos == []
        assert len(fake.messages) == 1
        assert "Как тебе его прислать?" in fake.messages[0][1]

    async def test_artifacts_helper_is_gone(self) -> None:
        """_send_peer_artifacts удалён: пока он существует, к нему легко
        вернуться мимо экрана выбора."""
        from bot.handlers import configs as configs_h

        assert not hasattr(configs_h, "_send_peer_artifacts")
```

- [ ] **Шаг 2: Прогнать тест и убедиться, что он падает**

Команда: `python -m pytest tests/test_config_delivery.py::TestNoMoreTripleSend -v`
Ожидание: FAIL — первый тест увидит документ и фото, второй увидит существующий атрибут.

- [ ] **Шаг 3: Убрать `_send_peer_artifacts` и перевести вызывающих**

Удалить `bot/handlers/configs.py:200-236` целиком. Функции `_safe_filename_base` в `configs.py` (строки 193-197) остаётся, только если её читает кто-то ещё, — проверь `grep -rn "_safe_filename_base" bot/` и удали, если вызывающих не осталось.

`configs.py:543` (админ создал пир юзеру) — заменить блок отправки на:

```python
        await ask_config_format(message.chat.id, session, peer)
```

взяв `peer` из возврата `_create_peer_for_user` (задача 1 уже сделала возврат пиром).

`devices.py:224` и `devices.py:250` — цикл по `made` теперь отдаёт пиры:

```python
    for _server, peer in made:
        await ask_config_format(message.chat.id, session, peer)
```

и соответственно во втором месте — `call.message.chat.id`.

`devices.py:319` (`cb_dev_cfg`) — весь блок сборки конфига и отправки заменяется на:

```python
    await ask_config_format(call.message.chat.id, session, peer)
    await call.answer()
```

Проверки выше (пир существует, принадлежит юзеру, активен) НЕ удалять: без них юзер получит вопрос про чужой конфиг, и хотя нажатие потом отобьётся, показывать чужую метку и локацию всё равно нельзя.

`devices.py:352` (`cb_dev_send`) — цикл по активным пирам устройства:

```python
    for peer in peers:
        await ask_config_format(call.message.chat.id, session, peer)
    await call.answer()
```

Сборку конфига внутри цикла убрать целиком — она теперь не нужна.

`admin/user_items.py:111` — так же:

```python
    from bot.handlers.config_delivery import ask_config_format
    await ask_config_format(call.message.chat.id, session, peer)
    await call.answer("Спросил формат")
```

Импорт в `devices.py` — добавить `ask_config_format` к существующему импорту из `bot.handlers.configs`, переведя его на новый модуль; `_send_peer_artifacts` из списка импорта убрать.

- [ ] **Шаг 4: Поправить текст после создания устройства**

В `bot/texts/ru.py` текст `device_created` обещает «Выше я прислал всё для подключения: файл, QR-код и ссылку» — это стало неправдой. Заменить первый абзац на:

```python
    device_created = (
        "🎉 <b>Устройство «{label}» добавлено!</b>\n\n"
        "Выше спросил, в каком виде прислать конфиг — выбери удобный. Дальше:\n\n"
```

Остальные пункты текста оставить как есть, но проверить, что шаг «2️⃣ Добавь конфиг» не ссылается на «файл выше» в единственном варианте; если ссылается — переформулировать так, чтобы годилось для любого из трёх форматов.

- [ ] **Шаг 5: Прогнать весь набор**

Команда: `python -m pytest --tb=short -p no:cacheprovider`
Ожидание: **2 failed** (только `test_qrgen.py`), остальное PASS. Если падает что-то ещё — чинить здесь же, а не откладывать.

- [ ] **Шаг 6: Коммит**

```bash
git add bot/handlers/configs.py bot/handlers/devices.py bot/handlers/admin/user_items.py bot/texts/ru.py tests/test_config_delivery.py
git commit -m "Конфиг: все шесть путей выдачи спрашивают формат

_send_peer_artifacts удалена целиком, а не оставлена «на всякий случай»:
пока такая функция есть, к ней возвращаются мимо экрана выбора.

Текст после создания устройства больше не обещает файл, QR и ссылку разом —
он обещал то, чего теперь не происходит."
```

---

### Задача 4: «Последний трафик» у обхода БС

Сервер обхода не отдаёт времени последнего контакта вовсе — в его ответе на `ctl -op list` только `down_bytes`, `up_bytes`, `expires_at`, `device_id`, `vk_hash`, `ports`, `is_deactivated` (проверено по `types.go` на klopas). Поэтому «последний раз использовался» выводится из прироста счётчиков, которые планировщик опрашивает каждые 5 минут: вырос — значит трафик шёл.

**Файлы:**
- Изменить: `bot/db/models.py` (поле `WdttAccess.last_seen_at`), `bot/services/scheduler.py:455-462` (секция 3a), `bot/handlers/servers/bypass.py:107-125` (серверная карточка), `bot/handlers/admin/user_items.py:163-172` (карточка обхода в карточке юзера)
- Тест: `tests/test_bypass_lastseen.py`

**Интерфейсы:**
- Отдаёт наружу: `WdttAccess.last_seen_at: Mapped[datetime | None]` — момент, когда планировщик В ПОСЛЕДНИЙ РАЗ увидел прирост трафика. `None` — прироста не видели ни разу.

**Миграция не нужна руками:** `bot/db/migrate.py` сам добавит nullable-колонку в существующую таблицу при старте. Поле обязано быть nullable — `ADD COLUMN NOT NULL` без дефолта на непустой таблице невозможен.

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_bypass_lastseen.py`:

```python
"""«Последний трафик» обхода БС: выводится из прироста счётчиков.

Сервер обхода времени последнего контакта не отдаёт — только накопленные
байты. Планировщик опрашивает их каждые 5 минут, и рост между опросами и есть
единственный доступный признак «им пользовались».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import PeerStatus, ServerStatus, WdttAccess
from bot.services.crypto import encrypt


async def _access(session: AsyncSession, *, tg_id: int) -> WdttAccess:
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    server = await repo.create_server(
        session, name="s", host="1.1.1.1", wg_port=585,
        owner_tg_id=tg_id, status=ServerStatus.READY,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    await session.flush()
    return await repo.create_wdtt_access(
        session, server_id=server.id, user_id=user.id, label="Телефон",
        uri_enc=encrypt("wdtt://x"), password_enc=encrypt("PASS"),
        expires_at=None, platform="android",
    )


class TestLastSeen:
    async def test_growth_marks_seen(self, session: AsyncSession) -> None:
        from bot.services.scheduler import apply_wdtt_traffic

        acc = await _access(session, tg_id=4001)
        assert acc.last_seen_at is None

        apply_wdtt_traffic(acc, raw=1000)

        assert acc.last_seen_at is not None
        assert acc.traffic_used_bytes == 1000

    async def test_no_growth_keeps_old_time(self, session: AsyncSession) -> None:
        """Тик без прироста не должен освежать время: иначе «последний трафик»
        всегда показывал бы «только что» и не значил бы ничего."""
        from bot.services.scheduler import apply_wdtt_traffic

        acc = await _access(session, tg_id=4002)
        apply_wdtt_traffic(acc, raw=1000)
        was = acc.last_seen_at
        acc.last_seen_at = was - timedelta(hours=3)
        stale = acc.last_seen_at

        apply_wdtt_traffic(acc, raw=1000)   # счётчик не изменился

        assert acc.last_seen_at == stale

    async def test_counter_reset_counts_as_traffic(
        self, session: AsyncSession
    ) -> None:
        """Сервер обхода перезапустили, счётчик обнулился. Накопление это уже
        умеет пережить; время последнего трафика тоже обязано обновиться —
        байты после сброса реальны."""
        from bot.services.scheduler import apply_wdtt_traffic

        acc = await _access(session, tg_id=4003)
        apply_wdtt_traffic(acc, raw=5000)
        acc.last_seen_at = acc.last_seen_at - timedelta(hours=3)
        stale = acc.last_seen_at

        apply_wdtt_traffic(acc, raw=10)     # сброс: raw меньше прошлого

        assert acc.last_seen_at != stale
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Команда: `python -m pytest tests/test_bypass_lastseen.py -v`
Ожидание: FAIL с `ImportError: cannot import name 'apply_wdtt_traffic'`.

- [ ] **Шаг 3: Добавить поле в модель**

В `bot/db/models.py`, в класс `WdttAccess`, рядом с `traffic_used_bytes`:

```python
    # Когда планировщик в последний раз увидел ПРИРОСТ трафика. Сервер обхода
    # времени последнего контакта не отдаёт вовсе, поэтому «пользовались» мы
    # выводим из роста счётчиков — точность равна периоду тика (5 мин).
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Шаг 4: Вынести накопление в функцию и проставлять время**

В `bot/services/scheduler.py` завести функцию рядом с секцией 3a:

```python
def apply_wdtt_traffic(access: WdttAccess, *, raw: int) -> None:
    """Копит трафик обхода и отмечает время, если он вырос.

    Отдельной функцией, а не строчками внутри тика: это единственное место, где
    решается вопрос «пользовались обходом или нет», и его надо уметь проверить
    тестом, не поднимая весь планировщик.

    Прирост определяется тем же accumulate_traffic, что и у пиров, — он же
    переживает сброс счётчика при рестарте сервера обхода. Считаем сброс
    трафиком: байты после рестарта настоящие.
    """
    before = access.traffic_used_bytes
    access.traffic_used_bytes, access.traffic_last_raw_bytes = (
        amnezia.accumulate_traffic(
            access.traffic_used_bytes, access.traffic_last_raw_bytes, raw
        )
    )
    if access.traffic_used_bytes > before:
        access.last_seen_at = datetime.now(timezone.utc)
```

И заменить тело цикла в секции 3a (`scheduler.py:455-462`) на:

```python
                    for acc in accs:
                        r = by_pw.get(decrypt(acc.password_enc))
                        if r is None:
                            continue
                        apply_wdtt_traffic(
                            acc,
                            raw=int(r.get("down_bytes", 0)) + int(r.get("up_bytes", 0)),
                        )
```

- [ ] **Шаг 5: Прогнать тесты**

Команда: `python -m pytest tests/test_bypass_lastseen.py -v`
Ожидание: PASS, 3 теста.

- [ ] **Шаг 6: Показать время в двух карточках**

Завести хелпер форматирования в `bot/utils/timefmt.py` — рядом с уже существующими функциями времени:

```python
def fmt_ago(moment: datetime | None) -> str:
    """«12 мин назад» для карточек. None — события не было ни разу.

    Шкала та же, что у хендшейка пира в метриках сервера (сек/мин/ч/д), чтобы
    админ читал обе карточки одинаково.
    """
    if moment is None:
        return "не видели ни разу"
    delta = int((datetime.now(timezone.utc) - as_utc(moment)).total_seconds())
    if delta < 60:
        return f"{max(delta, 0)} сек назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    return f"{delta // 86400} д назад"
```

**Осторожно:** `as_utc` обязателен — SQLite вернёт `last_seen_at` без tzinfo, и вычитание из aware-`now()` иначе падает с `TypeError`. Этот капкан в проекте ловили уже дважды.

В `bot/handlers/servers/bypass.py`, в `cb_server_wdtt_open`, добавить строку после трафика:

```python
        f"• 📊 Трафик: {amnezia.fmt_bytes(access.traffic_used_bytes)}\n"
        f"• 🕐 Последний трафик: {fmt_ago(access.last_seen_at)}",
```

То же самое — в `bot/handlers/admin/user_items.py`, в `cb_panel_user_bypass_open`, чтобы карточка обхода читалась одинаково с обеих сторон.

**Название именно «Последний трафик», а не «хендшейк»:** хендшейка тут нет, есть факт, что байты шли. Назвать иначе значит обещать точность, которой у нас нет.

- [ ] **Шаг 7: Прогнать весь набор**

Команда: `python -m pytest --tb=short -p no:cacheprovider`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 8: Коммит**

```bash
git add bot/db/models.py bot/services/scheduler.py bot/handlers/servers/bypass.py bot/handlers/admin/user_items.py bot/utils/timefmt.py tests/test_bypass_lastseen.py
git commit -m "Обход БС: когда им пользовались в последний раз

Сервер обхода времени последнего контакта не отдаёт — в ответе ctl только
байты, срок и device_id. Выводим из прироста счётчиков, которые планировщик и
так опрашивает каждые 5 минут: вырос — значит трафик шёл.

Поэтому и называется «последний трафик», а не «хендшейк»: обещать точность,
которой у нас нет, хуже, чем не показывать ничего.

Накопление вынесено в отдельную функцию — это единственное место, где решается
«пользовались или нет», и его надо уметь проверить, не поднимая весь тик."
```

---

### Задача 5: Кнопки перехода к пиру и обходу

Из карточки устройства и карточки обхода в админ-панели админ должен попадать в серверную карточку того же объекта — туда, где есть трафик, хендшейк и управление.

**Файлы:**
- Изменить: `bot/keyboards/inline/admin.py` (`admin_user_device_card_kb`, `admin_user_bypass_card_kb`)
- Тест: `tests/test_admin_nav.py` (создать)

**Интерфейсы:**
- Цели перехода уже существуют и новых хендлеров не требуют:
  - карточка пира на сервере — `adm:peer:<peer_id>` (`bot/handlers/servers/peers.py:77`);
  - карточка обхода на сервере — `srv:wopen:<access_id>` (`bot/handlers/servers/bypass.py:107`).
- `admin_user_device_card_kb` получает новый параметр `configs`, уже содержащий `(peer_id, loc_label)` — переиспользуется как есть.

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_admin_nav.py`:

```python
"""Переходы из карточек юзера в серверные карточки объектов."""
from __future__ import annotations

from bot.keyboards.inline import admin_user_bypass_card_kb, admin_user_device_card_kb


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestJumpToServerCard:
    def test_device_card_links_to_peer(self) -> None:
        """Из устройства юзера админ должен попадать в карточку пира на
        сервере — там трафик, хендшейк и отзыв."""
        kb = admin_user_device_card_kb(
            device_id=7, user_id=3, page=0, configs=[(42, "🇳🇱 Нидерланды")]
        )

        assert "adm:peer:42" in _callbacks(kb)

    def test_bypass_card_links_to_bypass(self) -> None:
        kb = admin_user_bypass_card_kb(
            access_id=15, user_id=3, page=0, is_active=True, server_id=9
        )

        assert "srv:wopen:15" in _callbacks(kb)

    def test_revoked_bypass_still_links(self) -> None:
        """Отозванный обход тоже надо уметь открыть: разбор жалобы начинается
        как раз с того, что доступ уже погас."""
        kb = admin_user_bypass_card_kb(
            access_id=15, user_id=3, page=0, is_active=False, server_id=9
        )

        assert "srv:wopen:15" in _callbacks(kb)
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Команда: `python -m pytest tests/test_admin_nav.py -v`
Ожидание: FAIL — первые два по отсутствию callback'ов, третий по `TypeError` (у `admin_user_bypass_card_kb` пока нет параметра `server_id`).

- [ ] **Шаг 3: Добавить кнопки**

В `bot/keyboards/inline/admin.py`, в `admin_user_device_card_kb`, после кнопок «📥 <локация>»:

```python
    # Провал в серверную карточку пира: там трафик, последний хендшейк и отзыв.
    # Кнопка отдельная от «📥 <локация>» — та шлёт конфиг, эта показывает пир.
    for peer_id, loc in (configs or []):
        kb.button(text=f"🔍 К пиру · {loc}", callback_data=f"{CB_ADMIN}:peer:{peer_id}")
```

В `admin_user_bypass_card_kb` добавить параметр и кнопку:

```python
def admin_user_bypass_card_kb(
    access_id: int, user_id: int, page: int, is_active: bool = True,
    server_id: int | None = None,
) -> InlineKeyboardMarkup:
    ...
    if server_id is not None:
        kb.button(text="🔍 К обходу на сервере", callback_data=f"{CB_SERVERS}:wopen:{access_id}")
```

`server_id` необязателен намеренно: без него кнопка просто не рисуется, и старый вызывающий не ломается, пока его не поправили.

Импорт `CB_SERVERS` в `bot/keyboards/inline/admin.py` добавить, если его там ещё нет.

Передать `server_id` из `bot/handlers/admin/user_items.py:170`:

```python
        reply_markup=admin_user_bypass_card_kb(
            access.id, user_id, page, is_active=access.status == PeerStatus.ACTIVE,
            server_id=access.server_id,
        ),
```

**Проверь фактическую сигнатуру вызова по месту** — приведённый фрагмент показывает только добавляемый аргумент, остальные оставь как есть.

- [ ] **Шаг 4: Прогнать тесты**

Команда: `python -m pytest tests/test_admin_nav.py -v`
Ожидание: PASS, 3 теста.

- [ ] **Шаг 5: Проверить, что переход не уводит в тупик**

Прочитай кнопки «назад» у обеих целевых карточек (`bot/handlers/servers/peers.py:77-126` и `bot/keyboards/inline/servers.py:77-82`). Они ведут в списки сервера, а не обратно в карточку юзера — то есть админ, провалившись, вернётся не туда, откуда пришёл.

Чинить это в рамках задачи НЕ надо: сквозная история навигации потребовала бы тащить `user_id` и страницу через все серверные экраны. Опиши поведение в отчёте как известное и осознанное — решение, оставлять ли так, за Владом.

- [ ] **Шаг 6: Прогнать весь набор**

Команда: `python -m pytest --tb=short -p no:cacheprovider`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 7: Коммит**

```bash
git add bot/keyboards/inline/admin.py bot/handlers/admin/user_items.py tests/test_admin_nav.py
git commit -m "Админка: переход из карточки юзера в серверную карточку

Разбор жалобы начинался с карточки юзера, а трафик и хендшейк лежат в
серверной — админ искал тот же объект руками через список серверов.

Кнопка отдельная от «📥 <локация>»: та шлёт конфиг, эта показывает пир."
```

---

## Порядок и зависимости

1. Задача 1 → задача 2 → задача 3. Строго последовательно: каждая следующая использует интерфейс предыдущей.
2. Задачи 4 и 5 ни от чего не зависят и друг с другом не пересекаются — их можно делать параллельно с ветки после задачи 3 (или до неё, файлы не пересекаются с задачами 1-3, кроме `admin/user_items.py`, который трогают задачи 3, 4 и 5 в разных местах).

**Из-за пересечения по `bot/handlers/admin/user_items.py` задачи 3, 4 и 5 не запускать одновременно разными агентами.**

## Что сознательно не делается

- **Не трогается сервер обхода (Go-исходник на klopas).** Настоящий «последний контакт» потребовал бы правки второго продукта, пересборки бинаря и рестарта обхода у живых юзеров. Прирост трафика с точностью 5 минут отвечает на тот же вопрос админа.
- **Выбор формата не запоминается.** Соблазн «он всегда берёт файл, давай слать сразу файл» ломает главный сценарий: формат зависит не от привычки, а от того, какое устройство юзер настраивает прямо сейчас.
- **Не добавляется кнопка «прислать всё».** Она вернула бы ровно ту стену сообщений, ради устранения которой задача и делается.
- **Сквозная навигация «назад в карточку юзера» из серверных карточек не делается** — потребовала бы тащить `user_id` и страницу через все серверные экраны. Отмечено в задаче 5 как известное поведение.
- **Не трогается этап C (смена сервера)** — у него свой план.
