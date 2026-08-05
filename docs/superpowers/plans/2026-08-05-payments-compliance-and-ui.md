# Требования платёжки и оформление интерфейса — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Привести бота в соответствие с требованиями платёжного провайдера
(документы, тарифы, контакты, чистка формулировок) и заодно навести порядок в
оформлении интерфейса.

**Architecture:** Пользовательские тексты чистятся под защитой теста-стража,
который разбирает исходники через `ast` и падает на стоп-словах в строковых
литералах. Новые экраны (тарифы, согласие) живут в отдельном роутере
`bot/handlers/legal.py`. Документы — внешние страницы telegra.ph, их адреса
приходят из `.env`. Согласие пишется новой колонкой `users.terms_accepted_at`
через существующий механизм автомиграций.

**Tech Stack:** Python 3.11+, aiogram 3.28.2, SQLAlchemy 2 (async), aiosqlite,
pytest + pytest-asyncio, loguru.

## Global Constraints

- Все пользовательские тексты — на русском, обращение на «ты» (как во всём боте).
- Стоп-слова, запрещённые в пользовательских текстах: `обход`, `блокиров`,
  `белы(й|е|х) спис`, `DPI`, `ТСПУ`, `глушилк`, `LTE`, `ИНН`.
- Раздел wdtt называется «⚡ Резервное подключение». Слово «обход» в
  пользовательских текстах не встречается нигде.
- `PRAGMA foreign_keys` НЕ включать — на выключенных FK держится `user_wipe`.
- Новые колонки добавляются только nullable или с `server_default`, без
  `REFERENCES` — иначе автомиграция `ALTER TABLE ADD COLUMN` на живой базе упадёт.
- Деньги — только в копейках, `int`, никаких `float`.
- Цены на экранах берутся из `bot.services.pricing`, а не пишутся в текстах.
- Админские экраны не трогаем: `bot/keyboards/inline/admin.py`,
  `bot/keyboards/inline/servers.py`, `bot/handlers/admin/`, `bot/handlers/servers/`.
- Прод: `ssh klopas`, сессия садится в `/root`, поэтому git-команды через
  `git -C /root/myvpn-bot`. Боевая база — `data/vpn_bot.sqlite3`.
- Тесты запускаются из корня репозитория: `python -m pytest`.

## Структура файлов

| Файл | Ответственность |
|---|---|
| `tests/test_wording.py` | создать — страж стоп-слов в пользовательских текстах |
| `bot/texts/ru.py` | правка — чистка формулировок + тексты новых экранов |
| `bot/keyboards/inline/menu.py` | правка — новая раскладка, цвета, кнопки документов |
| `bot/keyboards/inline/legal.py` | создать — клавиатуры согласия и тарифов |
| `bot/keyboards/inline/prefixes.py` | правка — префикс `CB_LEGAL` |
| `bot/keyboards/inline/__init__.py` | правка — реэкспорт новых клавиатур |
| `bot/keyboards/inline/devices.py`, `wdtt.py`, `balance.py` | правка — подписи и цвета |
| `bot/handlers/legal.py` | создать — экран тарифов, экран согласия, гейт |
| `bot/handlers/common.py` | правка — гейт согласия в `/start`, статус подписки в меню |
| `bot/handlers/devices.py`, `balance.py`, `wdtt.py` | правка — чистка формулировок |
| `bot/services/scheduler.py` | правка — два оповещения об истечении |
| `bot/db/models.py` | правка — `User.terms_accepted_at` |
| `bot/db/repo/users.py` | правка — `accept_terms()` |
| `bot/config.py` | правка — URL документов, флаг премиум-эмодзи |
| `docs/legal/privacy.md`, `docs/legal/terms.md` | создать — тексты документов |
| `scripts/publish_legal.py` | создать — публикация документов на telegra.ph |

---

### Task 1: Тест-страж стоп-слов

Пишем страж ДО чистки: он покажет полный список мест и после правок не даст
формулировкам вернуться.

**Files:**
- Test: `tests/test_wording.py` (создать)

**Interfaces:**
- Consumes: ничего
- Produces: `tests/test_wording.py::test_no_forbidden_wording` — падает, пока в
  пользовательских строковых литералах есть стоп-слова.

- [ ] **Step 1: Написать падающий тест**

```python
"""Страж формулировок: платёжный провайдер требует, чтобы в текстах для
пользователя не было упоминаний обхода блокировок, DPI, ТСПУ, белых списков,
LTE и ИНН (требование законодательства РФ для VPN-проектов).

Разбираем файлы через ast и смотрим ТОЛЬКО строковые литералы: комментарии и
докстринги — внутренняя кухня, их чистить не нужно и незачем.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Файлы, которые видит пользователь.
SCAN_GLOBS = [
    "bot/texts/*.py",
    "bot/keyboards/inline/*.py",
    "bot/handlers/*.py",
    "bot/services/scheduler.py",
]

# Админские экраны: их видит только Влад, формулировки там остаются.
EXCLUDE = {
    "bot/keyboards/inline/admin.py",
    "bot/keyboards/inline/servers.py",
    "bot/keyboards/inline/install.py",
    "bot/handlers/install.py",
}

FORBIDDEN = re.compile(
    r"обход|блокиров|белы[йех]\s+спис|\bDPI\b|ТСПУ|глушилк|\bLTE\b|\bИНН\b",
    re.IGNORECASE,
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() узлов-докстрингов — их из проверки исключаем."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _user_facing_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCAN_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            if rel in EXCLUDE or path.name == "__init__.py":
                continue
            files.append(path)
    return files


@pytest.mark.parametrize(
    "path", _user_facing_files(), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_no_forbidden_wording(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)

    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        match = FORBIDDEN.search(node.value)
        if match:
            hits.append(f"строка {node.lineno}: «{match.group(0)}» в {node.value[:70]!r}")

    assert not hits, "Запрещённые формулировки:\n" + "\n".join(hits)


def test_scanner_sees_files() -> None:
    """Защита от опечатки в глобах: если список пуст, страж молча зеленеет."""
    assert len(_user_facing_files()) >= 8
```

- [ ] **Step 2: Запустить и убедиться, что тест падает**

Run: `python -m pytest tests/test_wording.py -v`
Expected: FAIL на `bot/texts/ru.py`, `bot/keyboards/inline/menu.py`,
`bot/keyboards/inline/devices.py`, `bot/keyboards/inline/wdtt.py`,
`bot/handlers/devices.py`, `bot/handlers/balance.py`, `bot/handlers/wdtt.py`,
`bot/services/scheduler.py`. `test_scanner_sees_files` — PASS.

Выпиши список падений: это чек-лист для задач 2 и 3.

- [ ] **Step 3: Коммит**

```bash
git -C /root/myvpn-bot add tests/test_wording.py
git -C /root/myvpn-bot commit -m "Страж формулировок: тест на стоп-слова в текстах для юзера"
```

---

### Task 2: Переименование раздела в текстах и клавиатурах

**Files:**
- Modify: `bot/texts/ru.py` (блоки `start_user`, `help_text`, `device_created`,
  весь блок `# ---------- Обход белых списков (wdtt) ----------`)
- Modify: `bot/keyboards/inline/menu.py:21`
- Modify: `bot/keyboards/inline/devices.py:146-149`
- Modify: `bot/keyboards/inline/wdtt.py` (докстринг модуля можно оставить,
  правим подписи кнопок в строках 43 и 77)

**Interfaces:**
- Consumes: `tests/test_wording.py` из задачи 1
- Produces: текстовые константы `t.wdtt_*` с новыми формулировками; подпись
  кнопки главного меню «⚡ Резервное подключение»

- [ ] **Step 1: Переписать `start_user` в `bot/texts/ru.py`**

```python
    start_user = (
        "👋 <b>Привет, {name}!</b>\n\n"
        "Это <b>Moschata VPN</b> (читается «Моска́та») — твой личный VPN: "
        "быстрый, стабильный, с серверами в нескольких странах.\n"
        "<i>Наш маскот — выхухоль: её никто не может поймать, и твой трафик "
        "тоже.</i>\n\n"
        "🎁 <b>Первые {trial_days} дней — бесплатно</b> ({trial_devices} устройство, "
        "{trial_gb} ГБ) — пробный период уже включён. Дальше — от {base_price}/мес.\n\n"
        "Подключиться просто:\n"
        "1. Жми «📱 Мои устройства» и добавь телефон или компьютер.\n"
        "2. Я пришлю всё нужное и пошаговую инструкцию — поставишь бесплатное "
        "приложение <b>AmneziaVPN</b> и подключишься за пару минут.\n\n"
        "Ещё в меню: ⚡ <b>Резервное подключение</b> — второй способ выйти в сеть, "
        "если основной работает неустойчиво, и 🎫 <b>Моя подписка</b> — твои "
        "сроки и лимиты.\n\n"
        "Выбирай в меню ниже 👇"
    )
```

- [ ] **Step 2: Переписать хвост `help_text`**

Заменить последние две строки (`"Нужен обход «белых списков»…"`) на:

```python
        "Основное подключение работает нестабильно — попробуй раздел "
        "«⚡ Резервное подключение».\n"
        "Сроки и лимиты — в «🎫 Моя подписка»."
```

- [ ] **Step 2b: Переписать шапку `help_text`**

Провайдер требует явно обозначенный канал обращений. Первый абзац `help_text`
меняется на:

```python
    help_text = (
        "🆘 <b>Поддержка Moschata VPN</b>\n\n"
        "Это официальный канал обращений сервиса: вопросы по работе, оплате, "
        "продлению и возвратам. Жми «✍️ Написать в поддержку» и опиши "
        "проблему — ответ придёт прямо в этот чат."
        "{contact_block}\n\n"
```

Время ответа цифрой не обещаем — сервис ведёт один человек. Остальная часть
`help_text` (инструкция по установке) не меняется, кроме хвоста из шага 2.

- [ ] **Step 3: Переписать хвост `device_created`**

Заменить последний абзац на:

```python
        "<i>Резервное подключение для этого устройства подключается отдельно — "
        "раздел «⚡ Резервное подключение».</i>"
```

- [ ] **Step 4: Переписать блок wdtt-текстов целиком**

```python
    # ---------- Резервное подключение (wdtt) ----------
    # Вход в раздел: объясняем нетехнарю, что это и чем отличается от VPN.
    wdtt_intro = (
        "⚡ <b>Резервное подключение</b>\n\n"
        "Бывает, что основное подключение работает неустойчиво: низкая "
        "скорость, обрывы, страницы не открываются. Резервное подключение "
        "использует другую технологию и в таких условиях часто держится "
        "лучше.\n"
        "Для него нужно <b>отдельное приложение</b> (не AmneziaVPN) — бот "
        "пришлёт ссылку и инструкцию, когда создашь доступ.\n\n"
        "Доступов: <b>{used}/{limit}</b>"
    )
    wdtt_pick_server = "🌍 В какой локации создать резервное подключение?"
    wdtt_pick_device = (
        "📱 <b>К какому устройству привязать резервное подключение?</b>\n\n"
        "Оно идёт в паре с устройством из подписки — выбери то, на котором "
        "будешь им пользоваться. Работает в отдельном приложении, не внутри "
        "AmneziaVPN."
    )
    wdtt_ask_vk = (
        "⚙️ <b>Дополнительная настройка</b>\n\n"
        "Обычно ничего менять не нужно — просто жми "
        "<b>«⚡ Рекомендуемый вариант»</b>, всё уже настроено.\n\n"
        "<i>Свой адрес подключения указывают в редких случаях — например, "
        "если об этом попросила поддержка.</i>"
    )
    wdtt_ask_vk_link = (
        "🔗 Пришли <b>адрес подключения</b>, который дала поддержка "
        "(можно без <code>https://</code>):"
    )
    wdtt_ask_platform = (
        "📱 <b>На чём будешь пользоваться резервным подключением?</b>\n\n"
        "От этого зависит, какое приложение понадобится — его название и ссылку "
        "пришлю вместе со ссылкой для подключения."
    )
    wdtt_creating = "⏳ Настраиваю резервное подключение..."
    # {app_block} — строка «где взять приложение» (URL либо совет про поддержку),
    # собирается в хендлере из _PLATFORMS.
    wdtt_created = (
        "⚡ <b>Готово! Резервное подключение создано.</b>\n\n"
        "• Устройство: <code>{label}</code>\n"
        "• Локация: <b>{server}</b>\n\n"
        "<b>Осталось 2 шага:</b>\n"
        "1️⃣ Установи приложение <b>{app}</b> — резервное подключение работает "
        "через него, а не через AmneziaVPN:\n{app_block}\n"
        "2️⃣ Скопируй ссылку (просто нажми на неё) и вставь в приложение:\n"
        "<code>{link}</code>\n\n"
        "💡 Включай его, когда основное подключение работает плохо."
    )
    # {app_line} — «Импортируй её в …» с именем приложения под платформу доступа
    # (или общее перечисление для старых доступов без платформы).
    wdtt_link = (
        "🔗 <b>Твоя ссылка для резервного подключения</b> "
        "(нажми, чтобы скопировать):\n\n"
        "<code>{link}</code>\n\n"
        "{app_line}"
    )
    wdtt_revoked = "🗑 Резервное подключение <code>{label}</code> отключено."
    wdtt_disabled = (
        "Резервное подключение временно недоступно. Напиши в поддержку через "
        "меню («🆘 Поддержка») — разберёмся."
    )
```

- [ ] **Step 5: Поправить подписи кнопок**

`bot/keyboards/inline/menu.py:21`:

```python
    kb.button(text="⚡ Резервное подключение", callback_data=f"{CB_WDTT}:my")
```

`bot/keyboards/inline/devices.py:146-149` — докстринг и кнопка:

```python
    """После создания устройства: текст t.device_created отсылает к разделу
    «⚡ Резервное подключение» — даём кнопку туда сразу."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Резервное подключение", callback_data=f"{CB_WDTT}:my")
```

`bot/keyboards/inline/wdtt.py:43` и `:77`:

```python
        kb.button(text="➕ Добавить резервное подключение", callback_data=f"{CB_WDTT}:new")
```

```python
    kb.button(text="« Мои подключения", callback_data=f"{CB_WDTT}:my")
```

- [ ] **Step 6: Прогнать страж и весь набор тестов**

Run: `python -m pytest tests/test_wording.py -v`
Expected: `bot/texts/ru.py`, `bot/keyboards/inline/menu.py`,
`bot/keyboards/inline/devices.py`, `bot/keyboards/inline/wdtt.py` — PASS.
Остальные файлы пока падают, это задача 3.

Run: `python -m pytest -q`
Expected: прежние тесты зелёные (кроме известных падений стража).

- [ ] **Step 7: Коммит**

```bash
git -C /root/myvpn-bot add bot/texts/ru.py bot/keyboards/inline/
git -C /root/myvpn-bot commit -m "Раздел обхода переименован в «Резервное подключение»"
```

---

### Task 3: Чистка оставшихся пользовательских мест

**Files:**
- Modify: `bot/handlers/devices.py:95`, `:269`, `:452`, `:465`
- Modify: `bot/handlers/balance.py:120`, `:468-474`, `:534-536`
- Modify: `bot/handlers/wdtt.py` (докстринг модуля оставляем, правим строки в коде)
- Modify: `bot/services/scheduler.py:203`, `:260`

**Interfaces:**
- Consumes: тексты из задачи 2
- Produces: страж стоп-слов зелёный целиком

- [ ] **Step 1: `bot/handlers/devices.py`**

Строка 95:

```python
            "\n\nВ твоём тарифе сейчас нет устройств — только резервное "
            "подключение. "
```

Строка 269:

```python
    lines.append(f"• Резервных подключений: <b>{len(active_acc)}</b>")
```

Строка 452:

```python
        f"• Резервное подключение: <b>{bypass}/{user.sub_max_bypass}</b>\n"
```

Строка 465:

```python
            "1 резервное подключение)."
```

- [ ] **Step 2: `bot/handlers/balance.py`**

Строка 120:

```python
        "<i>Суммы на кнопках — стоимость базового тарифа (1 устройство + "
        "1 резервное подключение) на месяц, 3 месяца, полгода и год.</i>",
```

Строки 468-474:

```python
        f"Считаем просто: первая позиция (устройство или резервное "
        f"подключение) — "
        f"<b>{first_rub} ₽/мес</b>, каждая следующая — "
        f"<b>+{settings.price_extra_device_rub} ₽/мес</b>. Не нужны устройства "
        "или резервные подключения — смело ставь 0.\n\n"
        "Твой тариф:\n"
        f"📱 Устройств: <b>{devices}</b>\n"
        f"⚡ Резервных подключений: <b>{bypass}</b>\n"
```

Строки 534-536:

```python
            f"У тебя сейчас активно {used_dev} устр. и {used_byp} резервных — "
            "тариф не может быть меньше. Сначала удали лишнее в «📱 Мои "
            "устройства» / «⚡ Резервное подключение».",
```

- [ ] **Step 3: `bot/services/scheduler.py`**

Строка 203:

```python
                        "⏱ <b>Подписка закончилась</b> — VPN и резервное "
                        "подключение встали на паузу.\n"
```

Строка 260:

```python
                                "Продли, чтобы устройства и резервное "
                                "подключение не отключились."
```

- [ ] **Step 4: `bot/handlers/wdtt.py`**

Проверить строковые литералы, на которые ругается страж, и заменить «обход» на
«резервное подключение». Докстринг модуля и комментарии не трогать.

Run: `python -m pytest tests/test_wording.py::test_no_forbidden_wording -v`
и читать список падений — он точно укажет строки.

- [ ] **Step 5: Страж должен позеленеть полностью**

Run: `python -m pytest tests/test_wording.py -v`
Expected: все PASS.

Run: `python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 6: Коммит**

```bash
git -C /root/myvpn-bot add bot/handlers/ bot/services/scheduler.py
git -C /root/myvpn-bot commit -m "Чистка формулировок в устройствах, балансе и оповещениях"
```

---

### Task 4: Экран «💳 Тарифы»

**Files:**
- Create: `bot/handlers/legal.py`
- Create: `bot/keyboards/inline/legal.py`
- Modify: `bot/keyboards/inline/prefixes.py`
- Modify: `bot/keyboards/inline/__init__.py`
- Modify: `bot/texts/ru.py`
- Modify: `bot/handlers/__init__.py`
- Test: `tests/test_legal.py` (создать)

**Interfaces:**
- Consumes: `bot.services.pricing.monthly_price_kopeks`, `term_price_kopeks`,
  `fmt_rub`, `TERM_DISCOUNTS`, `TERM_LABELS`
- Produces: `bot.handlers.legal.build_tariffs_text() -> str`;
  `CB_LEGAL = "leg"`; callback `leg:tariffs`

- [ ] **Step 1: Написать падающий тест**

```python
"""Экран тарифов: цифры берутся из pricing, а не пишутся руками."""
from __future__ import annotations

import pytest

from bot.handlers.legal import build_tariffs_text
from bot.services.pricing import fmt_rub, monthly_price_kopeks, term_price_kopeks


def test_tariffs_text_shows_base_price() -> None:
    text = build_tariffs_text()
    assert fmt_rub(monthly_price_kopeks(1, 1)) in text


def test_tariffs_text_shows_term_prices() -> None:
    """Скидочные суммы за 3/6/12 месяцев названы явно — банк требует, чтобы
    было понятно, сколько и за что платит клиент."""
    text = build_tariffs_text()
    monthly = monthly_price_kopeks(1, 1)
    for months in (3, 6, 12):
        assert fmt_rub(term_price_kopeks(monthly, months)) in text


def test_tariffs_text_has_no_forbidden_wording() -> None:
    assert "обход" not in build_tariffs_text().lower()
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m pytest tests/test_legal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.handlers.legal'`

- [ ] **Step 3: Добавить префикс**

`bot/keyboards/inline/prefixes.py`:

```python
CB_LEGAL = "leg"   # тарифы, документы, согласие с условиями
```

- [ ] **Step 4: Текст-шаблон в `bot/texts/ru.py`**

```python
    # ---------- Тарифы и документы ----------
    tariffs = (
        "💳 <b>Тарифы Moschata VPN</b>\n\n"
        "Подписка складывается из позиций:\n"
        "• 1 устройство <b>или</b> 1 резервное подключение — {first}/мес\n"
        "• каждая следующая позиция — +{extra}/мес\n\n"
        "Пример: 1 устройство + 1 резервное подключение = <b>{base}/мес</b>\n\n"
        "<b>При оплате сразу за несколько месяцев:</b>\n"
        "{terms}\n"
        "🎁 Первые {trial_days} дней — бесплатно ({trial_devices} устройство, "
        "{trial_gb} ГБ).\n\n"
        "Оплата — с внутреннего баланса, пополнить можно в разделе "
        "«💰 Баланс». Подписка продлевается автоматически; автопродление "
        "отключается в разделе «🎫 Моя подписка»."
    )
```

- [ ] **Step 5: Реализация `bot/handlers/legal.py`**

```python
"""Юридические экраны: тарифы, документы, согласие с условиями.

Отдельный роутер, потому что это требование платёжного провайдера, а не часть
продуктовой логики: тарифы и документы должны быть доступны из главного меню
всегда, без подписки и без оплаты.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.keyboards.inline import CB_LEGAL, back_to_menu
from bot.services.pricing import (
    TERM_DISCOUNTS,
    TERM_LABELS,
    fmt_rub,
    monthly_price_kopeks,
    term_price_kopeks,
)
from bot.texts import t

router = Router(name="legal")


def build_tariffs_text() -> str:
    """Экран тарифов. Все суммы считаются из pricing: поменяются цены в .env —
    экран обновится сам, без правки текстов."""
    base = monthly_price_kopeks(1, 1)
    first = monthly_price_kopeks(1, 0)

    lines = []
    for months in (3, 6, 12):
        price = term_price_kopeks(base, months)
        discount = TERM_DISCOUNTS.get(months, 0)
        lines.append(
            f"• {TERM_LABELS[months]} — <b>{fmt_rub(price)}</b> (−{discount}%)"
        )

    return t.tariffs.format(
        first=fmt_rub(first),
        extra=fmt_rub(settings.price_extra_device_rub * 100),
        base=fmt_rub(base),
        terms="\n".join(lines),
        trial_days=settings.trial_days,
        trial_devices=settings.trial_devices,
        trial_gb=settings.trial_traffic_gb,
    )


@router.callback_query(F.data == f"{CB_LEGAL}:tariffs")
async def cb_tariffs(call: CallbackQuery) -> None:
    await call.message.edit_text(build_tariffs_text(), reply_markup=back_to_menu())
    await call.answer()
```

- [ ] **Step 6: Реэкспорт префикса**

В `bot/keyboards/inline/__init__.py` добавить `CB_LEGAL` в импорт из
`bot.keyboards.inline.prefixes` и в `__all__`, если он там есть.

- [ ] **Step 7: Подключить роутер**

`bot/handlers/__init__.py`, рядом с остальными:

```python
    dp.include_router(legal.router)
```

и импорт `legal` в шапке модуля.

- [ ] **Step 8: Тесты зелёные**

Run: `python -m pytest tests/test_legal.py -v`
Expected: 3 PASS

- [ ] **Step 9: Коммит**

```bash
git -C /root/myvpn-bot add bot/handlers/legal.py bot/keyboards/inline/ bot/texts/ru.py bot/handlers/__init__.py tests/test_legal.py
git -C /root/myvpn-bot commit -m "Экран тарифов: цены и скидки из pricing"
```

---

### Task 5: Тексты документов и публикация на telegra.ph

**Files:**
- Create: `docs/legal/privacy.md`
- Create: `docs/legal/terms.md`
- Create: `scripts/publish_legal.py`

**Interfaces:**
- Consumes: ничего
- Produces: два URL telegra.ph, которые попадут в `.env` в задаче 6

- [ ] **Step 1: Написать политику конфиденциальности**

`docs/legal/privacy.md`. Формат: первая строка — заголовок `# ...`, дальше
абзацы и подзаголовки `## ...`. От имени «сервиса Moschata VPN», без ФИО, ИНН
и адреса. Обязательные разделы:

- какие данные собираются: Telegram ID, имя и username, история платежей и
  баланс, объём переданного трафика, дата и время операций;
- какие данные НЕ собираются: история посещённых сайтов, содержимое трафика,
  логи подключений, IP-адреса посещаемых ресурсов;
- цели обработки: предоставление доступа к сервису, учёт оплаты и лимитов,
  поддержка;
- срок хранения и удаление данных по запросу через поддержку;
- передача третьим лицам: платёжный сервис получает только сведения о факте
  и сумме платежа;
- как связаться: раздел «🆘 Поддержка» внутри бота;
- дата вступления в силу: 05.08.2026.

- [ ] **Step 2: Написать пользовательское соглашение**

`docs/legal/terms.md`, те же требования к оформлению. Разделы:

- предмет: доступ к VPN-сервису по подписке, услуга оказывается «как есть»;
- регистрация и подтверждение согласия с условиями при первом входе;
- пробный период: срок и лимиты по умолчанию;
- оплата: пополнение внутреннего баланса, списание за выбранный тариф,
  автопродление и его отключение;
- возврат неиспользованного остатка через обращение в поддержку;
- обязанности пользователя: не использовать сервис для действий, нарушающих
  законодательство; не передавать доступы третьим лицам;
- основания приостановки доступа;
- ограничение ответственности за перебои у третьих лиц (хостинг, операторы);
- порядок изменения условий и уведомления об этом;
- дата вступления в силу: 05.08.2026.

- [ ] **Step 3: Показать тексты Владу и дождаться вычитки**

Прежде чем публиковать — тексты уходят наружу и потом индексируются. Влад
читает оба и подтверждает.

- [ ] **Step 4: Скрипт публикации**

`scripts/publish_legal.py`:

```python
"""Публикация юридических документов на telegra.ph.

Первый запуск создаёт аккаунт и печатает access_token — его нужно положить в
.env как TELEGRAPH_TOKEN, иначе страницы потом не отредактировать. При
последующих запусках с TELEGRAPH_TOKEN и --path страница обновляется, а не
создаётся заново (адрес не меняется — он уже будет прописан в кнопках бота).

Запуск:
    python scripts/publish_legal.py docs/legal/privacy.md
    python scripts/publish_legal.py docs/legal/terms.md --path Politika-...-08-05
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegra.ph"


def _call(method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
         for k, v in params.items()}
    ).encode()
    with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=30) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise SystemExit(f"telegra.ph: {payload.get('error')}")
    return payload["result"]


def md_to_nodes(text: str) -> tuple[str, list]:
    """Очень простой конвертер: '# ' — заголовок страницы, '## ' — h3,
    остальное — абзацы. Списков в документах не делаем намеренно."""
    title = ""
    nodes: list = []
    for raw in text.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        if block.startswith("# ") and not title:
            title = block[2:].strip()
            continue
        if block.startswith("## "):
            nodes.append({"tag": "h3", "children": [block[3:].strip()]})
            continue
        nodes.append({"tag": "p", "children": [block.replace("\n", " ")]})
    return title, nodes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--path", default=None, help="path существующей страницы")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAPH_TOKEN", "")
    if not token:
        account = _call(
            "createAccount",
            {"short_name": "Moschata", "author_name": "Moschata VPN"},
        )
        token = account["access_token"]
        print("СОХРАНИ В .env:  TELEGRAPH_TOKEN=" + token)

    title, nodes = md_to_nodes(args.source.read_text(encoding="utf-8"))
    params = {
        "access_token": token,
        "title": title,
        "author_name": "Moschata VPN",
        "content": nodes,
        "return_content": False,
    }
    if args.path:
        params["path"] = args.path
        page = _call("editPage", params)
    else:
        page = _call("createPage", params)
    print("URL:", page["url"])
    print("path:", page["path"])


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Проверить конвертер без публикации**

Run:
```bash
python -c "
from pathlib import Path
import sys; sys.path.insert(0, 'scripts')
from publish_legal import md_to_nodes
title, nodes = md_to_nodes(Path('docs/legal/privacy.md').read_text())
print(title); print(len(nodes), 'блоков'); print(nodes[0])
"
```
Expected: заголовок непустой, блоков больше десяти, первый блок — словарь с
`tag`.

- [ ] **Step 6: Опубликовать обе страницы**

Run: `python scripts/publish_legal.py docs/legal/privacy.md`
Затем: `python scripts/publish_legal.py docs/legal/terms.md`

Записать `TELEGRAPH_TOKEN` и оба URL — они нужны в задаче 6. Токен положить
в `.env` на проде (он попадает в бэкап вместе с `.env`).

- [ ] **Step 7: Коммит**

```bash
git -C /root/myvpn-bot add docs/legal/ scripts/publish_legal.py
git -C /root/myvpn-bot commit -m "Политика конфиденциальности и пользовательское соглашение"
```

---

### Task 6: Кнопки документов в главном меню

**Files:**
- Modify: `bot/config.py`
- Modify: `bot/keyboards/inline/menu.py`
- Modify: `.env.example`
- Test: `tests/test_legal.py` (дополнить)

**Interfaces:**
- Consumes: URL из задачи 5
- Produces: `settings.legal_privacy_url`, `settings.legal_terms_url`;
  главное меню с кнопками документов

- [ ] **Step 1: Дописать падающий тест**

```python
def test_menu_has_legal_buttons(monkeypatch) -> None:
    """Кнопки документов появляются, когда адреса заданы — банк требует
    постоянного доступа к ним из бота."""
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "https://telegra.ph/p")
    monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/t")

    urls = [
        b.url
        for row in main_menu(is_admin=False).inline_keyboard
        for b in row
        if b.url
    ]
    assert "https://telegra.ph/p" in urls
    assert "https://telegra.ph/t" in urls


def test_menu_without_legal_urls_has_no_dead_buttons(monkeypatch) -> None:
    """Адреса не заданы — кнопок нет: пустая ссылка ломает отправку меню."""
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")

    assert not [
        b for row in main_menu(is_admin=False).inline_keyboard for b in row if b.url
    ]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m pytest tests/test_legal.py -v`
Expected: FAIL — у `Settings` нет атрибута `legal_privacy_url`

- [ ] **Step 3: Настройки в `bot/config.py`**

```python
    # ── Юридические документы (требование платёжного провайдера) ─────────────
    # Адреса страниц на telegra.ph. Пусто = кнопки в меню не показываются.
    legal_privacy_url: str = ""
    legal_terms_url: str = ""
    # Премиум-эмодзи в текстах и на кнопках. Работают ТОЛЬКО если у владельца
    # бота активен Telegram Premium (правило Bot API от 09.02.2026); без него Telegram
    # молча выбрасывает их, подставляя обычный символ. Проверено 05.08.2026:
    # прав нет, поэтому по умолчанию выключено.
    premium_emoji_enabled: bool = False
```

- [ ] **Step 4: Кнопки в меню**

В `main_menu()` перед кнопкой поддержки:

```python
    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
```

Импорт `settings` — внутри функции, как это уже сделано в `common.py`, чтобы
не тянуть конфиг в момент импорта клавиатур.

- [ ] **Step 5: Дописать `.env.example`**

```
# Юридические документы (кнопки в главном меню). Пусто = кнопок нет.
LEGAL_PRIVACY_URL=
LEGAL_TERMS_URL=
# Токен telegra.ph — нужен, чтобы редактировать уже опубликованные страницы.
TELEGRAPH_TOKEN=
# Премиум-эмодзи (нужен Telegram Premium у владельца бота).
PREMIUM_EMOJI_ENABLED=false
```

- [ ] **Step 6: Тесты**

Run: `python -m pytest tests/test_legal.py -v`
Expected: все PASS

- [ ] **Step 7: Коммит**

```bash
git -C /root/myvpn-bot add bot/config.py bot/keyboards/inline/menu.py .env.example tests/test_legal.py
git -C /root/myvpn-bot commit -m "Кнопки документов в главном меню"
```

---

### Task 7: Экран согласия при первом входе

**Files:**
- Modify: `bot/db/models.py` (класс `User`)
- Modify: `bot/db/repo/users.py`
- Modify: `bot/db/repo/__init__.py` (реэкспорт)
- Modify: `bot/texts/ru.py`
- Create: клавиатуры в `bot/keyboards/inline/legal.py`
- Modify: `bot/handlers/legal.py`, `bot/handlers/common.py`
- Test: `tests/test_consent.py` (создать)

**Interfaces:**
- Consumes: `settings.legal_privacy_url`, `settings.legal_terms_url`
- Produces: `User.terms_accepted_at: datetime | None`;
  `repo.accept_terms(session, user) -> None`;
  `bot.keyboards.inline.consent_kb() -> InlineKeyboardMarkup`;
  callbacks `leg:accept`, `leg:decline`

- [ ] **Step 1: Написать падающие тесты**

```python
"""Согласие с условиями при первом входе (требование платёжного провайдера)."""
from __future__ import annotations

import pytest

from bot.db import repo


@pytest.mark.asyncio
async def test_new_user_has_no_consent(session) -> None:
    user = await repo.get_or_create_user(
        session, tg_id=9001, username="new", full_name="Новый"
    )
    assert user.terms_accepted_at is None


@pytest.mark.asyncio
async def test_accept_terms_writes_timestamp(session) -> None:
    user = await repo.get_or_create_user(
        session, tg_id=9002, username="new", full_name="Новый"
    )
    await repo.accept_terms(session, user)
    assert user.terms_accepted_at is not None


@pytest.mark.asyncio
async def test_accept_terms_is_idempotent(session) -> None:
    """Повторное нажатие «Согласен» не переписывает исходную дату — она может
    понадобиться при разборе спора об оплате."""
    user = await repo.get_or_create_user(
        session, tg_id=9003, username="new", full_name="Новый"
    )
    await repo.accept_terms(session, user)
    first = user.terms_accepted_at
    await repo.accept_terms(session, user)
    assert user.terms_accepted_at == first


def test_consent_keyboard_has_both_documents(monkeypatch) -> None:
    from bot.config import settings
    from bot.keyboards.inline import consent_kb

    monkeypatch.setattr(settings, "legal_privacy_url", "https://telegra.ph/p")
    monkeypatch.setattr(settings, "legal_terms_url", "https://telegra.ph/t")

    kb = consent_kb()
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row
                 if b.callback_data]
    assert "https://telegra.ph/p" in urls
    assert "https://telegra.ph/t" in urls
    assert any(c.endswith(":accept") for c in callbacks)
    assert any(c.endswith(":decline") for c in callbacks)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m pytest tests/test_consent.py -v`
Expected: FAIL — у `User` нет `terms_accepted_at`, у `repo` нет `accept_terms`

- [ ] **Step 3: Колонка в модели**

В классе `User` рядом с `created_at`:

```python
    # Когда юзер принял условия (экран согласия при первом входе). NULL —
    # либо ещё не принял, либо пользовался ботом до появления экрана: таких
    # не трогаем, гейт только для новых. Колонку добавляет автомиграция
    # ALTER TABLE ADD COLUMN, поэтому она nullable и без REFERENCES.
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
```

- [ ] **Step 4: Репозиторий**

`bot/db/repo/users.py`:

```python
async def accept_terms(session: AsyncSession, user: User) -> None:
    """Фиксирует согласие с условиями. Повторный вызов дату не переписывает:
    важна ПЕРВАЯ, именно её показывают при разборе спора об оплате."""
    if user.terms_accepted_at is not None:
        return
    user.terms_accepted_at = datetime.now(timezone.utc)
    await session.commit()
```

Добавить `accept_terms` в реэкспорт `bot/db/repo/__init__.py` рядом с
остальными функциями из `users`.

- [ ] **Step 5: Тексты**

`bot/texts/ru.py`:

```python
    consent_intro = (
        "👋 <b>Привет!</b>\n\n"
        "Прежде чем начать, ознакомься с условиями работы сервиса — они "
        "по кнопкам ниже.\n\n"
        "Оплата подписки или начало использования сервиса означает принятие "
        "этих условий.\n\n"
        "Нажми «Согласен», чтобы продолжить."
    )
    consent_declined = (
        "Без принятия условий пользоваться сервисом не получится.\n\n"
        "Передумаешь — просто отправь /start ещё раз."
    )
```

- [ ] **Step 6: Клавиатура**

`bot/keyboards/inline/legal.py`:

```python
"""Клавиатуры юридических экранов: согласие с условиями."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.inline.prefixes import CB_LEGAL


def consent_kb() -> InlineKeyboardMarkup:
    """Документы ссылками + «Согласен»/«Не согласен». Ссылки показываем, только
    если адреса заданы: кнопка с пустым url ломает отправку сообщения."""
    from bot.config import settings

    kb = InlineKeyboardBuilder()
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
    kb.button(text="Согласен", callback_data=f"{CB_LEGAL}:accept", style="success")
    kb.button(text="Не согласен", callback_data=f"{CB_LEGAL}:decline", style="danger")
    kb.adjust(1, 1, 2)
    return kb.as_markup()
```

Реэкспорт `consent_kb` в `bot/keyboards/inline/__init__.py`.

- [ ] **Step 7: Проверить, что `style` проходит через builder**

Run:
```bash
python -c "
from aiogram.utils.keyboard import InlineKeyboardBuilder
kb = InlineKeyboardBuilder()
kb.button(text='x', callback_data='y', style='success')
print(kb.as_markup().inline_keyboard[0][0].style)
"
```
Expected: `success`

Если упадёт — собирать `InlineKeyboardMarkup` напрямую из
`InlineKeyboardButton(...)`, минуя builder, и так же поступить в задаче 8.

- [ ] **Step 8: Хендлеры согласия**

В `bot/handlers/legal.py` дописать импорты, которых там ещё нет (файл создан в
задаче 4 без работы с БД):

```python
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.inline import CB_LEGAL, back_to_menu, consent_kb
```

и сами хендлеры:

```python
@router.callback_query(F.data == f"{CB_LEGAL}:accept")
async def cb_accept(call: CallbackQuery, session: AsyncSession) -> None:
    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await repo.accept_terms(session, user)
    await call.message.delete()
    from bot.handlers.common import send_start_screens

    await send_start_screens(call.message, user, is_new=True)
    await call.answer()


@router.callback_query(F.data == f"{CB_LEGAL}:decline")
async def cb_decline(call: CallbackQuery) -> None:
    await call.message.edit_text(t.consent_declined)
    await call.answer()
```

- [ ] **Step 9: Гейт в `/start`**

В `bot/handlers/common.py` вынести отправку меню и подсказки в общую функцию
и добавить проверку согласия:

```python
async def send_start_screens(message: Message, user, *, is_new: bool) -> None:
    """Главное меню + подсказка новичку. Вызывается и из /start, и после
    нажатия «Согласен» на экране условий."""
    await _send_main_menu(message, user.is_admin)
    await _send_onboarding_hint(message, is_new=is_new, is_admin=user.is_admin)


async def _needs_consent(user) -> bool:
    """Гейт только для НОВЫХ: у тех, кто пользовался ботом до появления экрана,
    terms_accepted_at пустой, но дёргать их лишним вопросом не будем —
    отличаем по дате регистрации относительно даты выката."""
    from bot.config import settings

    if user.terms_accepted_at is not None:
        return False
    if not (settings.legal_terms_url or settings.legal_privacy_url):
        return False
    return user.created_at is not None and user.created_at >= CONSENT_SINCE
```

`CONSENT_SINCE` — константа с датой выката (UTC), задаётся в момент деплоя:

```python
# Экран согласия показываем только тем, кто зарегистрировался начиная с этой
# даты. Действующих юзеров не дёргаем: они пришли до появления требования.
CONSENT_SINCE = datetime(2026, 8, 5, tzinfo=timezone.utc)
```

В обоих `/start`-хендлерах перед отправкой меню:

```python
    if await _needs_consent(user):
        await message.answer(t.consent_intro, reply_markup=consent_kb())
        return
```

- [ ] **Step 10: Тесты**

Run: `python -m pytest tests/test_consent.py -v`
Expected: 4 PASS

Run: `python -m pytest -q`
Expected: всё зелёное

- [ ] **Step 11: Коммит**

```bash
git -C /root/myvpn-bot add bot/db/ bot/texts/ru.py bot/keyboards/inline/ bot/handlers/ tests/test_consent.py
git -C /root/myvpn-bot commit -m "Экран согласия с условиями при первом входе"
```

---

### Task 8: Цвета кнопок, раскладка и статус подписки

**Files:**
- Modify: `bot/keyboards/inline/menu.py`
- Modify: `bot/keyboards/inline/devices.py`, `balance.py`
- Modify: `bot/handlers/common.py` (`_send_main_menu`)
- Modify: `bot/texts/ru.py`
- Test: `tests/test_menu_layout.py` (создать)

**Interfaces:**
- Consumes: `settings.legal_*` из задачи 6
- Produces: `main_menu(is_admin: bool) -> InlineKeyboardMarkup` с новой
  раскладкой; `bot.handlers.common.build_sub_status_line(user) -> str`

- [ ] **Step 1: Написать падающие тесты**

```python
"""Раскладка главного меню и строка статуса подписки."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def test_main_menu_pairs_secondary_buttons(monkeypatch) -> None:
    """Главные действия — во всю ширину, второстепенные — парами: иначе меню
    выглядит как восемь одинаковых кнопок без иерархии."""
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")

    rows = main_menu(is_admin=False).inline_keyboard
    assert len(rows[0]) == 1
    assert len(rows[1]) == 1
    assert any(len(row) == 2 for row in rows)


def test_main_menu_marks_primary_action(monkeypatch) -> None:
    from bot.config import settings
    from bot.keyboards.inline import main_menu

    monkeypatch.setattr(settings, "legal_privacy_url", "")
    monkeypatch.setattr(settings, "legal_terms_url", "")

    styles = [b.style for row in main_menu(is_admin=False).inline_keyboard for b in row]
    assert "primary" in styles


def test_sub_status_line_active() -> None:
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    user = User(
        tg_id=1, sub_expires_at=datetime.now(timezone.utc) + timedelta(days=5)
    )
    line = build_sub_status_line(user)
    assert "5" in line


def test_sub_status_line_expired() -> None:
    from bot.db.models import User
    from bot.handlers.common import build_sub_status_line

    user = User(
        tg_id=1, sub_expires_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert "не активна" in build_sub_status_line(user).lower()
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m pytest tests/test_menu_layout.py -v`
Expected: FAIL

- [ ] **Step 3: Новая раскладка меню**

`bot/keyboards/inline/menu.py`, функция `main_menu`:

```python
def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    """Главные действия — во всю ширину и цветом, справочные разделы — парами.
    Цвет кнопок (style) поддерживается с Bot API 9.4; старые клиенты просто
    покажут обычные кнопки."""
    from bot.config import settings

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Мои устройства", callback_data=f"{CB_DEVICE}:list",
              style="primary")
    kb.button(text="⚡ Резервное подключение", callback_data=f"{CB_WDTT}:my")
    kb.button(text="🎫 Моя подписка", callback_data=f"{CB_SUB}:my")
    kb.button(text="💰 Баланс", callback_data=f"{CB_BAL}:my")
    kb.button(text="💳 Тарифы", callback_data=f"{CB_LEGAL}:tariffs")
    kb.button(text="🌍 Локации", callback_data=f"{CB_MENU}:locations")
    kb.button(text="🔔 Оповещения", callback_data=f"{CB_MENU}:notify")
    kb.button(text="🆘 Поддержка", callback_data=f"{CB_MENU}:help")

    sizes = [1, 1, 2, 2, 2]

    if settings.legal_privacy_url:
        kb.button(text="📄 Политика конфиденциальности", url=settings.legal_privacy_url)
        sizes.append(1)
    if settings.legal_terms_url:
        kb.button(text="📜 Пользовательское соглашение", url=settings.legal_terms_url)
        sizes.append(1)
    if is_admin:
        kb.button(text="👮 Админ-панель", callback_data=f"{CB_PANEL}:main")
        sizes.append(1)

    kb.adjust(*sizes)
    return kb.as_markup()
```

Импорт `CB_LEGAL` добавить в шапку модуля.

- [ ] **Step 4: Цвет на разрушительных и денежных действиях**

В `bot/keyboards/inline/devices.py` — кнопкам удаления устройства и отзыва
доступа добавить `style="danger"`. В `bot/keyboards/inline/balance.py` —
кнопкам пополнения и продления `style="success"`. Навигационные кнопки
(«В меню», «Назад») цвета не получают.

- [ ] **Step 5: Строка статуса подписки**

`bot/handlers/common.py`:

```python
def build_sub_status_line(user) -> str:
    """Две строки о подписке в главном меню: раньше за сроком нужно было идти
    в отдельный раздел."""
    from datetime import datetime, timezone

    if user.sub_expires_at is None:
        return "Подписка: <b>активна</b> ✅ (бессрочно)"

    expires = user.sub_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    left = expires - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "Подписка: <b>не активна</b> ❌"
    return f"Подписка: <b>активна</b> ✅ · осталось дней: <b>{left.days}</b>"
```

В `_send_main_menu` эту строку дописывать к тексту меню для не-админов.

- [ ] **Step 6: Тесты**

Run: `python -m pytest tests/test_menu_layout.py -v`
Expected: 4 PASS

Run: `python -m pytest -q`
Expected: всё зелёное

- [ ] **Step 7: Коммит**

```bash
git -C /root/myvpn-bot add bot/keyboards/inline/ bot/handlers/common.py bot/texts/ru.py tests/test_menu_layout.py
git -C /root/myvpn-bot commit -m "Цветные кнопки, раскладка меню и статус подписки на главном экране"
```

---

### Task 9: Выкат и проверка на живом боте

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Обновить README**

Дописать раздел про юридические экраны: новые переменные `.env`
(`LEGAL_PRIVACY_URL`, `LEGAL_TERMS_URL`, `TELEGRAPH_TOKEN`,
`PREMIUM_EMOJI_ENABLED`), экран согласия и константу `CONSENT_SINCE`, экран
тарифов, страж формулировок и то, зачем он нужен. В описании раздела wdtt
заменить пользовательское название на «Резервное подключение», оставив
техническую суть.

- [ ] **Step 2: Прогнать весь набор тестов**

Run: `python -m pytest -q`
Expected: всё зелёное, счётчик тестов вырос минимум на 12

- [ ] **Step 3: Влить ветку и выкатить**

```bash
git -C /root/myvpn-bot push origin etap-c-smena-servera
ssh klopas 'git -C /root/myvpn-bot pull && systemctl restart myvpn-bot && sleep 5 && systemctl is-active myvpn-bot'
```

Перед рестартом положить в прод-`.env` `LEGAL_PRIVACY_URL`, `LEGAL_TERMS_URL`
и `TELEGRAPH_TOKEN` из задачи 5.

- [ ] **Step 4: Проверить логи**

```bash
ssh klopas 'journalctl -u myvpn-bot -n 50 --no-pager | tail -30'
```
Expected: миграция добавила `terms_accepted_at`, ошибок нет

- [ ] **Step 5: Живая проверка**

Пройти в боте: `/start` (меню, цвета, статус подписки), «💳 Тарифы» (цифры
совпадают с `.env`), обе кнопки документов открываются, раздел «⚡ Резервное
подключение» открывается и нигде не говорит «обход».

Экран согласия проверяется только новым аккаунтом — у действующих
`created_at` раньше `CONSENT_SINCE`. Если нового аккаунта нет, временно
обнулить `terms_accepted_at` и сдвинуть `CONSENT_SINCE` для проверки, потом
вернуть.

- [ ] **Step 6: Коммит**

```bash
git -C /root/myvpn-bot add README.md
git -C /root/myvpn-bot commit -m "README: юридические экраны, страж формулировок, тарифы"
```

---

## Что осталось за планом

- **Премиум-эмодзи.** Флаг `PREMIUM_EMOJI_ENABLED` добавляется в задаче 6, но
  сами эмодзи не расставляются: у владельца бота нет Telegram Premium
  (проверено 05.08.2026 — 0 из 50 из списка и 0 из 3 эталонных). Когда Premium
  появится — прогнать тот же тест по списку ID и расставить теги
  `<tg-emoji emoji-id="…">обычный_символ</tg-emoji>` в текстах, обязательно
  оставляя обычный символ внутри тега как запасной.
- **Описание бота в BotFather** — Влад проверяет сам, из кода оно не правится.
- **Почта поддержки** — заводится, только если банк попросит второй канал связи.
