# Этап C: смена сервера у конфига — план реализации

> **Для агентов:** ОБЯЗАТЕЛЬНЫЙ СУБ-СКИЛЛ: используй superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans, чтобы выполнять план задача за задачей. Шаги помечены чекбоксами (`- [ ]`).

**Цель:** Конфиг можно переселить на другой сервер — юзер сам («сервер лёг / тормозит»: кнопка → локация → сервер → подтверждение) и админ из карточки пира (бот подбирает сервер сам), причём старый конфиг доживает сутки, а у каждого сервера появляется заданный руками потолок конфигов.

**Архитектура:** Переезд — это создание НОВОГО пира на целевом сервере и пометка старого «доживает до такого-то момента»; порядок именно такой, чтобы упавшее создание не оставило юзера без связи. Снятие дожившего конфига делает отдельная секция планировщика через функцию сервиса (инлайн в тике её не покрыть тестом). Потолок конфигов на сервере — ровно тот же приём, что уже работает для обхода БС (`wdtt_max_accesses`): цифра руками, живую нагрузку не меряем.

**Стек:** Python 3.13, aiogram 3, SQLAlchemy 2 (async), SQLite (aiosqlite), pytest (asyncio_mode=auto), loguru.

## Глобальные ограничения

- Прогон: `python -m pytest` из `/root/myvpn-bot` (termux-питон). Базовая линия на 3.08.2026 — **254 passed, 2 failed**; оба падения в `tests/test_qrgen.py` (`ModuleNotFoundError: No module named 'PIL'`), это предсуществующая поломка окружения: чинить её не надо, трогать эти тесты нельзя.
- Функции репозитория делают `flush()`, но НЕ `commit()`. Коммит — на вызывающем.
- Событие журнала пишется в той же транзакции, что и само действие: откатилось действие — откатилась запись.
- `target_user_id` в журнале — это `User.id`, не `tg_id`.
- Время: в БД UTC, человеку ВЕЗДЕ МСК через `bot.utils.timefmt.fmt_msk`. SQLite отдаёт `DateTime(timezone=True)` БЕЗ tzinfo — перед арифметикой в Python гнать через `bot.utils.timefmt.as_utc`, а сравнения дат по возможности оставлять в SQL.
- Миграции автоматические (`bot/db/migrate.py` сравнивает модели со схемой): добавлять можно только nullable-колонки или колонки с `server_default`. Никакого `PRAGMA user_version`.
- **`PRAGMA foreign_keys` не включать** — выключенные FK здесь осознанный дизайн (см. README, раздел про FK, и `tests/test_user_wipe.py`).
- Комментарии и сообщения коммитов — на русском, объясняют «почему», а не «что».
- Никаких новых зависимостей и новых env-переменных.
- Тексты для юзера — человеческим языком, без техножаргона; сырой текст исключений юзеру не показывается (пугает и может раскрыть host сервера).
- `callback_data` — не длиннее 64 байт, и всё, что в неё едет, подделывается тривиально: **права проверяются в самом хендлере**, ответ на чужой id дословно совпадает с ответом на несуществующий (урок Этапа B).

## Решения, принятые Владом (дизайн-документ, строки 213–236, плюс разбор 3.08.2026)

- **Бесшовного переезда не бывает.** У другого сервера свой публичный ключ, адрес и endpoint — юзер обязан заменить файл в приложении, и бот пишет об этом прямым текстом.
- **Старый конфиг живёт ещё сутки**, потом бот сам снимает его со старого сервера. Мгновенный обрыв отвергнут: юзер мог нажать кнопку из любопытства и остался бы без интернета, пока не заменит файл.
- **Потолок конфигов на сервер задаётся руками**, по образцу `wdtt_max_accesses`; живую нагрузку бот не измеряет, чтобы выбор у юзера не плавал от часа к часу.
- **Потолок действует ВЕЗДЕ** — и при переезде, и при выдаче нового устройства: потолок, который обходится кнопкой «добавить устройство», не потолок.
- **Не чаще раза в сутки на конфиг** — иначе серверы будут дёргать каждые пять минут. На админа ограничение не распространяется: он разгружает сервер, а не капризничает.
- **Сценарий админа:** в карточке пира на сервере админ отвязывает конфиг, бот сам подбирает другой доступный сервер, переселяет и присылает юзеру новый конфиг с пояснением.
- **Сценарий юзера:** в карточке устройства кнопка «Сменить сервер» → локации → серверы → подтверждение → новый конфиг.

## Что здесь считается «доступным сервером»

Рабочий (`READY`), не приватный для этого юзера (гейт `repo.list_ready_servers(for_user=...)`: приватные видят только админы и «друзья» `is_vip`), не упёршийся в потолок конфигов и не тот, где пир живёт сейчас.

**Про «уехать в другую страну».** Дизайн-документ разрешает переезд и в чужую локацию. В коде устройство УЖЕ держит по конфигу на каждую READY-локацию (`provision_device_peers` дозакидывает недостающие при открытии карточки), поэтому переезд в страну, где у устройства конфиг уже есть, дал бы там два конфига, а в родной локации — ни одного (и следующий заход в карточку устройства всё равно выдал бы новый). Поэтому отбор ниже исключает локации, где у этого устройства уже есть свой активный конфиг, — механика переезда в другую страну остаётся рабочей (сработает, если конфига там нет), но практически юзер выбирает другой сервер внутри своей локации, ровно тот случай, ради которого кнопка и делается. **Это единственное место, где план сузил дизайн-документ; Владу сказать при сдаче.**

## Раскладка файлов

| Файл | Что делает |
|---|---|
| `bot/db/models.py` (править) | `Server.max_peers`, `Peer.grace_until`, `Peer.moved_at`, `AuditAction.CONFIG_MOVED`. |
| `bot/db/repo/common.py` (править) | `has_free_wg_slot` — есть ли на сервере место под ещё один конфиг. |
| `bot/db/repo/peers.py` (править) | `start_peer_grace`, `list_grace_expired_peers`. |
| `bot/db/repo/__init__.py` (править) | Реэкспорт трёх новых функций (списком, не `import *`). |
| `bot/services/relocate.py` (создать, ~200 строк) | Ядро переезда: кулдаун, отбор серверов, сам переезд, конец грейса, «какие конфиги показывать юзеру». |
| `bot/handlers/configs.py` (править) | Потолок в `provision_device_peers`; `_create_peer_for_user` умеет не писать «выдан конфиг» (переезд пишет своё событие). |
| `bot/services/revive.py` (править) | Не воскрешать переехавшие пиры при продлении подписки. |
| `bot/services/scheduler.py` (править) | Секция 2d: снять конфиги, у которых сутки грейса вышли. |
| `bot/handlers/servers/peers.py` (править) | Админ: лимит конфигов сервера + кнопка «Переселить» в карточке пира. |
| `bot/handlers/config_move.py` (создать, ~230 строк) | Юзерские экраны переезда: какой конфиг → локация → сервер → подтверждение → выполнение. |
| `bot/handlers/devices.py` (править) | Кнопка «Сменить сервер» и скрытие доживающего конфига из карточки устройства. |
| `bot/handlers/__init__.py` (править) | Регистрация роутера `config_move`. |
| `bot/keyboards/inline/servers.py` (править) | Кнопки «Лимит конфигов» и «Переселить». |
| `bot/keyboards/inline/devices.py` (править) | Клавиатуры четырёх экранов переезда + кнопка в карточке устройства. |
| `bot/states/install.py` (править) | `ServerEditStates.peer_limit`. |
| `bot/handlers/admin/audit.py` (править) | Человеческое название события «Конфиг переехал». |
| `bot/texts/ru.py` (править) | Тексты подтверждения, результата и уведомления от админа. |
| `tests/test_distribution.py` (править) | Потолок конфигов при выдаче устройства. |
| `tests/test_relocate.py` (создать) | Кулдаун, отбор серверов, переезд, грейс, ревайв. |
| `tests/test_admin_nav.py` (править) | Кнопки админки на месте. |
| `README.md` (править) | Новые поля, кнопки и поведение. |

---

### Задача 1: Потолок конфигов на сервере

Сейчас ёмкость есть только у обхода БС (`Server.wdtt_max_accesses`, `bot/db/models.py:169-172`), а WG-конфиги выдаются, пока не кончатся IP в `/24`. Заводим симметричное поле и учитываем его в единственном месте, где создаются пиры устройства, — `provision_device_peers` (`bot/handlers/configs.py:129-165`). Переезд (задача 3) будет спрашивать ту же функцию.

**Файлы:**
- Изменить: `bot/db/models.py:169-172` (рядом с `wdtt_max_accesses`), `bot/db/repo/common.py:51-70`, `bot/db/repo/__init__.py:27-35` и `__all__`, `bot/handlers/configs.py:129-165`
- Тест: `tests/test_distribution.py`

**Интерфейсы:**
- Отдаёт наружу:
  - `Server.max_peers: int | None` — NULL безлимит, 0 закрывает новую выдачу.
  - `def repo.has_free_wg_slot(server: Server, load: dict[int, int]) -> bool` — чистая функция, нагрузку вызывающий считает одним запросом `repo.count_active_peers_by_server`.

- [ ] **Шаг 1: Написать падающий тест**

В конец `tests/test_distribution.py` добавить класс. Хелперы `_make_user`, `_make_server`, `_add_active_peers`, `_fake_create_peer` уже есть в этом файле (строки 26-85):

```python
class TestPeerCapacity:
    """Потолок конфигов на сервере (Этап C): Server.max_peers. Зеркало того, что
    уже проверено для обхода БС в TestWdttCapacity."""

    async def test_full_server_skipped_next_in_location_used(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _make_user(session)
        other = await _make_user(session, tg_id=222)
        full = await _make_server(session, name="nl1", location="🇳🇱 Нидерланды")
        free = await _make_server(session, name="nl2", location="🇳🇱 Нидерланды")
        full.max_peers = 1
        await _add_active_peers(session, full, other, 1)  # место занято
        device = await repo.create_device(session, user_id=user.id, label="phone")

        calls: list[int] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create_peer(calls))
        made = await configs.provision_device_peers(session, user, device)

        # Заполненный сервер не берём, хотя он наименее загруженный он или нет —
        # решает потолок, а не сравнение нагрузок.
        assert calls == [free.id]
        assert [srv.id for srv, _ in made] == [free.id]

    async def test_zero_limit_closes_issuance_for_whole_location(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """max_peers=0 — «сюда больше никого»: локация из выдачи выпадает
        целиком, а не подсовывает переполненный сервер."""
        user = await _make_user(session)
        closed = await _make_server(session, name="nl1", location="🇳🇱 Нидерланды")
        closed.max_peers = 0
        device = await repo.create_device(session, user_id=user.id, label="phone")

        calls: list[int] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create_peer(calls))
        made = await configs.provision_device_peers(session, user, device)

        assert calls == [] and made == []

    async def test_null_limit_is_unlimited(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Старые серверы (поле NULL после миграции) продолжают выдавать конфиги."""
        user = await _make_user(session)
        other = await _make_user(session, tg_id=333)
        srv = await _make_server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _add_active_peers(session, srv, other, 50)
        device = await repo.create_device(session, user_id=user.id, label="phone")

        calls: list[int] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create_peer(calls))
        made = await configs.provision_device_peers(session, user, device)

        assert calls == [srv.id] and len(made) == 1

    def test_has_free_wg_slot_counts_only_this_server(self) -> None:
        """Нагрузка приходит словарём по всем серверам разом — функция обязана
        смотреть только на свой id, иначе один загруженный сервер закрыл бы все."""
        class S:  # лёгкая заглушка вместо ORM-объекта
            pass
        s = S()
        s.id, s.max_peers = 7, 2

        assert repo.has_free_wg_slot(s, {7: 1, 8: 99}) is True
        assert repo.has_free_wg_slot(s, {7: 2}) is False
        assert repo.has_free_wg_slot(s, {}) is True
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Команда: `python -m pytest tests/test_distribution.py::TestPeerCapacity -v`
Ожидание: FAIL — `AttributeError: 'Server' object has no attribute 'max_peers'` и `AttributeError: module 'bot.db.repo' has no attribute 'has_free_wg_slot'`.

- [ ] **Шаг 3: Добавить поле в модель**

В `bot/db/models.py`, сразу после блока `wdtt_max_accesses` (строка 172):

```python
    # Ёмкость по VPN-конфигам (Этап C): максимум АКТИВНЫХ пиров на сервере.
    # NULL — без лимита, 0 — новая выдача закрыта (выданные конфиги продолжают
    # работать). Цифру ставит админ руками: живую нагрузку не меряем, иначе
    # выбор сервера у юзера плавал бы от часа к часу. Действует ВЕЗДЕ, где
    # создаётся пир, — и при переезде, и при выдаче нового устройства: потолок,
    # который обходится кнопкой «добавить устройство», не потолок.
    max_peers: Mapped[int | None] = mapped_column(Integer)
```

- [ ] **Шаг 4: Добавить функцию в репозиторий**

В `bot/db/repo/common.py`, после `count_active_peers_by_server` (строка 59):

```python
def has_free_wg_slot(server: Server, load: dict[int, int]) -> bool:
    """Есть ли на сервере место под ещё один WG-конфиг (`Server.max_peers`:
    NULL — безлимит, 0 — выдача закрыта).

    Функция чистая, а нагрузку вызывающий считает одним запросом
    (`count_active_peers_by_server`): подбор сервера идёт по списку, и запрос
    на каждый сервер дал бы N+1. Ровно так же устроена ёмкость обхода БС в
    `handlers/wdtt._wdtt_location_groups`.
    """
    if server.max_peers is None:
        return True
    return load.get(server.id, 0) < server.max_peers
```

В `bot/db/repo/__init__.py` добавить `has_free_wg_slot` в импорт из `bot.db.repo.common` (строки 27-35) и в `__all__` (по алфавиту, между `group_by_location` и `is_support_reply_from_user`).

- [ ] **Шаг 5: Учесть потолок при выдаче устройства**

В `bot/handlers/configs.py`, в цикле по серверам группы (строка 150), первой строкой тела:

```python
        for server in sorted(group, key=lambda s: load.get(s.id, 0)):
            if not repo.has_free_wg_slot(server, load):
                continue  # сервер упёрся в потолок — юзеру его не предлагаем
```

И в докстринг `provision_device_peers` (строки 132-138) добавить предложение:

```python
    Заполненные по `Server.max_peers` серверы пропускаются — потолок действует и
    здесь, иначе его обходил бы кто угодно кнопкой «добавить устройство».
```

- [ ] **Шаг 6: Прогнать тесты задачи**

Команда: `python -m pytest tests/test_distribution.py -v`
Ожидание: PASS, 16 тестов (12 старых + 4 новых).

- [ ] **Шаг 7: Прогнать весь набор**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 8: Коммит**

```bash
git add bot/db/models.py bot/db/repo/common.py bot/db/repo/__init__.py bot/handlers/configs.py tests/test_distribution.py
git commit -m "Этап C: потолок конфигов на сервере

Ёмкость была только у обхода БС, а WG-конфиги выдавались, пока не кончатся
IP в /24. Теперь у сервера есть свой потолок, заданный руками.

Учитывается в provision_device_peers — единственном месте, где создаются
пиры устройства: потолок, который обходится кнопкой «добавить устройство»,
не потолок."
```

---

### Задача 2: Админ задаёт лимит конфигов

Поле есть, но выставить его пока негде. Делаем как у обхода БС: цифра правится со списка объектов сервера (`bot/handlers/servers/bypass.py:54-105` — образец целиком, включая приём «`-` = без лимита»).

**Файлы:**
- Изменить: `bot/states/install.py:36-40`, `bot/keyboards/inline/servers.py:186-207`, `bot/handlers/servers/peers.py:32-71`
- Тест: `tests/test_admin_nav.py`

**Интерфейсы:**
- Потребляет: `Server.max_peers` (задача 1).
- Отдаёт наружу: `server_peers_admin(peers, server_id, page=0, has_prev=False, has_next=False)` — сигнатура не меняется, в клавиатуре появляется кнопка `srv:plim:<server_id>`.

- [ ] **Шаг 1: Написать падающий тест**

В `tests/test_admin_nav.py` добавить класс:

```python
class TestServerPeerLimitButton:
    def test_peers_list_has_limit_button(self) -> None:
        """Лимит конфигов правится оттуда же, откуда админ смотрит пиры, —
        как «✏️ Лимит обходов» в списке обходов сервера."""
        from bot.keyboards.inline import server_peers_admin

        kb = server_peers_admin([], server_id=9)

        assert "srv:plim:9" in _callbacks(kb)
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Команда: `python -m pytest tests/test_admin_nav.py -v`
Ожидание: FAIL — `assert 'srv:plim:9' in ['srv:open:9']`.

- [ ] **Шаг 3: Добавить состояние FSM**

В `bot/states/install.py`, в `ServerEditStates` (строки 36-40):

```python
    peer_limit = State()  # ёмкость по конфигам: Server.max_peers
```

- [ ] **Шаг 4: Добавить кнопку в клавиатуру**

В `bot/keyboards/inline/servers.py`, в `server_peers_admin`, перед кнопкой «« К серверу» (строка 205):

```python
    kb.button(text="✏️ Лимит конфигов", callback_data=f"{CB_SERVERS}:plim:{server_id}")
```

- [ ] **Шаг 5: Показать лимит и научиться его менять**

В `bot/handlers/servers/peers.py` заменить сигнатуру и тело `cb_server_peers` (строки 32-71). Изменения: принимает `state` и чистит FSM (сюда ведёт «Отмена» из ввода лимита), пустой сервер тоже показывает список (иначе до кнопки лимита не добраться), в шапке — лимит:

```python
@router.callback_query(F.data.startswith(f"{CB_SERVERS}:peers:"))
async def cb_server_peers(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Список всех пиров сервера — включая выданные через инвайт чужим юзерам."""
    # callback: "srv:peers:<id>" (стр. 0) или "srv:peers:<id>:<page>" (навигация)
    await state.clear()  # сюда ведёт «Отмена» из редактирования лимита конфигов
    parts = call.data.split(":")
    server_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    peers = await repo.list_peers_for_server(session, server.id)
    active = sum(1 for p in peers if p.status == PeerStatus.ACTIVE)
    total = len(peers)
    # Активные сверху, затем по id; режем на страницы.
    peers.sort(key=lambda p: (p.status != PeerStatus.ACTIVE, p.id))
    start = page * _PEERS_PER_PAGE
    page_peers = peers[start:start + _PEERS_PER_PAGE]
    limit = "∞" if server.max_peers is None else str(server.max_peers)

    # Пустой сервер тоже показываем этим экраном, а не карточкой сервера:
    # иначе до кнопки лимита не добраться, пока кому-нибудь не выдан конфиг.
    await call.message.edit_text(
        f"👥 <b>Peers — {server.name}</b>\n"
        f"Активных: <b>{active}</b> / лимит: <b>{limit}</b> / всего: <b>{total}</b>\n"
        "<i>Заполненный сервер юзерам при выдаче и переезде не предлагается.</i>",
        reply_markup=server_peers_admin(
            page_peers,
            server_id,
            page,
            has_prev=page > 0,
            has_next=start + _PEERS_PER_PAGE < total,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:plim:"))
async def cb_server_peer_limit(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    server_id = int(call.data.rsplit(":", 1)[-1])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await state.set_state(ServerEditStates.peer_limit)
    await state.update_data(server_id=server_id)
    limit = "∞" if server.max_peers is None else str(server.max_peers)
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="✖️ Отмена", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    await call.message.edit_text(
        "✏️ <b>Лимит конфигов на сервере</b>\n\n"
        f"Сейчас: <b>{limit}</b>\n\n"
        "Введи максимум активных конфигов (<code>0</code> — закрыть новую выдачу, "
        "выданные продолжают работать). <code>-</code> — без лимита.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(ServerEditStates.peer_limit, F.text, AdminFilter())
async def step_server_peer_limit(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    if raw == "-":
        value = None
    elif raw.isdigit() and int(raw) <= 100_000:
        value = int(raw)
    else:
        await message.answer("Число ≥ 0 или <code>-</code> (без лимита). Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    server = await repo.get_server(session, data["server_id"])
    if server is None:
        await message.answer("Сервер не найден.")
        return
    server.max_peers = value
    await session.commit()
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="« К пирам", callback_data=f"{CB_SERVERS}:peers:{server.id}")
    await message.answer(
        f"✅ Лимит конфигов: {'∞' if value is None else value}",
        reply_markup=kb.as_markup(),
    )
```

Импорты в шапке файла: к существующим добавить `ServerEditStates` (сейчас там `PeerRenameStates` — строка 22):

```python
from bot.states.install import PeerRenameStates, ServerEditStates
```

`FSMContext`, `Message`, `AdminFilter`, `server_card` уже импортированы (строки 5-25); `server_card` после правки в этом файле больше не используется — **проверь `grep -n "server_card" bot/handlers/servers/peers.py` и убери имя из импорта, если вызовов не осталось** (мёртвый импорт ловится линтером и путает следующего читателя).

- [ ] **Шаг 6: Прогнать тесты задачи**

Команда: `python -m pytest tests/test_admin_nav.py -v`
Ожидание: PASS, 4 теста.

- [ ] **Шаг 7: Проверить, что экран не сломан**

Команда: `python -c "import bot.handlers.servers.peers"`
Ожидание: без ошибок (проверяет, что импорты после правки сходятся).

- [ ] **Шаг 8: Прогнать весь набор**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 9: Коммит**

```bash
git add bot/states/install.py bot/keyboards/inline/servers.py bot/handlers/servers/peers.py tests/test_admin_nav.py
git commit -m "Этап C: админ задаёт лимит конфигов сервера

Правится оттуда же, откуда админ смотрит пиры, — как лимит обходов в списке
обходов. Пустой сервер теперь показывается тем же экраном: иначе до кнопки
лимита не добраться, пока кому-нибудь не выдан конфиг."
```

---

### Задача 3: Ядро переезда

Сердце этапа: два поля у пира, новое событие журнала и сервис, который умеет посчитать кулдаун, отобрать серверы-кандидаты и собственно переселить. Экранов здесь нет — задачи 5 и 6 будут звать готовые функции.

**Файлы:**
- Создать: `bot/services/relocate.py`
- Изменить: `bot/db/models.py:224-242` (поля пира), `bot/db/models.py:440-447` (`AuditAction`), `bot/db/repo/peers.py`, `bot/db/repo/__init__.py`, `bot/handlers/configs.py:62-126`, `bot/handlers/admin/audit.py:23-39`, `tests/test_distribution.py:80-85`
- Тест: `tests/test_relocate.py`

**Интерфейсы:**
- Потребляет: `repo.has_free_wg_slot(server, load)` (задача 1).
- Отдаёт наружу:
  - `Peer.grace_until: datetime | None`, `Peer.moved_at: datetime | None`
  - `AuditAction.CONFIG_MOVED = "config_moved"`
  - `async def repo.start_peer_grace(session, peer_id: int, *, until: datetime) -> None`
  - `async def repo.list_grace_expired_peers(session, now: datetime) -> list[Peer]`
  - `_create_peer_for_user(..., log_issue: bool = True)` — при `False` не пишет «выдан конфиг».
  - `relocate.GRACE_HOURS = 24`, `relocate.COOLDOWN_HOURS = 24`
  - `def relocate.cooldown_left(peer: Peer, now: datetime) -> timedelta | None`
  - `def relocate.visible_peers(peers: Iterable[Peer]) -> list[Peer]`
  - `async def relocate.candidates_for_peer(session, peer: Peer, *, owner: User) -> dict[str, list[Server]]`
  - `async def relocate.auto_target(session, peer: Peer, *, owner: User) -> Server | None`
  - `async def relocate.move_peer(session, peer: Peer, target: Server, *, owner: User, actor_tg_id: int | None, actor_is_admin: bool = False, reason: str) -> Peer`

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_relocate.py`:

```python
"""Переезд конфига на другой сервер (Этап C).

SSH замокан — проверяем оркестрацию и состояние БД:
  • кулдаун «раз в сутки на конфиг»;
  • отбор серверов-кандидатов (потолок, приватность, чужие локации);
  • сам переезд: новый пир создаётся ДО того, как старый помечен грейсом;
  • журнал: одна строка «конфиг переехал», без «выдан конфиг».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, AuditLog, Peer, PeerStatus, ServerStatus
from bot.handlers import configs
from bot.services import relocate
from bot.services.crypto import encrypt
from bot.services.ssh import SSHError


async def _user(session: AsyncSession, *, tg_id: int = 111, vip: bool = False):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.sub_max_devices = 5
    user.sub_max_bypass = 5
    user.sub_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    user.is_vip = vip
    return user


async def _server(session: AsyncSession, *, name: str, location: str | None,
                  max_peers: int | None = None, private: bool = False):
    server = await repo.create_server(
        session, name=name, host="1.1.1.1", wg_port=585,
        owner_tg_id=1, status=ServerStatus.READY, location=location,
        server_public_key="pub", server_endpoint="1.1.1.1:585",
    )
    server.max_peers = max_peers
    server.is_private = private
    await session.flush()
    return server


async def _peer(session: AsyncSession, *, server, user, device_id, ip="10.8.0.2"):
    peer = Peer(
        server_id=server.id, user_id=user.id, device_id=device_id,
        label="phone", ip=ip, public_key=f"pk{server.id}-{ip}",
        private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
    )
    session.add(peer)
    await session.flush()
    return peer


def _fake_create(calls: list):
    """Подмена configs._create_peer_for_user: без SSH, помнит сервер и log_issue."""
    async def fake(session, server, user, label, *, device_id=None, expires_at=None,
                   log_issue=True):
        calls.append((server.id, log_issue))
        peer = Peer(
            server_id=server.id, user_id=user.id, device_id=device_id,
            label=label, ip=f"10.8.{server.id}.99", public_key=f"new-pk{server.id}",
            private_key_enc=encrypt("priv"), status=PeerStatus.ACTIVE,
            expires_at=expires_at,
        )
        session.add(peer)
        await session.flush()
        return peer, f"conf-{server.id}"
    return fake


class TestCooldown:
    def test_never_moved_can_move_now(self) -> None:
        peer = Peer(moved_at=None)
        assert relocate.cooldown_left(peer, datetime.now(timezone.utc)) is None

    def test_just_moved_must_wait(self) -> None:
        now = datetime.now(timezone.utc)
        peer = Peer(moved_at=now - timedelta(hours=1))
        left = relocate.cooldown_left(peer, now)
        assert left is not None
        # Ждать примерно 23 часа — точную секунду не фиксируем.
        assert timedelta(hours=22) < left < timedelta(hours=23, minutes=1)

    def test_after_a_day_free_again(self) -> None:
        now = datetime.now(timezone.utc)
        peer = Peer(moved_at=now - timedelta(hours=25))
        assert relocate.cooldown_left(peer, now) is None

    def test_naive_datetime_from_sqlite_does_not_crash(self) -> None:
        """SQLite отдаёт время без таймзоны — вычитание aware-naive упало бы
        TypeError'ом (тот же капкан, что лечит utils.timefmt.as_utc)."""
        now = datetime.now(timezone.utc)
        peer = Peer(moved_at=(now - timedelta(hours=2)).replace(tzinfo=None))
        assert relocate.cooldown_left(peer, now) is not None


class TestVisiblePeers:
    def test_hides_grace_and_revoked(self) -> None:
        live = Peer(status=PeerStatus.ACTIVE, grace_until=None)
        dying = Peer(status=PeerStatus.ACTIVE,
                     grace_until=datetime.now(timezone.utc) + timedelta(hours=5))
        dead = Peer(status=PeerStatus.REVOKED, grace_until=None)

        assert relocate.visible_peers([live, dying, dead]) == [live]


class TestCandidates:
    async def test_excludes_current_and_full_servers(self, session: AsyncSession) -> None:
        user = await _user(session)
        other = await _user(session, tg_id=222)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        full = await _server(session, name="nl2", location="🇳🇱 Нидерланды", max_peers=1)
        free = await _server(session, name="nl3", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        await _peer(session, server=full, user=other, device_id=None, ip="10.8.1.5")

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert [s.id for s in groups["🇳🇱 Нидерланды"]] == [free.id]

    async def test_private_server_hidden_from_plain_user(self, session: AsyncSession) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="nl2", location="🇳🇱 Нидерланды", private=True)
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert groups == {}

    async def test_private_server_offered_to_friend(self, session: AsyncSession) -> None:
        user = await _user(session, vip=True)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        priv = await _server(session, name="nl2", location="🇳🇱 Нидерланды", private=True)
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert [s.id for s in groups["🇳🇱 Нидерланды"]] == [priv.id]

    async def test_location_where_device_already_has_config_excluded(
        self, session: AsyncSession
    ) -> None:
        """Устройство держит по конфигу на локацию. Переезд в страну, где конфиг
        уже есть, дал бы там два, а в родной — ни одного."""
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        de1 = await _server(session, name="de1", location="🇩🇪 Германия")
        await _server(session, name="de2", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        await _peer(session, server=de1, user=user, device_id=device.id, ip="10.8.2.2")

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert "🇩🇪 Германия" not in groups

    async def test_free_foreign_location_is_offered(self, session: AsyncSession) -> None:
        """А если конфига в той стране нет — механика переезда туда работает."""
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        de1 = await _server(session, name="de1", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        groups = await relocate.candidates_for_peer(session, peer, owner=user)

        assert [s.id for s in groups["🇩🇪 Германия"]] == [de1.id]


class TestAutoTarget:
    async def test_picks_least_loaded_in_same_location(self, session: AsyncSession) -> None:
        user = await _user(session)
        other = await _user(session, tg_id=222)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        loaded = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        empty = await _server(session, name="nl3", location="🇳🇱 Нидерланды")
        await _server(session, name="de1", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)
        for i in range(3):
            await _peer(session, server=loaded, user=other, device_id=None,
                        ip=f"10.8.1.{i + 10}")

        target = await relocate.auto_target(session, peer, owner=user)

        # Своя локация, наименее загруженный. Германию не берём: устройство
        # осталось бы без конфига в Нидерландах.
        assert target is not None and target.id == empty.id

    async def test_none_when_no_other_server_in_location(self, session: AsyncSession) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        await _server(session, name="de1", location="🇩🇪 Германия")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=home, user=user, device_id=device.id)

        assert await relocate.auto_target(session, peer, owner=user) is None


class TestMovePeer:
    async def test_creates_new_peer_and_graces_old(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)

        calls: list[tuple[int, bool]] = []
        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create(calls))
        before = datetime.now(timezone.utc)
        new = await relocate.move_peer(
            session, old, target, owner=user,
            actor_tg_id=user.tg_id, reason="по просьбе юзера",
        )
        await session.commit()

        assert calls == [(target.id, False)]      # событие пишет сам переезд
        assert new.server_id == target.id
        assert new.device_id == device.id
        assert new.label == old.label
        assert new.moved_at is not None           # кулдаун поехал с новым пиром
        # Старый конфиг ОСТАЁТСЯ рабочим — просто с датой смерти.
        assert old.status == PeerStatus.ACTIVE
        assert old.grace_until is not None
        left = relocate.as_utc(old.grace_until) - before
        assert timedelta(hours=23, minutes=59) < left < timedelta(hours=24, minutes=1)

    async def test_writes_one_moved_event(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)

        monkeypatch.setattr(configs, "_create_peer_for_user", _fake_create([]))
        new = await relocate.move_peer(
            session, old, target, owner=user,
            actor_tg_id=999, actor_is_admin=True, reason="отвязал админ",
        )
        await session.commit()

        rows = list((await session.execute(select(AuditLog))).scalars())
        assert [r.action for r in rows] == [AuditAction.CONFIG_MOVED]
        row = rows[0]
        assert row.target_user_id == user.id     # User.id, не tg_id
        assert row.target_type == "peer" and row.target_id == new.id
        assert row.actor_tg_id == 999 and row.actor_is_admin is True
        # В строке видно и «откуда → куда», и почему.
        assert "🇳🇱 Нидерланды 1" in row.details
        assert "🇳🇱 Нидерланды 2" in row.details
        assert "отвязал админ" in row.details

    async def test_failed_creation_leaves_old_peer_untouched(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Порядок «сначала создать» и существует ради этого случая: сеть упала —
        юзер остался со работающим конфигом."""
        user = await _user(session)
        home = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        target = await _server(session, name="nl2", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        old = await _peer(session, server=home, user=user, device_id=device.id)

        async def boom(*a, **kw):
            raise SSHError("сервер лёг")

        monkeypatch.setattr(configs, "_create_peer_for_user", boom)

        with pytest.raises(SSHError):
            await relocate.move_peer(
                session, old, target, owner=user,
                actor_tg_id=user.tg_id, reason="по просьбе юзера",
            )

        assert old.status == PeerStatus.ACTIVE and old.grace_until is None
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Команда: `python -m pytest tests/test_relocate.py -v`
Ожидание: FAIL — `ModuleNotFoundError: No module named 'bot.services.relocate'`.

- [ ] **Шаг 3: Добавить поля пира и код события**

В `bot/db/models.py`, в класс `Peer` после `expiry_warn_flags` (строка 242):

```python
    # Переезд конфига на другой сервер (Этап C). Бесшовного переезда не бывает:
    # у нового сервера свой публичный ключ и адрес, файл в приложении юзер
    # обязан заменить. Поэтому старый пир не гасится сразу, а доживает сутки:
    # grace_until — момент, когда планировщик снимет его с сервера. Поле
    # остаётся заполненным и ПОСЛЕ снятия: по нему ревайв узнаёт, что этот
    # конфиг переехал, и не поднимает его обратно при продлении подписки.
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Когда ЭТОТ конфиг появился в результате переезда (у обычных пиров NULL).
    # На нём держится ограничение «переезжать не чаще раза в сутки»: иначе
    # серверы дёргали бы каждые пять минут.
    moved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

В `AuditAction`, в блок «Доступ» после `CONFIG_REVIVED` (строка 443):

```python
    CONFIG_MOVED = "config_moved"          # конфиг переехал на другой сервер
```

В `bot/handlers/admin/audit.py`, в `_TITLES` после `CONFIG_REVIVED` (строка 30):

```python
    AuditAction.CONFIG_MOVED: "🔀 Конфиг переехал",
```

- [ ] **Шаг 4: Добавить функции репозитория**

В `bot/db/repo/peers.py`, после `revive_peer` (строка 85):

```python
async def start_peer_grace(session: AsyncSession, peer_id: int, *, until: datetime) -> None:
    """Помечает пир «доживает до until» (Этап C).

    Статус НЕ меняем: конфиг обязан работать все эти сутки — юзер мог нажать
    кнопку из любопытства, и мгновенный обрыв оставил бы его без интернета.
    """
    await session.execute(
        update(Peer).where(Peer.id == peer_id).values(grace_until=until)
    )


async def list_grace_expired_peers(session: AsyncSession, now: datetime) -> list[Peer]:
    """Активные пиры, у которых сутки после переезда вышли.

    Сравнение дат остаётся в SQL: SQLite отдаёт naive datetime, и в Python это
    был бы TypeError (тот же капкан, что описан у `utils.timefmt.as_utc`).
    """
    res = await session.execute(
        select(Peer)
        .where(Peer.status == PeerStatus.ACTIVE)
        .where(Peer.grace_until.isnot(None))
        .where(Peer.grace_until <= now)
        .order_by(Peer.id)
    )
    return list(res.scalars())
```

В `bot/db/repo/__init__.py` добавить `list_grace_expired_peers` и `start_peer_grace` в импорт из `bot.db.repo.peers` и в `__all__` (по алфавиту).

- [ ] **Шаг 5: Научить `_create_peer_for_user` молчать в журнале**

В `bot/handlers/configs.py` в сигнатуру `_create_peer_for_user` (строки 62-70) добавить параметр, а запись события (строки 114-121) обернуть условием:

```python
async def _create_peer_for_user(
    session: AsyncSession,
    server: Server,
    user: User,
    label: str,
    *,
    device_id: int | None = None,
    expires_at: "datetime | None" = None,
    log_issue: bool = True,
) -> tuple[Peer, str]:
```

и ниже, вместо безусловного `await repo.log_action(...)`:

```python
    # Пишем в журнал уже ВНЕ лока: запись в БД не участвует в аллокации IP,
    # держать из-за неё сериализацию сервера незачем.
    #
    # log_issue=False зовёт переезд (services/relocate): он пишет своё событие
    # «конфиг переехал». Иначе на один переезд в истории вышли бы две строки —
    # «выдан конфиг» и «переехал», — и админ, разбирая жалобу, не отличил бы
    # переезд от выдачи нового устройства.
    if log_issue:
        await repo.log_action(
            session, AuditAction.CONFIG_ISSUED,
            actor_tg_id=user.tg_id,
            target_user_id=user.id,
            target_type="peer",
            target_id=peer.id,
            details=f"{label} на сервере «{server.name}»",
        )
```

В `tests/test_distribution.py` подмена `_create_peer_for_user` должна пережить новый параметр — в `_fake_create_peer` (строка 82) добавить его в сигнатуру:

```python
    async def fake(session, server, user, label, *, device_id=None, expires_at=None,
                   log_issue=True):
```

То же самое — в локальной подмене `fake` внутри `test_falls_back_to_next_server_on_ssh_error` (строка 157).

- [ ] **Шаг 6: Написать сервис переезда**

Создать `bot/services/relocate.py`:

```python
"""Переезд конфига на другой сервер (Этап C).

Бесшовного переезда не бывает: у другого сервера свой публичный ключ, адрес и
endpoint, поэтому файл в приложении юзер обязан заменить руками. Отсюда весь
дизайн:

  • сначала создаём НОВЫЙ пир на целевом сервере и только потом трогаем
    старый — упало создание, юзер ничего не потерял и остался на рабочем
    конфиге;
  • старый конфиг живёт ещё сутки (грейс) и снимается планировщиком. Мгновенный
    обрыв отвергнут Владом: юзер мог нажать кнопку из любопытства и остался бы
    без интернета, пока не заменит файл;
  • переезжать можно не чаще раза в сутки на конфиг, иначе серверы будут
    дёргать каждые пять минут. На админа ограничение не распространяется — он
    разгружает сервер, а не капризничает.

Ёмкость сервера (`Server.max_peers`) уважается и здесь, и при обычной выдаче
устройства (`handlers/configs.provision_device_peers`): потолок, который
обходится кнопкой «добавить устройство», не потолок.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, Peer, PeerStatus, Server, User
from bot.services import amnezia
from bot.services.ssh import SSHClient, SSHError
from bot.utils.timefmt import as_utc

# Сколько старый конфиг доживает после переезда и как часто можно переезжать.
# Обе цифры — сутки, но смысл разный: первая про «успеть заменить файл»,
# вторая про «не дёргать серверы». Настройками не делаем — Влад просил цифру,
# а не ещё две env-переменные.
GRACE_HOURS = 24
COOLDOWN_HOURS = 24


def cooldown_left(peer: Peer, now: datetime) -> timedelta | None:
    """Сколько ждать до следующего переезда этого конфига; None — можно сейчас.

    `as_utc` обязателен: `moved_at` приезжает из SQLite без таймзоны, и прямое
    вычитание из aware-now упало бы TypeError'ом.
    """
    if peer.moved_at is None:
        return None
    left = timedelta(hours=COOLDOWN_HOURS) - (now - as_utc(peer.moved_at))
    return left if left > timedelta(0) else None


def visible_peers(peers: Iterable[Peer]) -> list[Peer]:
    """Конфиги, которые юзер должен видеть в карточке устройства: активные и не
    переехавшие.

    Доживающий сутки старый конфиг из списка убираем — он уже заменён новым в
    той же локации, и две строки одной страны читались бы как удвоение. Работать
    он при этом продолжает: сутки на замену файла у юзера остаются.
    """
    return [p for p in peers if p.status == PeerStatus.ACTIVE and p.grace_until is None]


async def candidates_for_peer(
    session: AsyncSession, peer: Peer, *, owner: User
) -> dict[str, list[Server]]:
    """Локация → серверы, куда можно переселить этот конфиг (внутри локации —
    по возрастанию загрузки).

    Отбор: READY, свободные по `max_peers`, кроме сервера, где пир живёт сейчас;
    приватные — только админам и «друзьям» (гейт в `list_ready_servers`).

    Локации, где у устройства УЖЕ есть свой активный конфиг, из выбора
    исключаются (кроме той, где живёт переезжающий пир): устройство держит по
    конфигу на каждую локацию, и переезд в чужую страну дал бы там два конфига,
    а в родной — ни одного. Практически это значит, что юзер выбирает другой
    сервер внутри своей локации, — ровно тот случай, ради которого кнопка и
    делается («сервер лёг / тормозит»).
    """
    home_server = await repo.get_server(session, peer.server_id)
    home_key = (
        (home_server.location or f"#{home_server.id}") if home_server else None
    )
    servers = await repo.list_ready_servers(session, for_user=owner)
    load = await repo.count_active_peers_by_server(session)
    taken: set[int] = set()
    if peer.device_id is not None:
        taken = {
            p.server_id
            for p in await repo.list_peers_for_device(session, peer.device_id)
            if p.status == PeerStatus.ACTIVE and p.id != peer.id
        }

    groups: dict[str, list[Server]] = {}
    for key, group in repo.group_by_location(servers).items():
        if key != home_key and any(s.id in taken for s in group):
            continue  # в этой стране у устройства уже есть свой конфиг
        free = [
            s for s in group
            if s.id != peer.server_id and repo.has_free_wg_slot(s, load)
        ]
        if free:
            groups[key] = sorted(free, key=lambda s: load.get(s.id, 0))
    return groups


async def auto_target(
    session: AsyncSession, peer: Peer, *, owner: User
) -> Server | None:
    """Кем заменить сервер, когда выбирает бот (админ отвязал конфиг): наименее
    загруженный свободный сервер В ТОЙ ЖЕ локации.

    Другая локация не годится: устройство держит по конфигу на страну, и
    «переселение» в другую страну оставило бы юзера без конфига в прежней.
    None — переселять некуда, и админу надо об этом сказать, а не выбирать
    что попало.
    """
    home_server = await repo.get_server(session, peer.server_id)
    if home_server is None:
        return None
    home_key = home_server.location or f"#{home_server.id}"
    group = (await candidates_for_peer(session, peer, owner=owner)).get(home_key)
    return group[0] if group else None


async def move_peer(
    session: AsyncSession,
    peer: Peer,
    target: Server,
    *,
    owner: User,
    actor_tg_id: int | None,
    actor_is_admin: bool = False,
    reason: str,
) -> Peer:
    """Переселяет конфиг на `target`: создаёт новый пир и ставит старому грейс.

    Порядок именно такой. Создание пира ходит по SSH и может упасть — тогда
    SSHError уходит вызывающему, а старый конфиг остаётся нетронутым и рабочим.
    Обратный порядок («сначала погасить») оставлял бы юзера без связи при первой
    же сетевой ошибке.

    Коммит — на вызывающем: событие журнала обязано откатиться вместе с
    переездом. Уведомление юзеру тоже на вызывающем (сервис не знает, кто
    инициатор — сам юзер или админ).
    """
    # Импорт внутри функции: handlers/configs тянет сервисы, и на уровне модуля
    # вышел бы цикл. Машинерия создания пира (лок на сервер, аллокация IP,
    # SSH-добавление) живёт там и дублировать её здесь нельзя.
    from bot.handlers.configs import _create_peer_for_user

    labels = await repo.server_labels_map(session)
    old_server = await repo.get_server(session, peer.server_id)
    where_from = labels.get(peer.server_id) or (old_server.name if old_server else "?")
    where_to = labels.get(target.id) or target.name

    new_peer, _conf = await _create_peer_for_user(
        session, target, owner, peer.label,
        device_id=peer.device_id,
        expires_at=peer.expires_at,
        log_issue=False,  # одна строка на переезд — она ниже
    )
    now = datetime.now(timezone.utc)
    new_peer.moved_at = now
    await repo.start_peer_grace(session, peer.id, until=now + timedelta(hours=GRACE_HOURS))
    await repo.log_action(
        session, AuditAction.CONFIG_MOVED,
        actor_tg_id=actor_tg_id,
        actor_is_admin=actor_is_admin,
        target_user_id=owner.id,
        target_type="peer",
        target_id=new_peer.id,
        details=f"«{peer.label}»: {where_from} → {where_to} ({reason})",
    )
    await session.flush()
    logger.info(
        "Peer {} moved to server {} (new peer {}), reason: {}",
        peer.id, target.id, new_peer.id, reason,
    )
    return new_peer
```

- [ ] **Шаг 7: Прогнать тесты задачи**

Команда: `python -m pytest tests/test_relocate.py -v`
Ожидание: PASS, 15 тестов.

Тест `test_creates_new_peer_and_graces_old` зовёт `relocate.as_utc` — это переэкспорт импортированного в модуле хелпера, отдельного кода не требует.

- [ ] **Шаг 8: Прогнать весь набор**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS. Особое внимание — `tests/test_distribution.py` и `tests/test_audit.py`: первый трогали в шаге 5, второй считает события журнала.

- [ ] **Шаг 9: Коммит**

```bash
git add bot/db/models.py bot/db/repo/peers.py bot/db/repo/__init__.py bot/handlers/configs.py bot/handlers/admin/audit.py bot/services/relocate.py tests/test_relocate.py tests/test_distribution.py
git commit -m "Этап C: ядро переезда конфига

Новый пир создаётся ДО того, как старый помечен грейсом: упало создание —
юзер остался на рабочем конфиге. Старый доживает сутки, потому что файл в
приложении юзер должен заменить руками, а нажать кнопку он мог и из
любопытства.

_create_peer_for_user умеет не писать «выдан конфиг»: на один переезд в
истории должна быть одна строка «переехал», иначе админ не отличит переезд
от выдачи нового устройства."
```

---

### Задача 4: Конец грейса и ревайв

Сутки прошли — старый конфиг надо снять с сервера, иначе он живёт вечно и занимает место под потолком. Отдельно: продление подписки не должно воскрешать переехавшие пиры (`bot/services/revive.py:170-198` поднимает все REVOKED-пиры устройства с прежними ключами — для переехавшего это вернуло бы юзеру конфиг, которого у него уже нет в приложении).

**Файлы:**
- Изменить: `bot/services/relocate.py` (функция `expire_grace_peers`), `bot/services/scheduler.py:398-408` (новая секция 2d после 2c), `bot/services/revive.py:170-176`
- Тест: `tests/test_relocate.py`

**Интерфейсы:**
- Потребляет: `repo.list_grace_expired_peers`, `repo.revoke_peer`, `relocate.GRACE_HOURS` (задача 3).
- Отдаёт наружу: `async def relocate.expire_grace_peers(session, now: datetime) -> list[Peer]` — снятые пиры (для лога планировщика).

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_relocate.py`. `FakeSSH` — тот же приём, что в `tests/test_revive.py:22-32`:

```python
class FakeSSH:
    """Асинхронный контекст-менеджер вместо SSHClient — соединения нет."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "FakeSSH":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FailingSSH(FakeSSH):
    """Коннект не поднимается — как упавший сервер."""

    async def __aenter__(self):
        raise SSHError("нет коннекта")


def _patch_ssh(monkeypatch, cls=FakeSSH) -> None:
    monkeypatch.setattr(relocate, "SSHClient", cls)
    monkeypatch.setattr(relocate.repo, "creds_from_server", lambda s: None)


class TestExpireGrace:
    async def test_peer_alive_until_grace_ends(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=srv, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) + timedelta(hours=5)
        await session.flush()

        _patch_ssh(monkeypatch)
        removed: list[str] = []
        monkeypatch.setattr(
            relocate.amnezia, "remove_peer_on_server",
            lambda ssh, *, public_key: removed.append(public_key),
        )

        done = await relocate.expire_grace_peers(session, datetime.now(timezone.utc))

        assert done == [] and removed == []
        assert peer.status == PeerStatus.ACTIVE

    async def test_revokes_after_grace_and_logs_event(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=srv, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        _patch_ssh(monkeypatch)
        removed: list[str] = []

        async def fake_remove(ssh, *, public_key):
            removed.append(public_key)

        monkeypatch.setattr(relocate.amnezia, "remove_peer_on_server", fake_remove)

        done = await relocate.expire_grace_peers(session, datetime.now(timezone.utc))
        await session.commit()

        assert [p.id for p in done] == [peer.id]
        assert removed == [peer.public_key]      # реально сняли с сервера
        assert peer.status == PeerStatus.REVOKED
        assert peer.revoked_at is not None       # дальше его подберёт ретеншн
        # grace_until НЕ обнуляем: по нему ревайв узнаёт переехавший конфиг.
        assert peer.grace_until is not None
        rows = list((await session.execute(select(AuditLog))).scalars())
        assert [r.action for r in rows] == [AuditAction.CONFIG_REVOKED]
        assert rows[0].actor_tg_id is None       # снял бот, не человек

    async def test_ssh_connect_failure_keeps_row_for_next_tick(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """В строке пира единственные ключи, которыми его снимают с VPS. Не
        поднялся коннект — не трогаем: повторим на следующем тике."""
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        peer = await _peer(session, server=srv, user=user, device_id=device.id)
        peer.grace_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        _patch_ssh(monkeypatch, FailingSSH)

        done = await relocate.expire_grace_peers(session, datetime.now(timezone.utc))

        assert done == []
        assert peer.status == PeerStatus.ACTIVE and peer.grace_until is not None


class TestReviveSkipsMoved:
    async def test_moved_peer_not_resurrected(
        self, session: AsyncSession, monkeypatch
    ) -> None:
        """Подписка кончилась во время грейса, юзер продлил. Переехавший конфиг
        поднимать нельзя: в приложении у юзера уже новый."""
        from bot.services import revive

        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        moved = await _peer(session, server=srv, user=user, device_id=device.id)
        normal = await _peer(session, server=srv, user=user, device_id=device.id,
                             ip="10.8.0.3")
        moved.grace_until = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.revoke_device(session, device.id)
        await session.flush()

        monkeypatch.setattr(revive, "SSHClient", FakeSSH)
        monkeypatch.setattr(revive.repo, "creds_from_server", lambda s: None)

        async def fake_add(ssh, *, public_key, peer_ip):
            return None

        monkeypatch.setattr(revive.amnezia, "add_peer_on_server", fake_add)

        res = await revive.revive_devices_for_user(session, user)
        await session.commit()

        assert res.peers_restored == 1
        assert normal.status == PeerStatus.ACTIVE
        assert moved.status == PeerStatus.REVOKED
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Команда: `python -m pytest tests/test_relocate.py::TestExpireGrace tests/test_relocate.py::TestReviveSkipsMoved -v`
Ожидание: FAIL — `AttributeError: module 'bot.services.relocate' has no attribute 'expire_grace_peers'`, а `TestReviveSkipsMoved` падает на `assert res.peers_restored == 1` (получит 2).

- [ ] **Шаг 3: Написать снятие доживших конфигов**

В конец `bot/services/relocate.py`:

```python
async def expire_grace_peers(session: AsyncSession, now: datetime) -> list[Peer]:
    """Снимает с серверов конфиги, у которых сутки грейса вышли, и метит их
    REVOKED — дальше их подберёт обычный ретеншн (30 дней, секция 2 тика).

    Отдельной функцией, а не строчками внутри тика планировщика: тик в тесте не
    поднять, а поведение «снял по SSH → отозвал» — ровно то, что надо проверять.

    Группируем по серверу: один коннект на сервер. Не поднялся коннект — строки
    этого сервера НЕ трогаем: в них единственные ключи, которыми пир снимается с
    VPS, и, потеряв их, мы оставили бы вечный конфиг на сервере (тот же приём,
    что в ретеншне пиров). Повторим на следующем тике.

    `grace_until` при отзыве не обнуляем: по нему ревайв узнаёт переехавший
    конфиг и не поднимает его при продлении подписки.
    """
    peers = await repo.list_grace_expired_peers(session, now)
    if not peers:
        return []

    by_server: dict[int, list[Peer]] = {}
    for p in peers:
        by_server.setdefault(p.server_id, []).append(p)

    done: list[Peer] = []
    for server_id, plist in by_server.items():
        server = await repo.get_server(session, server_id)
        if server is None:
            continue
        try:
            async with SSHClient(repo.creds_from_server(server)) as ssh:
                for p in plist:
                    try:
                        await amnezia.remove_peer_on_server(ssh, public_key=p.public_key)
                    except SSHError as exc:
                        # Сам пир мог быть уже снят руками — это не причина
                        # держать строку живой: отзываем и идём дальше.
                        logger.warning("Grace peer {} remove err: {}", p.id, exc)
        except SSHError as exc:
            logger.warning("Grace peers SSH connect err server {}: {}", server_id, exc)
            continue
        for p in plist:
            # Актора-человека нет: снимает бот по расписанию. Поставить сюда
            # tg_id владельца значило бы, что админ, разбирая жалобу «я ничего
            # не отключал», увидел бы инициатором самого жалующегося.
            await repo.revoke_peer(
                session, p.id,
                details=(
                    f"Старый конфиг «{p.label}» снят: сутки после переезда прошли"
                ),
            )
            done.append(p)
    return done
```

- [ ] **Шаг 4: Добавить секцию планировщика**

В `bot/services/scheduler.py`, после секции 2c (строка 408) и перед секцией 3:

```python
        # ── 2d. Конец грейса после переезда конфига (Этап C) ─────────────────
        # Переехавший конфиг сутки работает со старого сервера, чтобы юзер успел
        # заменить файл в приложении. Сутки вышли — снимаем его с VPS и метим
        # REVOKED; дальше строку подберёт обычный ретеншн (секция 2). Изоляция
        # та же, что у соседей: свой try/except + rollback.
        try:
            from bot.services import relocate as relocate_svc

            expired_moved = await relocate_svc.expire_grace_peers(session, now)
            if expired_moved:
                await session.commit()
                logger.info("Grace ended for {} moved peer(s)", len(expired_moved))
        except Exception:
            logger.exception("Scheduler section 2d (moved peers grace) failed")
            await session.rollback()
```

- [ ] **Шаг 5: Научить ревайв пропускать переехавшие**

В `bot/services/revive.py`, в цикле по пирам устройства (строки 170-176), сразу после проверки статуса:

```python
        for peer in await repo.list_peers_for_device(session, device.id):
            if peer.status != PeerStatus.REVOKED:
                continue
            # Переехавший конфиг (Этап C) обратно не поднимаем: его заменил новый
            # пир на другом сервере, а файла от старого у юзера в приложении
            # уже нет — «оживший» конфиг просто занимал бы IP и место под
            # потолком сервера. Метку переезда держит grace_until.
            if peer.grace_until is not None:
                continue
```

И в докстринг модуля (строки 12-15) добавить строку:

```python
Переехавшие пиры (Этап C, `Peer.grace_until`) не воскрешаются вообще.
```

- [ ] **Шаг 6: Прогнать тесты задачи**

Команда: `python -m pytest tests/test_relocate.py tests/test_revive.py -v`
Ожидание: PASS, 31 тест (15 из задачи 3 + 4 новых + 12 старых из `test_revive.py`).

- [ ] **Шаг 7: Прогнать весь набор**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 8: Проверить планировщик глазами**

Команда: `python -c "import bot.services.scheduler"` и `grep -n "2d\." bot/services/scheduler.py`
Ожидание: импорт без ошибок; секция 2d стоит между 2c и «── 3. Учёт трафика».

Убедись, что `now` в секции 2d — это `now`, посчитанный в начале `_run_checks` (строка 153), а не новый вызов `datetime.now`: все секции тика обязаны видеть одно и то же время, иначе граница «грейс истёк» плавает внутри одного тика.

- [ ] **Шаг 9: Коммит**

```bash
git add bot/services/relocate.py bot/services/scheduler.py bot/services/revive.py tests/test_relocate.py
git commit -m "Этап C: конец грейса и ревайв переехавших

Сутки прошли — бот снимает старый конфиг с сервера сам, иначе он живёт вечно
и занимает место под потолком. Логика в функции сервиса, а не инлайном в
тике: тик в тесте не поднять.

Продление подписки переехавшие пиры больше не воскрешает: файла от старого
конфига у юзера в приложении уже нет, а IP и слот он бы занял."
```

---

### Задача 5: Сценарий админа — «Переселить»

В карточке пира на сервере (`bot/handlers/servers/peers.py:74-120`) появляется кнопка. Бот сам выбирает сервер (`relocate.auto_target`), переселяет и присылает юзеру новый конфиг с пояснением. Кулдаун на админа не распространяется — он разгружает сервер.

**Файлы:**
- Изменить: `bot/keyboards/inline/servers.py:210-222`, `bot/handlers/servers/peers.py:74-120` (карточка) и новый хендлер после неё, `bot/texts/ru.py`
- Тест: `tests/test_admin_nav.py`

**Интерфейсы:**
- Потребляет: `relocate.auto_target`, `relocate.move_peer` (задача 3), `ask_config_format` из `bot/handlers/config_delivery.py`.
- Отдаёт наружу: `admin_peer_card(peer_id, server_id, can_revoke, can_move: bool = False)` — кнопка `adm:move:<peer_id>`; `t.move_by_admin`.

- [ ] **Шаг 1: Написать падающий тест**

В `tests/test_admin_nav.py`:

```python
class TestAdminMoveButton:
    def test_active_peer_card_has_move(self) -> None:
        from bot.keyboards.inline import admin_peer_card

        kb = admin_peer_card(peer_id=42, server_id=9, can_revoke=True, can_move=True)

        assert "adm:move:42" in _callbacks(kb)

    def test_no_move_when_nowhere_to_go(self) -> None:
        """Кнопки нет, когда переселять некуда: живая кнопка, отвечающая
        «некуда», — это обещание, которого админу не выполнят."""
        from bot.keyboards.inline import admin_peer_card

        kb = admin_peer_card(peer_id=42, server_id=9, can_revoke=True, can_move=False)

        assert "adm:move:42" not in _callbacks(kb)

    def test_revoked_peer_has_no_move(self) -> None:
        from bot.keyboards.inline import admin_peer_card

        kb = admin_peer_card(peer_id=42, server_id=9, can_revoke=False, can_move=False)

        assert "adm:move:42" not in _callbacks(kb)
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Команда: `python -m pytest tests/test_admin_nav.py -v`
Ожидание: FAIL — `TypeError: admin_peer_card() got an unexpected keyword argument 'can_move'`.

- [ ] **Шаг 3: Добавить кнопку**

В `bot/keyboards/inline/servers.py` заменить `admin_peer_card` (строки 210-222):

```python
def admin_peer_card(
    peer_id: int, server_id: int, can_revoke: bool, can_move: bool = False
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_revoke:
        kb.button(text="📥 Получить конфиг", callback_data=f"{CB_ADMIN}:conf:{peer_id}")
        # Переезд (Этап C): бот сам возьмёт свободный сервер этой же локации.
        # Кнопки нет, когда переселять некуда, — живая кнопка, отвечающая
        # «некуда», это обещание, которого админу не выполнят.
        if can_move:
            kb.button(text="🔀 Переселить", callback_data=f"{CB_ADMIN}:move:{peer_id}")
        kb.button(text="🗑 Отозвать",         callback_data=f"{CB_ADMIN}:revoke:{peer_id}")
    else:
        kb.button(text="♻️ Возобновить",   callback_data=f"{CB_ADMIN}:revive:{peer_id}")
        kb.button(text="❌ Удалить из БД", callback_data=f"{CB_ADMIN}:delete:{peer_id}")
    # Переименование доступно всегда — это просто метка в БД, не трогает конфиг.
    kb.button(text="✏️ Переименовать", callback_data=f"{CB_ADMIN}:rename:{peer_id}")
    kb.button(text="« К пирам", callback_data=f"{CB_SERVERS}:peers:{server_id}")
    kb.adjust(1)
    return kb.as_markup()
```

- [ ] **Шаг 4: Показать состояние переезда в карточке**

В `bot/handlers/servers/peers.py`, в `cb_admin_peer_open`, перед `await call.message.edit_text(...)` (строка 114) добавить строку про грейс и посчитать `can_move`:

```python
    if peer.grace_until is not None:
        # Переехавший конфиг: админ должен понимать, почему пир жив, но его
        # нет в карточке устройства у юзера.
        text += (
            f"\n• 🔀 Переехал, работает до {fmt_msk(peer.grace_until)} МСК"
        )

    can_move = False
    if peer.status == PeerStatus.ACTIVE and peer.grace_until is None:
        owner_user = await repo.get_user_by_id(session, peer.user_id)
        if owner_user is not None:
            can_move = await relocate.auto_target(
                session, peer, owner=owner_user
            ) is not None

    await call.message.edit_text(
        text,
        reply_markup=admin_peer_card(
            peer.id, server.id,
            can_revoke=peer.status == PeerStatus.ACTIVE,
            can_move=can_move,
        ),
    )
    await call.answer()
```

Импорт в шапке файла: `from bot.services import amnezia, relocate` (сейчас там `from bot.services import amnezia`, строка 20).

- [ ] **Шаг 5: Добавить текст уведомления юзеру**

В `bot/texts/ru.py`, в конец блока устройств (после `device_created`, ~строка 250):

```python
    # ---------- Смена сервера (Этап C) ----------
    move_by_admin = (
        "🔀 <b>Мы сменили сервер у конфига «{label}»</b>\n\n"
        "Было: {where_from} · стало: {where_to}.\n"
        "Так бывает, когда сервер перегружен или мы выводим его из работы — "
        "скорость от этого только выиграет.\n\n"
        "⚠️ <b>Новый конфиг нужно добавить в приложение</b>: у другого сервера "
        "свои ключи, старый файл на нём не заработает. Ниже спрошу, в каком "
        "виде его прислать.\n"
        "Старый конфиг проработает ещё <b>сутки</b> — не торопись, но и не "
        "забудь заменить."
    )
```

- [ ] **Шаг 6: Написать хендлер переезда**

В `bot/handlers/servers/peers.py`, после `cb_admin_peer_open`:

```python
@router.callback_query(F.data.startswith(f"{CB_ADMIN}:move:"))
async def cb_admin_peer_move(call: CallbackQuery, session: AsyncSession) -> None:
    """Админ отвязывает конфиг от сервера — бот сам подбирает другой.

    Кулдаун «раз в сутки» здесь намеренно не проверяется: он защищает серверы
    от юзера, который дёргает кнопку, а админ как раз разгружает сервер.
    """
    peer_id = int(call.data.rsplit(":", 1)[-1])
    peer = await repo.get_peer(session, peer_id)
    if peer is None:
        await call.answer("Не найдено", show_alert=True)
        return
    server = await repo.get_server(session, peer.server_id)
    if server is None:
        await call.answer("Нет доступа", show_alert=True)
        return
    if peer.status != PeerStatus.ACTIVE or peer.grace_until is not None:
        await call.answer("Этот конфиг уже отозван или переехал", show_alert=True)
        return
    owner = await repo.get_user_by_id(session, peer.user_id)
    if owner is None:
        await call.answer("Владелец не найден", show_alert=True)
        return

    target = await relocate.auto_target(session, peer, owner=owner)
    if target is None:
        await call.answer(
            "Переселять некуда: в этой локации нет другого свободного сервера. "
            "Подними лимит конфигов на соседнем или добавь сервер.",
            show_alert=True,
        )
        return

    await call.answer("⏳ Переселяю...")
    try:
        new_peer = await relocate.move_peer(
            session, peer, target, owner=owner,
            actor_tg_id=call.from_user.id, actor_is_admin=True,
            reason="отвязал админ",
        )
    except SSHError as exc:
        await session.rollback()
        # Сырой exc в текст не тащим: он может содержать host:port и ломает
        # HTML символом «<». Админу хватает факта и лога.
        logger.warning("Admin peer move failed: {}", exc)
        await call.message.edit_text(
            "❌ Не получилось переселить — целевой сервер не отвечает. "
            "Конфиг остался на прежнем месте и работает.",
            reply_markup=admin_peer_card(
                peer.id, server.id, can_revoke=True, can_move=True
            ),
        )
        return
    await session.commit()

    labels = await repo.server_labels_map(session)
    where_from = labels.get(server.id, server.name)
    where_to = labels.get(target.id, target.name)

    # Уведомление юзеру — best-effort: он мог заблокировать бота, и это не
    # повод считать переезд несостоявшимся (он уже в БД и на серверах).
    from bot.handlers.config_delivery import ask_config_format

    try:
        await bot.send_message(
            owner.tg_id,
            t.move_by_admin.format(
                label=peer.label, where_from=where_from, where_to=where_to
            ),
        )
        await ask_config_format(owner.tg_id, session, new_peer)
    except Exception:
        logger.warning("Move notify failed for user {}", owner.id)

    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
    kb = IKB()
    kb.button(text="🔍 К новому пиру", callback_data=f"{CB_ADMIN}:peer:{new_peer.id}")
    kb.button(text="« К пирам сервера", callback_data=f"{CB_SERVERS}:peers:{server.id}")
    kb.adjust(1)
    await call.message.edit_text(
        f"🔀 Конфиг <code>{peer.label}</code> переехал: {where_from} → {where_to}.\n"
        "Юзеру ушёл новый конфиг с пояснением. Старый работает ещё сутки, "
        "потом бот снимет его сам.",
        reply_markup=kb.as_markup(),
    )
```

Импорты в шапке файла, которых там ещё нет: `from bot.loader import bot` и `from bot.texts import t` (`t` уже импортирован — строка 23; `bot` нет). `logger` и `SSHError` уже импортированы (строки 7, 21).

- [ ] **Шаг 7: Прогнать тесты задачи**

Команда: `python -m pytest tests/test_admin_nav.py -v`
Ожидание: PASS, 7 тестов.

- [ ] **Шаг 8: Проверить импорты модуля**

Команда: `python -c "import bot.handlers.servers.peers"`
Ожидание: без ошибок.

- [ ] **Шаг 9: Прогнать весь набор**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 10: Коммит**

```bash
git add bot/keyboards/inline/servers.py bot/handlers/servers/peers.py bot/texts/ru.py tests/test_admin_nav.py
git commit -m "Этап C: админ переселяет конфиг из карточки пира

Бот сам берёт свободный сервер той же локации: в другую страну нельзя —
устройство держит по конфигу на страну и осталось бы без конфига в прежней.

Кнопки нет, когда переселять некуда: живая кнопка, отвечающая «некуда», —
обещание, которого админу не выполнят."
```

---

### Задача 6: Сценарий юзера — «Сменить сервер»

Четыре экрана: какой конфиг → локация → сервер → подтверждение. Плюс карточка устройства перестаёт показывать доживающий конфиг (иначе в одной локации две строки, читается как удвоение).

Экраны живут в отдельном модуле, а не в `bot/handlers/devices.py`: там уже 482 строки и своя тема (список устройств, создание, удаление, подписка). FSM не заводим — `peer_id`/`server_id` едут в `callback_data`, а права проверяются в каждом хендлере (Этап B: `peer_id` подделывается тривиально).

**Файлы:**
- Создать: `bot/handlers/config_move.py`
- Изменить: `bot/keyboards/inline/devices.py:48-71`, `bot/keyboards/inline/__init__.py` (реэкспорт), `bot/handlers/devices.py:243-279` и `298-313`, `bot/handlers/__init__.py`, `bot/texts/ru.py`
- Тест: `tests/test_relocate.py`

**Интерфейсы:**
- Потребляет: `relocate.candidates_for_peer`, `relocate.cooldown_left`, `relocate.move_peer`, `relocate.visible_peers` (задача 3), `ask_config_format` (`bot/handlers/config_delivery.py`), `_sub_active` (`bot/handlers/devices.py:47-51`).
- Отдаёт наружу:
  - `device_card_kb(device_id, can_get, can_revoke, locations=None, can_move: bool = False)` — кнопка `dev:move:<device_id>`
  - `move_pick_config_kb(rows: list[tuple[int, str]], device_id: int)` → `dev:mvloc:<peer_id>`
  - `move_pick_location_kb(peer_id: int, names: list[str], device_id: int)` → `dev:mvsrv:<peer_id>:<idx>`
  - `move_pick_server_kb(peer_id: int, rows: list[tuple[int, str]], device_id: int)` → `dev:mvok:<peer_id>:<server_id>`
  - `move_confirm_kb(peer_id: int, server_id: int, device_id: int)` → `dev:mvgo:<peer_id>:<server_id>`
  - `t.move_confirm`, `t.move_done`

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_relocate.py`:

```python
class TestMoveKeyboards:
    def _cb(self, markup) -> list[str]:
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_device_card_has_move_button(self) -> None:
        from bot.keyboards.inline import device_card_kb

        kb = device_card_kb(
            device_id=7, can_get=True, can_revoke=True,
            locations=[(42, "🇳🇱 Нидерланды")], can_move=True,
        )

        assert "dev:move:7" in self._cb(kb)

    def test_device_card_without_move(self) -> None:
        """Подписка кончилась или переезжать некуда — кнопки нет."""
        from bot.keyboards.inline import device_card_kb

        kb = device_card_kb(
            device_id=7, can_get=True, can_revoke=True,
            locations=[(42, "🇳🇱 Нидерланды")], can_move=False,
        )

        assert "dev:move:7" not in self._cb(kb)

    def test_pick_config_then_location_then_server_then_confirm(self) -> None:
        """Четыре экрана связаны в цепочку: каждый ведёт в следующий."""
        from bot.keyboards.inline import (
            move_confirm_kb, move_pick_config_kb, move_pick_location_kb,
            move_pick_server_kb,
        )

        assert "dev:mvloc:42" in self._cb(
            move_pick_config_kb([(42, "🇳🇱 Нидерланды")], device_id=7)
        )
        assert "dev:mvsrv:42:0" in self._cb(
            move_pick_location_kb(42, ["🇳🇱 Нидерланды"], device_id=7)
        )
        assert "dev:mvok:42:9" in self._cb(
            move_pick_server_kb(42, [(9, "🇳🇱 Нидерланды 2")], device_id=7)
        )
        assert "dev:mvgo:42:9" in self._cb(move_confirm_kb(42, 9, device_id=7))

    def test_every_screen_can_go_back_to_device(self) -> None:
        """Из любого шага юзер должен уметь выйти в карточку устройства, не
        доводя переезд до конца."""
        from bot.keyboards.inline import (
            move_confirm_kb, move_pick_config_kb, move_pick_location_kb,
            move_pick_server_kb,
        )

        for kb in (
            move_pick_config_kb([(42, "🇳🇱 Нидерланды")], device_id=7),
            move_pick_location_kb(42, ["🇳🇱 Нидерланды"], device_id=7),
            move_pick_server_kb(42, [(9, "🇳🇱 Нидерланды 2")], device_id=7),
            move_confirm_kb(42, 9, device_id=7),
        ):
            assert "dev:open:7" in self._cb(kb)


class TestDeviceCardHidesMovedPeer:
    async def test_card_shows_only_live_configs(self, session: AsyncSession) -> None:
        """Карточка устройства строится через relocate.visible_peers: старый
        конфиг сутки работает, но в списке его быть не должно."""
        user = await _user(session)
        srv = await _server(session, name="nl1", location="🇳🇱 Нидерланды")
        device = await repo.create_device(session, user_id=user.id, label="phone")
        dying = await _peer(session, server=srv, user=user, device_id=device.id)
        dying.grace_until = datetime.now(timezone.utc) + timedelta(hours=10)
        live = await _peer(session, server=srv, user=user, device_id=device.id,
                           ip="10.8.0.7")
        await session.flush()

        peers = await repo.list_peers_for_device(session, device.id)

        assert [p.id for p in relocate.visible_peers(peers)] == [live.id]
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Команда: `python -m pytest tests/test_relocate.py::TestMoveKeyboards -v`
Ожидание: FAIL — `TypeError: device_card_kb() got an unexpected keyword argument 'can_move'` и `ImportError` на `move_pick_config_kb`.

- [ ] **Шаг 3: Добавить клавиатуры**

В `bot/keyboards/inline/devices.py` заменить `device_card_kb` (строки 48-71) и дописать четыре новые:

```python
def device_card_kb(
    device_id: int,
    can_get: bool,
    can_revoke: bool,
    locations: list[tuple[int, str]] | None = None,  # (peer_id, loc_label)
    can_move: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_get:
        locs = locations or []
        if len(locs) > 1:
            # Несколько локаций → кнопка на каждую + «получить все» разом.
            for peer_id, loc in locs:
                kb.button(text=f"📥 {loc}", callback_data=f"{CB_DEVICE}:send1:{peer_id}")
            kb.button(text="📥 Получить все", callback_data=f"{CB_DEVICE}:send:{device_id}")
        else:
            kb.button(text="📥 Получить конфиг", callback_data=f"{CB_DEVICE}:send:{device_id}")
    # Смена сервера (Этап C). Кнопки нет, когда переезжать некуда: живая
    # кнопка, отвечающая «некуда», хуже отсутствующей.
    if can_move:
        kb.button(text="🔀 Сменить сервер", callback_data=f"{CB_DEVICE}:move:{device_id}")
    # Переименование — только метка в БД, конфиги не трогает (Блок «Ревизия»).
    kb.button(text="✏️ Переименовать", callback_data=f"{CB_DEVICE}:ren:{device_id}")
    # Удаление доступно всегда: активное устройство удаляется (с отзывом), а
    # неактивное (истекшее) — убирается из списка, чтобы не висело мусором.
    kb.button(text="🗑 Удалить устройство", callback_data=f"{CB_DEVICE}:revoke:{device_id}")
    kb.button(text="« К устройствам", callback_data=f"{CB_DEVICE}:list")
    kb.adjust(1)
    return kb.as_markup()


def move_pick_config_kb(
    rows: list[tuple[int, str]], device_id: int  # (peer_id, loc_label)
) -> InlineKeyboardMarkup:
    """Какой из конфигов устройства переселяем. Показывается только когда их
    больше одного — с единственным конфигом лишний экран это лишний тап."""
    kb = InlineKeyboardBuilder()
    for peer_id, loc in rows:
        kb.button(text=f"🔀 {loc}", callback_data=f"{CB_DEVICE}:mvloc:{peer_id}")
    kb.button(text="« К устройству", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def move_pick_location_kb(
    peer_id: int, names: list[str], device_id: int
) -> InlineKeyboardMarkup:
    """Локации кнопками ПО ИНДЕКСУ: юникод-название с флагом («🇳🇱 Нидерланды»)
    в 64 байта callback_data не всегда влезает — тот же приём, что в
    pick_location_kb. Индекс — позиция в отсортированном списке ключей, и
    хендлер пересобирает список тем же способом."""
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(names):
        kb.button(text=name, callback_data=f"{CB_DEVICE}:mvsrv:{peer_id}:{i}")
    kb.button(text="« К устройству", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def move_pick_server_kb(
    peer_id: int, rows: list[tuple[int, str]], device_id: int  # (server_id, label)
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for server_id, label in rows:
        kb.button(text=f"🖥 {label}", callback_data=f"{CB_DEVICE}:mvok:{peer_id}:{server_id}")
    kb.button(text="« К устройству", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()


def move_confirm_kb(peer_id: int, server_id: int, device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, переехать", callback_data=f"{CB_DEVICE}:mvgo:{peer_id}:{server_id}")
    kb.button(text="« К устройству", callback_data=f"{CB_DEVICE}:open:{device_id}")
    kb.adjust(1)
    return kb.as_markup()
```

В `bot/keyboards/inline/__init__.py` добавить четыре имени в импорт из `bot.keyboards.inline.devices` (строки 35-42) и в `__all__` (по алфавиту).

- [ ] **Шаг 4: Добавить тексты**

В `bot/texts/ru.py`, рядом с `move_by_admin` из задачи 5:

```python
    move_confirm = (
        "🔀 <b>Смена сервера</b>\n\n"
        "Конфиг «{label}» переедет:\n"
        "• сейчас: <b>{where_from}</b>\n"
        "• станет: <b>{where_to}</b>\n\n"
        "⚠️ <b>Файл в приложении придётся заменить.</b> У другого сервера свои "
        "ключи и адрес — старый конфиг на нём не заработает. Новый пришлю сразу "
        "после подтверждения.\n"
        "Старый будет работать ещё <b>сутки</b>, чтобы ты успел без спешки.\n\n"
        "<i>Менять сервер можно раз в сутки.</i>"
    )
    move_done = (
        "✅ <b>Готово: конфиг «{label}» теперь на {where_to}</b>\n\n"
        "Ниже спрошу, в каком виде прислать новый конфиг — добавь его в "
        "AmneziaVPN и подключись.\n"
        "Старый проработает ещё сутки и отключится сам; когда подключишься к "
        "новому, старый можно удалить из приложения."
    )
```

- [ ] **Шаг 5: Написать модуль экранов**

Создать `bot/handlers/config_move.py`:

```python
"""Смена сервера у конфига глазами юзера (Этап C).

Отдельным модулем, а не внутри handlers/devices.py: там своя тема — список
устройств, их создание, удаление и подписка, — а здесь четыре экрана подряд
(какой конфиг → локация → сервер → подтверждение), и в карточке устройств они
бы утонули.

FSM нет: peer_id и server_id едут в callback_data. Значит, права проверяются В
КАЖДОМ хендлере — id подделывается тривиально (урок Этапа B). Ответ на чужой id
дословно совпадает с ответом на несуществующий: по разнице ответов чужой конфиг
не должен отличаться от несуществующего.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Peer, PeerStatus, User
from bot.handlers.config_delivery import ask_config_format
from bot.handlers.devices import _sub_active
from bot.keyboards.inline import (
    CB_DEVICE,
    back_to_devices_kb,
    move_confirm_kb,
    move_pick_config_kb,
    move_pick_location_kb,
    move_pick_server_kb,
)
from bot.services import relocate
from bot.services.ssh import SSHError
from bot.texts import t

router = Router(name="config_move")


async def _own_peer(
    call: CallbackQuery, session: AsyncSession, peer_id: int
) -> tuple[Peer, User] | None:
    """Пир юзера, нажавшего кнопку, — или None с готовым ответом.

    Проверка одна на все экраны: забыть её в одном из четырёх хендлеров —
    ровно та ошибка, которой в Этапе B стоил отдельный разбор.
    """
    peer = await repo.get_peer(session, peer_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if peer is None or user is None or peer.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return None
    if peer.status != PeerStatus.ACTIVE or peer.grace_until is not None:
        await call.answer("Этот конфиг уже нельзя переселить", show_alert=True)
        return None
    if not _sub_active(user):
        await call.answer(
            "Подписка закончилась — сначала продли её в «🎫 Моя подписка».",
            show_alert=True,
        )
        return None
    return peer, user


def _cooldown_answer(peer: Peer) -> str | None:
    """Текст отказа по кулдауну или None, если переезжать можно."""
    left = relocate.cooldown_left(peer, datetime.now(timezone.utc))
    if left is None:
        return None
    hours = int(left.total_seconds() // 3600)
    when = f"{hours} ч" if hours >= 1 else f"{int(left.total_seconds() // 60)} мин"
    return (
        f"Этот конфиг уже переезжал недавно. Следующий переезд — через {when}: "
        "так серверы не дёргаются каждые пять минут."
    )


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:move:"))
async def cb_move_start(call: CallbackQuery, session: AsyncSession) -> None:
    """Начало: какой конфиг переселяем. Один конфиг — сразу к локациям."""
    device_id = int(call.data.rsplit(":", 1)[-1])
    device = await repo.get_device(session, device_id)
    user = await repo.get_user_by_tg_id(session, call.from_user.id)
    if device is None or user is None or device.user_id != user.id:
        await call.answer("Не найдено", show_alert=True)
        return

    peers = relocate.visible_peers(await repo.list_peers_for_device(session, device.id))
    if not peers:
        await call.answer("У этого устройства нет активных конфигов", show_alert=True)
        return
    if len(peers) == 1:
        await _render_locations(call, session, peers[0], user, device_id)
        return

    labels = await repo.server_labels_map(session)
    rows = [(p.id, labels.get(p.server_id, "?")) for p in peers]
    await call.message.edit_text(
        "🔀 <b>Смена сервера</b>\n\n"
        "У этого устройства несколько конфигов — по одному на страну. "
        "Выбери, какой переселить:",
        reply_markup=move_pick_config_kb(rows, device_id),
    )
    await call.answer()


async def _render_locations(
    call: CallbackQuery, session: AsyncSession, peer: Peer, user: User, device_id: int
) -> None:
    """Экран локаций. Вынесен, потому что в него ведут два пути: с выбора
    конфига и напрямую, когда конфиг единственный."""
    denied = _cooldown_answer(peer)
    if denied:
        await call.answer(denied, show_alert=True)
        return
    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    if not groups:
        await call.answer(
            "Сейчас переезжать некуда: свободных серверов нет. Попробуй позже — "
            "или напиши в «🆘 Поддержка», разберёмся.",
            show_alert=True,
        )
        return
    keys = sorted(groups)
    # Сервер без локации попал бы в кнопки как «#id» — показываем его имя.
    names = [k if not k.startswith("#") else groups[k][0].name for k in keys]
    labels = await repo.server_labels_map(session)
    await call.message.edit_text(
        f"🔀 <b>Куда переселить «{peer.label}»</b>\n\n"
        f"Сейчас конфиг живёт здесь: <b>{labels.get(peer.server_id, '?')}</b>.\n"
        "Выбери страну:",
        reply_markup=move_pick_location_kb(peer.id, names, device_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvloc:"))
async def cb_move_locations(call: CallbackQuery, session: AsyncSession) -> None:
    peer_id = int(call.data.rsplit(":", 1)[-1])
    got = await _own_peer(call, session, peer_id)
    if got is None:
        return
    peer, user = got
    await _render_locations(call, session, peer, user, peer.device_id or 0)


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvsrv:"))
async def cb_move_servers(call: CallbackQuery, session: AsyncSession) -> None:
    """Серверы выбранной локации. Список пересобираем заново: пока юзер думал,
    места могли кончиться."""
    _, _, rest = call.data.partition(f"{CB_DEVICE}:mvsrv:")
    peer_id_s, idx_s = rest.split(":")
    got = await _own_peer(call, session, int(peer_id_s))
    if got is None:
        return
    peer, user = got

    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    keys = sorted(groups)
    idx = int(idx_s)
    if idx >= len(keys):
        await call.answer("Список успел измениться, начни заново.", show_alert=True)
        return
    group = groups[keys[idx]]
    labels = await repo.server_labels_map(session)
    rows = [(s.id, labels.get(s.id, s.name)) for s in group]
    await call.message.edit_text(
        "🔀 <b>Выбери сервер</b>\n\n"
        "Серверы одной страны отличаются только нагрузкой — сверху те, где "
        "свободнее.",
        reply_markup=move_pick_server_kb(peer.id, rows, peer.device_id or 0),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvok:"))
async def cb_move_confirm(call: CallbackQuery, session: AsyncSession) -> None:
    """Экран подтверждения: здесь юзер узнаёт про замену файла ДО переезда."""
    _, _, rest = call.data.partition(f"{CB_DEVICE}:mvok:")
    peer_id_s, server_id_s = rest.split(":")
    got = await _own_peer(call, session, int(peer_id_s))
    if got is None:
        return
    peer, user = got

    server_id = int(server_id_s)
    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    allowed = {s.id for group in groups.values() for s in group}
    if server_id not in allowed:
        await call.answer(
            "Этот сервер уже недоступен — выбери другой.", show_alert=True
        )
        return
    labels = await repo.server_labels_map(session)
    await call.message.edit_text(
        t.move_confirm.format(
            label=peer.label,
            where_from=labels.get(peer.server_id, "?"),
            where_to=labels.get(server_id, "?"),
        ),
        reply_markup=move_confirm_kb(peer.id, server_id, peer.device_id or 0),
    )
    await call.answer()


@router.callback_query(F.data.startswith(f"{CB_DEVICE}:mvgo:"))
async def cb_move_go(call: CallbackQuery, session: AsyncSession) -> None:
    """Собственно переезд. Все проверки повторяются: между подтверждением и
    нажатием прошло время, и место на сервере мог занять кто-то другой."""
    _, _, rest = call.data.partition(f"{CB_DEVICE}:mvgo:")
    peer_id_s, server_id_s = rest.split(":")
    got = await _own_peer(call, session, int(peer_id_s))
    if got is None:
        return
    peer, user = got

    denied = _cooldown_answer(peer)
    if denied:
        await call.answer(denied, show_alert=True)
        return

    server_id = int(server_id_s)
    groups = await relocate.candidates_for_peer(session, peer, owner=user)
    target = next(
        (s for group in groups.values() for s in group if s.id == server_id), None
    )
    if target is None:
        await call.answer(
            "Место на этом сервере только что заняли — выбери другой.",
            show_alert=True,
        )
        return

    await call.answer("⏳ Переселяю...")
    try:
        new_peer = await relocate.move_peer(
            session, peer, target, owner=user,
            actor_tg_id=user.tg_id, reason="по просьбе юзера",
        )
    except SSHError as exc:
        await session.rollback()
        # Сырой exc юзеру не показываем: пугает и может раскрыть host сервера.
        logger.warning("User peer move failed: {}", exc)
        await call.message.edit_text(
            "⚠️ Не получилось переехать — сервер не ответил. Твой конфиг остался "
            "на прежнем месте и работает. Попробуй ещё раз чуть позже.",
            reply_markup=back_to_devices_kb(),
        )
        return
    except Exception:
        await session.rollback()
        logger.exception("Unexpected user peer move error")
        await call.message.edit_text(t.error_generic, reply_markup=back_to_devices_kb())
        return
    await session.commit()

    labels = await repo.server_labels_map(session)
    await call.message.edit_text(
        t.move_done.format(
            label=peer.label, where_to=labels.get(target.id, target.name)
        ),
        reply_markup=back_to_devices_kb(),
    )
    await ask_config_format(call.message.chat.id, session, new_peer)
```

- [ ] **Шаг 6: Подключить роутер**

В `bot/handlers/__init__.py` добавить `config_move` в импорт (по алфавиту — перед `configs`) и зарегистрировать сразу после `config_delivery`:

```python
    dp.include_router(config_move.router)
```

- [ ] **Шаг 7: Убрать доживающий конфиг из карточки устройства**

В `bot/handlers/devices.py`, в `cb_dev_open` (строки 243-278) заменить сбор `active_peers` и вызов клавиатуры:

```python
    peers = await repo.list_peers_for_device(session, device.id)
    accesses = await repo.list_wdtt_for_device(session, device.id)
    # Доживающий после переезда конфиг (Этап C) в списке не показываем: он уже
    # заменён новым в той же локации, и две строки одной страны читались бы как
    # удвоение. Работать он при этом продолжает — сутки на замену файла есть.
    from bot.services import relocate

    active_peers = relocate.visible_peers(peers)
```

и в конце того же хендлера:

```python
    can_move = bool(active_peers) and active and _sub_active(user)
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=device_card_kb(
            device.id, can_get=active, can_revoke=active,
            locations=locations, can_move=can_move,
        ),
    )
```

В `cb_dev_send` (строки 306-307) — та же фильтрация, чтобы «получить все» не слал старый конфиг:

```python
    from bot.services import relocate

    peers = relocate.visible_peers(await repo.list_peers_for_device(session, device.id))
```

- [ ] **Шаг 8: Прогнать тесты задачи**

Команда: `python -m pytest tests/test_relocate.py -v`
Ожидание: PASS, 24 теста.

- [ ] **Шаг 9: Проверить, что бот собирается**

Команда: `python -c "from bot.handlers import register_handlers; import bot.handlers.config_move"`
Ожидание: без ошибок — проверяет, что роутер подключён и импорты клавиатур сходятся.

- [ ] **Шаг 10: Прогнать весь набор**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py`), остальное PASS.

- [ ] **Шаг 11: Коммит**

```bash
git add bot/handlers/config_move.py bot/handlers/__init__.py bot/handlers/devices.py bot/keyboards/inline/devices.py bot/keyboards/inline/__init__.py bot/texts/ru.py tests/test_relocate.py
git commit -m "Этап C: юзер меняет сервер у конфига

Четыре экрана: какой конфиг → страна → сервер → подтверждение. Про замену
файла в приложении бот пишет ДО переезда, а не после: бесшовного переезда не
бывает, и узнать об этом постфактум — худший вариант.

Отдельный модуль, а не карточка устройств: там своя тема. FSM нет, поэтому
права проверяются в каждом хендлере — peer_id в callback_data подделывается.

Доживающий конфиг из карточки скрыт: две строки одной страны читались бы как
удвоение, хотя старый ещё сутки работает."
```

---

### Задача 7: README и выкатка на прод

README врал в четырёх разделах перед блоком «Живучесть» — правило с тех пор простое: описание обновляется в том же блоке работ, что и код. Плюс сама выкатка: миграция схемы идёт автоматически при старте (`bot/db/migrate.py` добавит три колонки через `ALTER TABLE ADD COLUMN`), но убедиться в этом надо глазами по логам.

**Файлы:**
- Изменить: `README.md`
- Тест: полный прогон + живая проверка на проде

- [ ] **Шаг 1: Обновить README**

Найти абзац про распределение по серверам (`grep -n "Распределение по серверам" README.md`, сейчас строка 27) и дописать в него ёмкость конфигов рядом с ёмкостью обходов:

```markdown
Заполненные сервера (лимит `wdtt_max_accesses` для обхода, `max_peers` для VPN-конфигов — оба правятся в карточке сервера) юзерам не предлагаются ни при выдаче устройства, ни при переезде.
```

И добавить новый пункт в список возможностей, рядом с описанием устройств:

```markdown
- **Смена сервера у конфига** — юзер меняет сервер сам («🔀 Сменить сервер» в карточке устройства: страна → сервер → подтверждение), админ переселяет чужой конфиг из карточки пира («🔀 Переселить», сервер бот подбирает сам в той же локации). Файл конфига при переезде обязательно меняется — у другого сервера свои ключи, — поэтому бот сразу присылает новый и прямо пишет, что старый надо заменить. Старый работает ещё сутки и снимается планировщиком (секция 2d), потом живёт как обычный отозванный до ретеншна 30 дней. Переезжать можно раз в сутки на конфиг; на админа лимит не распространяется.
```

- [ ] **Шаг 2: Прогнать весь набор целиком**

Команда: `python -m pytest --tb=short -q`
Ожидание: **2 failed** (`test_qrgen.py` — PIL), остальное PASS. Итог должен быть **286 passed, 2 failed**: базовые 254 плюс 32 новых (4 в `test_distribution.py`, 4 в `test_admin_nav.py`, 24 в `test_relocate.py`).

- [ ] **Шаг 3: Коммит и пуш**

```bash
git add README.md
git commit -m "README: смена сервера у конфига и потолок конфигов"
git push origin HEAD
```

- [ ] **Шаг 4: Выкатить на прод**

```bash
ssh klopas 'git -C /root/myvpn-bot pull && systemctl restart myvpn-bot'
```

Абсолютный путь через `git -C` обязателен: `ssh klopas` приземляется в `/root`, а не в репозиторий.

- [ ] **Шаг 5: Проверить миграцию и старт по логам**

```bash
ssh klopas 'journalctl -u myvpn-bot -n 60 --no-pager'
```

Ожидание: три строки «Миграция: ALTER TABLE servers ADD COLUMN max_peers…», «…peers ADD COLUMN grace_until…», «…peers ADD COLUMN moved_at…», затем обычный старт без трейсбеков. Если строк про миграцию нет — колонки уже были, это тоже нормально (миграция идемпотентна).

Проверить схему напрямую:

```bash
ssh klopas 'sqlite3 /root/myvpn-bot/data/vpn_bot.sqlite3 "PRAGMA table_info(peers);" | grep -E "grace_until|moved_at"'
ssh klopas 'sqlite3 /root/myvpn-bot/data/vpn_bot.sqlite3 "PRAGMA table_info(servers);" | grep max_peers'
```

База — именно `data/vpn_bot.sqlite3`: команда с `data/bot.db` молча создаст пустышку и «покажет», что всё чисто.

- [ ] **Шаг 6: Дождаться живого тика планировщика**

Через 5-6 минут после рестарта:

```bash
ssh klopas 'journalctl -u myvpn-bot -n 100 --no-pager | grep -iE "section 2d|Grace ended|Traceback"'
```

Ожидание: секция 2d молчит (переехавших конфигов на проде ещё нет) и никаких трейсбеков. Пустой вывод — это успех: секция логирует только когда сняла хотя бы один конфиг.

- [ ] **Шаг 7: Отчитаться Владу**

Что он проверяет в жизни (у него один сервер 🇳🇱, второй не куплен — а переезд без второго сервера в локации показать нечего):
- **Кнопки «🔀 Сменить сервер» в карточке устройства не будет** — переселять некуда, и это правильное поведение, а не баг. Проверить можно на лимите: поставить серверу лимит конфигов ниже текущего числа активных и убедиться, что новое устройство в этой локации конфиг не получает.
- Лимит конфигов: админка → сервер → «👥 Peers сервера» → «✏️ Лимит конфигов».
- Полноценно переезд проверяется только на двух серверах — как и распределение внутри локации (в бэклоге это уже отмечено).

Сказать отдельно: план сузил дизайн-документ в одном месте — переезд в чужую страну предлагается, только если у устройства там ещё нет конфига (см. раздел «Что здесь считается доступным сервером»).

---

## Порядок и зависимости

1. **Задача 1 → задача 2** (лимит правится у поля, которое завела первая).
2. **Задача 1 → задача 3** (`candidates_for_peer` зовёт `has_free_wg_slot`).
3. **Задача 3 → задачи 4, 5, 6** — все три зовут сервис переезда. Между собой 4, 5 и 6 не пересекаются по файлам и могут идти параллельно.
4. **Задача 7 — последней**, после всех.

**Задачи 2 и 5 обе правят `bot/handlers/servers/peers.py`** (в разных местах: 2 — список пиров, 5 — карточку пира) — не запускать одновременно разными агентами.

## Что сознательно не делается

- **Переезд обходов БС (wdtt) не делается.** Дизайн-документ говорит только про конфиги VPN. У обхода другая механика (пароль на сервере, `ctl add -password`), и переезд там — отдельная задача, которую Влад не заказывал.
- **Грейс не настраивается.** Сутки — цифра Влада; ещё две env-переменные ради неё заводить незачем (`GRACE_HOURS`/`COOLDOWN_HOURS` — константы модуля, правятся строкой).
- **Юзеру не приходит уведомление в момент, когда старый конфиг умирает.** Он уже получил предупреждение при переезде («старый работает ещё сутки»), а второе сообщение через сутки — это напоминание о том, что он и так сделал: конфиг заменил сразу, иначе бы не пользовался VPN.
- **Живая нагрузка сервера не измеряется** — потолок ставит админ руками. Решение Влада: иначе выбор сервера у юзера плавал бы от часа к часу.
- **Автоматический переезд при падении сервера не делается.** Бот не умеет отличать «сервер лёг» от «сеть моргнула», и массовое переселение по ложной тревоге хуже, чем ручная кнопка у админа.
- **Сквозная навигация «назад туда, откуда пришёл» из экранов переезда не делается** — все выходы ведут в карточку устройства (юзер) и в список пиров сервера (админ), как и в остальной админке.
- **Локация, где у устройства уже есть конфиг, из переезда исключена** (см. раздел «Что здесь считается доступным сервером») — единственное сужение дизайн-документа, о нём сказать Владу при сдаче.

