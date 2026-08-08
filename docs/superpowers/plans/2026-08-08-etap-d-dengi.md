# Этап D (деньги) — план реализации

> **Для агентов:** реализовывать по одной задаче через `superpowers:subagent-driven-development` или `superpowers:executing-plans`. Шаги отмечены чекбоксами.

**Цель:** новый прайс, бонусы за способ пополнения, оплата звёздами Telegram и честная статистика — всё, что можно сделать до прихода доступа к Platega.

**Архитектура:** все цены, скидки, бонусы и курс звезды сводятся в `bot/services/pricing.py` и настройки; экраны и списания только вызывают его функции. Деньги по-прежнему ходят одним путём — через баланс: и звёзды, и будущая Platega лишь пополняют его, а подписка покупается с баланса.

**Стек:** Python 3.13, aiogram 3.26, SQLAlchemy 2 async + aiosqlite, pytest (`asyncio_mode=auto`), loguru.

**Спека:** `docs/superpowers/specs/2026-08-08-etap-d-dengi-design.md`.

## Границы плана

Platega в этот план НЕ входит: доступа к её API ещё нет (проект на согласовании в банке, ответ обещан через 2–4 дня от 8.08). Под неё будет отдельный план, когда придут ключи. До тех пор экран пополнения живёт с двумя способами — CryptoBot и звёзды.

## Global Constraints

- **Все суммы — в копейках**, целыми числами. Никаких `float` у денег.
- **Движение денег только через `repo.add_balance_tx`** — иначе баланс разъедется с журналом `balance_txs`.
- **Тексты для юзера проходят страж формулировок** (`tests/test_wording.py`): запрещены «обход», «блокиров», «белые списки», DPI, ТСПУ, LTE, ИНН. Раздел называется «⚡ Резервное подключение». Для внутренних строк (аудит-лог, админка) — маркер `# wording: ok` на последней строке литерала.
- **Время**: в базе UTC, на экраны — МСК через `bot/utils/timefmt.py`.
- **`PRAGMA foreign_keys` не включать** — на выключенных внешних ключах держится стирание юзера.
- **Репозиторные функции делают `flush()`, но не `commit()`** — коммит на вызывающем; строка журнала пишется той же транзакцией, что и действие.
- **Новые колонки — только nullable или с `server_default`**: автомиграция делает `ALTER TABLE ADD COLUMN`, а `NOT NULL` без значения по умолчанию на непустой таблице невозможен.
- **Бота нельзя отключать надолго и нельзя менять его @username** — проект на согласовании в банке. Рестарт при деплое это не ломает.
- **Приёмка:** `tests/test_qrgen.py` падает в этом окружении из-за отсутствия PIL — критерий «зелёное» = нет НОВЫХ падений. Полный набор гоняется командой `python -m pytest` из корня репозитория.

---

## Структура файлов

| Файл | Ответственность | Задача |
|---|---|---|
| `bot/config.py` | три числа прайса + курс звезды | 1, 5 |
| `bot/services/pricing.py` | формула цены, скидки, бонусы, звёзды | 1, 3, 5 |
| `bot/handlers/balance.py` | конструктор тарифа, пополнение, история | 2, 3, 5 |
| `bot/handlers/legal.py` | экран тарифов | 2 |
| `bot/handlers/common.py`, `bot/handlers/devices.py`, `bot/services/scheduler.py` | упоминания цены в текстах | 2 |
| `bot/db/models.py` | флаг служебного аккаунта, таблица звёздных платежей | 4, 5 |
| `bot/handlers/admin/users.py`, `bot/keyboards/inline/admin.py` | переключатель «служебный» | 4 |
| `bot/handlers/admin/stats.py` | статистика без своих | 4 |
| `bot/handlers/stars.py` (создать) | приём оплаты звёздами | 5 |
| `bot/keyboards/inline/balance.py` | кнопки способов оплаты | 5 |

---

### Задача 1: Новая формула цены

**Файлы:**
- Изменить: `bot/config.py:61-64`
- Изменить: `bot/services/pricing.py:1-43`
- Изменить: `.env.example` (строки с `PRICE_*`)
- Тест: `tests/test_billing.py:33-60`

**Интерфейсы:**
- Отдаёт: `monthly_price_kopeks(max_devices: int, max_bypass: int) -> int` — сигнатура не меняется, меняется только формула. `settings.price_first_rub` (было `price_base_rub`), `settings.price_extra_device_rub`, `settings.price_extra_bypass_rub`.

- [ ] **Шаг 1: Переписать тесты цен под новые числа**

В `tests/test_billing.py` заменить содержимое класса с ценами:

```python
class TestPricing:
    def test_first_position_costs_the_minimum(self) -> None:
        """Любая одна позиция стоит 90 ₽ — это пол тарифа."""
        assert monthly_price_kopeks(1, 0) == 90_00
        assert monthly_price_kopeks(0, 1) == 90_00

    def test_next_positions_add_up(self) -> None:
        assert monthly_price_kopeks(1, 1) == 120_00   # +30 за подключение
        assert monthly_price_kopeks(2, 1) == 160_00   # +40 за устройство
        assert monthly_price_kopeks(3, 1) == 200_00
        assert monthly_price_kopeks(2, 0) == 130_00
        assert monthly_price_kopeks(0, 2) == 120_00
        assert monthly_price_kopeks(1, 3) == 180_00

    def test_nothing_costs_less_than_the_floor(self) -> None:
        """Формула только складывает — уйти ниже 90 ₽ неоткуда.

        Прежняя вычитала из базы неиспользуемые позиции и при неудачной
        правке цен могла уйти в минус; тест стережёт, что это не вернётся.
        """
        for devices in range(0, 11):
            for bypass in range(0, 11):
                if devices + bypass == 0:
                    continue
                assert monthly_price_kopeks(devices, bypass) >= 90_00

    def test_empty_tariff_is_not_sold(self) -> None:
        with pytest.raises(ValueError):
            monthly_price_kopeks(0, 0)

    def test_term_discounts(self) -> None:
        m = monthly_price_kopeks(1, 1)                # 120 ₽
        assert term_price_kopeks(m, 1) == 120_00
        assert term_price_kopeks(m, 3) == 320_00     # 360 −10% = 324 → вниз до 320
        assert term_price_kopeks(m, 6) == 610_00     # 720 −15% = 612 → 610
        assert term_price_kopeks(m, 12) == 1080_00   # 1440 −25% = 1080

    def test_term_discounts_on_the_floor_tariff(self) -> None:
        m = monthly_price_kopeks(1, 0)               # 90 ₽
        assert term_price_kopeks(m, 3) == 240_00
        assert term_price_kopeks(m, 6) == 450_00
        assert term_price_kopeks(m, 12) == 810_00
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Запустить: `python -m pytest tests/test_billing.py -k Pricing -v`
Ожидается: FAIL — старая формула даёт 90 ₽ за `(1, 1)` и 60 ₽ за `(1, 0)`.

- [ ] **Шаг 3: Переименовать и поменять настройку цены**

В `bot/config.py` заменить блок цен:

```python
    # Цены, ₽/мес. Первая позиция (устройство ИЛИ резервное подключение) стоит
    # price_first_rub — это пол тарифа, дешевле не бывает. Каждая следующая
    # позиция прибавляется. Формула только складывает: уйти в минус, как это
    # могло случиться у прежней (она вычитала неиспользуемое из базы), неоткуда.
    price_first_rub: int = 90
    price_extra_device_rub: int = 40
    price_extra_bypass_rub: int = 30
```

В `.env.example` заменить строку `PRICE_BASE_RUB=90` на `PRICE_FIRST_RUB=90` и поправить `PRICE_EXTRA_DEVICE_RUB` на `40`, сохранив стиль соседних комментариев.

- [ ] **Шаг 4: Переписать формулу**

В `bot/services/pricing.py` заменить докстринг модуля и `monthly_price_kopeks`:

```python
"""Цены подписки (Блок «Баланс»). Все суммы — в КОПЕЙКАХ (никаких float у денег).

Модель: первая позиция (устройство ИЛИ резервное подключение) стоит
`price_first_rub` — это пол тарифа. Каждая следующая позиция прибавляется по
своей цене. Чем длиннее срок — тем больше скидка. Рубли из конфига, скидки,
округление, бонусы за способ пополнения и курс звезды — здесь.
"""
```

```python
def monthly_price_kopeks(max_devices: int, max_bypass: int) -> int:
    """₽/мес тарифа. Первая позиция — `price_first_rub`, каждая следующая
    прибавляется: устройство +`price_extra_device_rub`, подключение
    +`price_extra_bypass_rub`.

    Первой считается устройство, если оно есть; если устройств ноль — первое
    резервное подключение. Тариф без единой позиции (0+0) не существует — за
    ним стоит ошибка вызывающего.

    Формула только складывает, поэтому дешевле пола тариф быть не может. У
    прежней формулы база покрывала позиции, а отказ от них вычитался, и при
    base < сумма доплат тариф уходил в минус — за этим приходилось следить
    отдельным правилом. Правило исчезло вместе с вычитанием.
    """
    if max_devices + max_bypass < 1:
        raise ValueError("тариф без устройств и обходов не продаётся")  # wording: ok
    if max_devices >= 1:
        rub = (
            settings.price_first_rub
            + (max_devices - 1) * settings.price_extra_device_rub
            + max_bypass * settings.price_extra_bypass_rub
        )
    else:
        rub = (
            settings.price_first_rub
            + (max_bypass - 1) * settings.price_extra_bypass_rub
        )
    return rub * 100
```

- [ ] **Шаг 5: Прогнать тесты цен**

Запустить: `python -m pytest tests/test_billing.py -k Pricing -v`
Ожидается: PASS.

- [ ] **Шаг 6: Найти оставшиеся упоминания старой настройки**

Запустить: `grep -rn "price_base_rub\|PRICE_BASE_RUB" bot/ tests/ docs/ README.md .env.example`
Ожидается: совпадения только в `bot/handlers/balance.py` и `bot/handlers/legal.py` и `README.md` — их чинит задача 2. Если совпадение найдено где-то ещё, починить здесь же, иначе бот не поднимется.

- [ ] **Шаг 7: Прогнать полный набор**

Запустить: `python -m pytest`
Ожидается: падают только 2 теста `tests/test_qrgen.py` плюс тесты экранов (`test_legal.py`), которые чинит задача 2. Записать точные имена падений — задача 2 обязана их закрыть.

- [ ] **Шаг 8: Коммит**

```bash
git add bot/config.py bot/services/pricing.py .env.example tests/test_billing.py
git commit -m "Прайс: первая позиция 90 ₽, следующие прибавляются"
```

---

### Задача 2: Цена во всех текстах и старт конструктора

**Файлы:**
- Изменить: `bot/handlers/balance.py:455-482` (конструктор), `bot/handlers/balance.py:53-60` (суммы пополнения)
- Изменить: `bot/handlers/legal.py:28-46` (экран тарифов)
- Изменить: `bot/handlers/common.py:149-158`, `bot/handlers/devices.py:447-470`, `bot/services/scheduler.py:247-254`
- Изменить: `README.md` (таблица переменных окружения, строки про цены)
- Тест: `tests/test_legal.py`, `tests/test_menu_layout.py`

**Интерфейсы:**
- Потребляет из задачи 1: `settings.price_first_rub`, `settings.price_extra_device_rub`, `settings.price_extra_bypass_rub`, `monthly_price_kopeks`.

- [ ] **Шаг 1: Написать падающий тест на старт конструктора и текст**

Создать `tests/test_pricing_screens.py`:

```python
"""Цена, названная словами, обязана совпадать с ценой, которая спишется."""
from __future__ import annotations

import pytest

from bot.config import settings
from bot.services.pricing import monthly_price_kopeks


def test_constructor_starts_at_one_device() -> None:
    """Старт конструктора — типовой тариф 1+1 (120 ₽), а не 2+1 (160 ₽).

    2+1 выше рыночной медианы ~150 ₽, и первым числом, которое видит юзер,
    ему быть не стоит.
    """
    from bot.handlers.balance import _START_DEVICES, _START_BYPASS

    assert (_START_DEVICES, _START_BYPASS) == (1, 1)
    assert monthly_price_kopeks(_START_DEVICES, _START_BYPASS) == 120_00


def test_extend_text_names_both_extra_prices() -> None:
    """Доплаты за устройство и за подключение теперь РАЗНЫЕ (40 и 30 ₽).

    Прежний текст называл одну цифру на обе позиции — с новым прайсом это
    прямая ложь про деньги.
    """
    from bot.handlers.balance import _extend_intro

    text = _extend_intro()
    assert f"{settings.price_first_rub} ₽" in text
    assert f"{settings.price_extra_device_rub} ₽" in text
    assert f"{settings.price_extra_bypass_rub} ₽" in text
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Запустить: `python -m pytest tests/test_pricing_screens.py -v`
Ожидается: FAIL с `ImportError` — `_START_DEVICES` и `_extend_intro` ещё не существуют.

- [ ] **Шаг 3: Вынести вводный текст конструктора и стартовое положение**

В `bot/handlers/balance.py` рядом с `_MAX_DEVICES`/`_MAX_BYPASS` добавить:

```python
# Стартовое положение конструктора — типовой тариф, а не витринный: 2+1 стоит
# 160 ₽, выше рыночной медианы ~150 ₽, и первым числом юзеру его показывать
# не стоит.
_START_DEVICES = 1
_START_BYPASS = 1


def _extend_intro() -> str:
    """Как считается цена — словами. Отдельной функцией, чтобы тест мог
    сверить названные цифры с настройками, а не с копией текста."""
    return (
        f"Считаем просто: первая позиция (устройство или резервное "
        f"подключение) — <b>{settings.price_first_rub} ₽/мес</b>. "
        f"Каждое следующее устройство — <b>+{settings.price_extra_device_rub} ₽/мес</b>, "
        f"каждое следующее подключение — <b>+{settings.price_extra_bypass_rub} ₽/мес</b>. "
        "Что-то из этого не нужно — смело ставь 0."
    )
```

- [ ] **Шаг 4: Подставить их в экран продления**

В `bot/handlers/balance.py` в `_render_extend` удалить вычисление `first_rub` и заменить первый абзац текста на `_extend_intro()`:

```python
async def _render_extend(edit, user, devices: int, bypass: int) -> None:
    devices, bypass = _clamp_tariff(user, devices, bypass)
    max_dev, max_byp = _tariff_bounds(user)
    monthly = monthly_price_kopeks(devices, bypass)
    text = (
        "🔁 <b>Продление подписки</b>\n\n"
        f"{_extend_intro()}\n\n"
        "Твой тариф:\n"
        f"📱 Устройств: <b>{devices}</b>\n"
        f"⚡ Резервных подключений: <b>{bypass}</b>\n"
        f"Цена: <b>{fmt_rub(monthly)}/мес</b>\n"
        f"💰 На балансе: <b>{fmt_rub(user.balance_kopeks)}</b>\n\n"
        "Настрой количество кнопками − и +, потом выбери срок — чем дольше, "
        "тем дешевле. Оплаченные дни прибавятся к текущей подписке, новый "
        "тариф заработает сразу."
    )
    await edit(text, reply_markup=extend_kb(
        devices, bypass, _term_price_rows(devices, bypass), max_dev, max_byp
    ))
```

В `cb_bal_extend` заменить стартовые значения для юзера, у которого лимиты ещё нулевые:

```python
    start_dev = user.sub_max_devices or _START_DEVICES
    start_byp = user.sub_max_bypass or _START_BYPASS
    await _render_extend(call.message.edit_text, user, start_dev, start_byp)
```

- [ ] **Шаг 5: Починить экран тарифов**

В `bot/handlers/legal.py` в `build_tariffs_text` заменить вычисление базы и доплат:

```python
    base = monthly_price_kopeks(1, 1)      # типовой тариф: устройство + подключение
    first = monthly_price_kopeks(1, 0)     # пол тарифа: одна позиция
```

и подстановку доплаты — на две отдельные:

```python
        extra_device=fmt_rub(settings.price_extra_device_rub * 100),
        extra_bypass=fmt_rub(settings.price_extra_bypass_rub * 100),
```

Соответственно поправить шаблон текста тарифов в `bot/texts/ru.py`: там, где называлась одна доплата, назвать обе — за устройство и за подключение.

- [ ] **Шаг 6: Прогнать тесты экранов**

Запустить: `python -m pytest tests/test_pricing_screens.py tests/test_legal.py tests/test_menu_layout.py -v`
Ожидается: PASS. Если `tests/test_legal.py` ждёт старую формулировку доплаты — поправить ожидание, а не текст.

- [ ] **Шаг 7: Проверить оставшиеся упоминания цены**

Запустить: `grep -rn "monthly_price_kopeks(1, 1)\|price_extra_device_rub\|price_first_rub" bot/`
Пройти по каждому совпадению в `common.py`, `devices.py`, `scheduler.py` и убедиться, что текст рядом не обещает старую цену словами («от 90 ₽», «+30 ₽»). Где обещает — подставить значение из настроек, а не цифру в тексте.

- [ ] **Шаг 8: README**

В таблице переменных окружения заменить `PRICE_BASE_RUB` на `PRICE_FIRST_RUB` с описанием «Цена первой позиции тарифа (устройство или подключение) в месяц», `PRICE_EXTRA_DEVICE_RUB` — значение по умолчанию 40. В разделе про цены заменить абзац «первая позиция 60 ₽, дальше +30 ₽» на новую формулу с примерами 1+1 = 120 ₽, 2+1 = 160 ₽, 1+0 = 90 ₽.

- [ ] **Шаг 9: Полный набор и коммит**

Запустить: `python -m pytest`
Ожидается: падают только 2 теста `tests/test_qrgen.py`.

```bash
git add bot/handlers/balance.py bot/handlers/legal.py bot/texts/ru.py README.md tests/test_pricing_screens.py tests/test_legal.py
git commit -m "Экраны называют новую цену, конструктор стартует с типового тарифа"
```

---

### Задача 3: Бонус за способ пополнения

**Файлы:**
- Изменить: `bot/services/pricing.py` (добавить таблицу бонусов)
- Изменить: `bot/services/billing.py:37-70` (`apply_paid_invoice`)
- Изменить: `bot/handlers/balance.py:107-124` (экран пополнения), `bot/handlers/balance.py:371` (`_KIND_ICONS`)
- Тест: `tests/test_billing.py`

**Интерфейсы:**
- Отдаёт: `pricing.DEPOSIT_BONUS_PERCENT: dict[str, int]`, `pricing.deposit_bonus_kopeks(amount_kopeks: int, method: str) -> int`. Ключи способов: `"cryptobot"`, `"stars"`, `"platega"`.
- Новый вид движения денег `kind="bonus"` в `balance_txs`.

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_billing.py`:

```python
class TestDepositBonus:
    def test_cryptobot_gives_four_percent(self) -> None:
        from bot.services.pricing import deposit_bonus_kopeks

        assert deposit_bonus_kopeks(100_00, "cryptobot") == 4_00
        assert deposit_bonus_kopeks(1000_00, "cryptobot") == 40_00

    def test_expensive_methods_give_nothing(self) -> None:
        """Карта и СБП обходятся сервису в 9 и 8 % — доплачивать юзеру за
        самый невыгодный способ нельзя."""
        from bot.services.pricing import deposit_bonus_kopeks

        assert deposit_bonus_kopeks(100_00, "platega") == 0
        assert deposit_bonus_kopeks(100_00, "stars") == 0

    def test_unknown_method_gives_nothing(self) -> None:
        from bot.services.pricing import deposit_bonus_kopeks

        assert deposit_bonus_kopeks(100_00, "нет такого") == 0

    async def test_bonus_lands_as_its_own_row(
        self, session: AsyncSession
    ) -> None:
        """Бонус — отдельная строка, а не надбавка внутри пополнения.

        Иначе статистика «пополнений за 30 дней» показывала бы сумму, которой
        сервис никогда не получал.
        """
        from bot.db.models import CryptoInvoice
        from bot.services import billing

        user = await repo.get_or_create_user(
            session, tg_id=4101, username="u", full_name="U"
        )
        inv = await repo.create_crypto_invoice(
            session, user_id=user.id, invoice_id="inv-1",
            amount_kopeks=100_00, url="https://x",
        )
        await session.commit()

        res = await billing.apply_paid_invoice(session, inv)
        await session.commit()

        assert res.credited
        assert user.balance_kopeks == 104_00
        rows = await repo.list_balance_txs(session, user.id, limit=10)
        kinds = {r.kind: r.amount_kopeks for r in rows}
        assert kinds["deposit"] == 100_00
        assert kinds["bonus"] == 4_00
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Запустить: `python -m pytest tests/test_billing.py -k DepositBonus -v`
Ожидается: FAIL — `deposit_bonus_kopeks` не существует.

- [ ] **Шаг 3: Добавить таблицу бонусов**

В `bot/services/pricing.py`:

```python
# Бонус за способ пополнения, % к зачисляемой сумме. Смысл — вести юзера к
# тому способу, который дешевле обходится сервису: карта 9 % и СБП 8 % самые
# дорогие, доплачивать за них нельзя. Звёзды идут со своей наценкой 25 %,
# бонус поверх неё был бы взаимоисключающим.
DEPOSIT_BONUS_PERCENT: dict[str, int] = {
    "cryptobot": 4,
    "platega": 0,
    "stars": 0,
}


def deposit_bonus_kopeks(amount_kopeks: int, method: str) -> int:
    """Надбавка к зачислению за способ пополнения. Неизвестный способ — 0:
    новый провайдер не должен начать раздавать бонусы по умолчанию."""
    return amount_kopeks * DEPOSIT_BONUS_PERCENT.get(method, 0) // 100
```

- [ ] **Шаг 4: Начислять бонус отдельной строкой**

В `bot/services/billing.py` в `apply_paid_invoice` сразу после начисления пополнения добавить:

```python
    # Бонус — ОТДЕЛЬНАЯ строка, а не надбавка внутри пополнения: в статистике
    # «пополнений за 30 дней» должны стоять деньги, которые сервис правда
    # получил, а не они же плюс подарок.
    bonus = deposit_bonus_kopeks(inv.amount_kopeks, "cryptobot")
    if bonus:
        await repo.add_balance_tx(
            session, inv.user_id, bonus, "bonus",
            note=f"Бонус {DEPOSIT_BONUS_PERCENT['cryptobot']}% за пополнение через CryptoBot",
        )
```

и добавить `deposit_bonus_kopeks`, `DEPOSIT_BONUS_PERCENT` в импорт из `bot.services.pricing` в шапке файла.

- [ ] **Шаг 5: Показать бонус юзеру**

В `bot/handlers/balance.py` в `cb_bal_deposit` дописать в текст экрана строку про бонус, взяв процент из таблицы, а не цифрой:

```python
        f"\n🎁 За пополнение через @CryptoBot начислим "
        f"<b>+{DEPOSIT_BONUS_PERCENT['cryptobot']}%</b> сверху.\n"
```

В `_KIND_ICONS` добавить `"bonus": "✨"`, чтобы строка бонуса в истории операций не осталась без значка.

- [ ] **Шаг 6: Прогнать тесты**

Запустить: `python -m pytest tests/test_billing.py -v`
Ожидается: PASS.

- [ ] **Шаг 7: Полный набор и коммит**

Запустить: `python -m pytest`
Ожидается: падают только 2 теста `tests/test_qrgen.py`.

```bash
git add bot/services/pricing.py bot/services/billing.py bot/handlers/balance.py tests/test_billing.py
git commit -m "Бонус за способ пополнения отдельной строкой в журнале"
```

---

### Задача 4: Статистика не считает своих

**Файлы:**
- Изменить: `bot/db/models.py:114-117` (рядом с `is_vip`)
- Изменить: `bot/keyboards/inline/admin.py:179-195`, `bot/handlers/admin/users.py:54-80`
- Изменить: `bot/handlers/admin/stats.py:68-105`
- Тест: `tests/test_stats.py` (создать)

**Интерфейсы:**
- Отдаёт: `User.is_staff: bool` (default False, `server_default="0"`, not null), переключатель в карточке юзера, исключение админов и служебных из конверсии и денег.

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_stats.py`:

```python
"""Статистика показывает чужих людей, а не своих.

Повод: 8.08 конверсия показывала «1 из 7 покупали подписку», и этот один был
сам владелец с тестовой покупкой на 27 270 ₽, оплаченной деньгами, которые он
сам себе начислил кнопкой.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo


async def _user(session: AsyncSession, *, tg_id: int, admin=False, staff=False):
    user = await repo.get_or_create_user(
        session, tg_id=tg_id, username="u", full_name="U"
    )
    user.is_admin = admin
    user.is_staff = staff
    await session.flush()
    return user


class TestStatsExcludesOwnPeople:
    async def test_admin_purchase_is_not_conversion(
        self, session: AsyncSession
    ) -> None:
        from bot.handlers.admin.stats import collect_money_stats

        admin = await _user(session, tg_id=4201, admin=True)
        await repo.add_balance_tx(session, admin.id, -2727000, "charge", note="тест")
        await _user(session, tg_id=4202)
        await session.commit()

        st = await collect_money_stats(session)

        assert st.users_counted == 1, "админ попал в знаменатель конверсии"
        assert st.users_paid == 0, "тестовая покупка админа засчитана как продажа"
        assert st.charged_30d == 0

    async def test_staff_flag_excludes_too(self, session: AsyncSession) -> None:
        """Друзья платят вне бота, проверяющий из платёжки не купит никогда —
        обоим не место в знаменателе."""
        from bot.handlers.admin.stats import collect_money_stats

        await _user(session, tg_id=4203, staff=True)
        await _user(session, tg_id=4204)
        await session.commit()

        st = await collect_money_stats(session)

        assert st.users_counted == 1
        assert st.staff_counted == 1

    async def test_real_purchase_is_counted(self, session: AsyncSession) -> None:
        from bot.handlers.admin.stats import collect_money_stats

        buyer = await _user(session, tg_id=4205)
        await repo.add_balance_tx(session, buyer.id, -120_00, "charge", note="покупка")
        await session.commit()

        st = await collect_money_stats(session)

        assert st.users_paid == 1
        assert st.charged_30d == 120_00
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Запустить: `python -m pytest tests/test_stats.py -v`
Ожидается: FAIL — нет ни `User.is_staff`, ни `collect_money_stats`.

- [ ] **Шаг 3: Добавить флаг в модель**

В `bot/db/models.py` сразу после `is_vip`:

```python
    # «Служебный»: свои люди и проверяющие — из статистики исключаются, чтобы
    # тесты владельца и бесплатные друзья не выдавали себя за продажи. Отдельно
    # от is_vip намеренно: «друг» — это доступ к приватным серверам, и друг
    # однажды может ещё и платить; смешаешь — спрячешь настоящие деньги.
    is_staff: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
```

Колонка с `server_default` — автомиграция добавит её на живой базе.

- [ ] **Шаг 4: Вынести подсчёт в отдельную функцию**

В `bot/handlers/admin/stats.py` добавить перед хендлером:

```python
@dataclass
class MoneyStats:
    users_counted: int      # чужие люди, знаменатель конверсии
    staff_counted: int      # свои: админы + служебные
    users_paid: int         # из чужих — сколько хоть раз платили
    deposited_30d: int
    charged_30d: int


async def collect_money_stats(session: AsyncSession) -> MoneyStats:
    """Деньги и конверсия по ЧУЖИМ людям.

    Отдельной функцией, а не строчками внутри хендлера: экран статистики в
    тесте не поднять, а «кого считаем» — ровно то, что надо проверять.
    """
    own = select(User.id).where(or_(User.is_admin.is_(True), User.is_staff.is_(True)))
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)

    async def _one(stmt) -> int:
        return (await session.execute(stmt)).scalar_one()

    staff_counted = await _one(select(func.count(User.id)).where(User.id.in_(own)))
    users_counted = await _one(select(func.count(User.id)).where(User.id.not_in(own)))
    users_paid = await _one(
        select(func.count(func.distinct(BalanceTx.user_id)))
        .where(BalanceTx.kind == "charge")
        .where(BalanceTx.user_id.not_in(own))
    )
    deposited = await _one(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.kind == "deposit")
        .where(BalanceTx.created_at >= month_ago)
        .where(BalanceTx.user_id.not_in(own))
    )
    charged = await _one(
        select(func.coalesce(func.sum(BalanceTx.amount_kopeks), 0))
        .where(BalanceTx.kind == "charge")
        .where(BalanceTx.created_at >= month_ago)
        .where(BalanceTx.user_id.not_in(own))
    )
    return MoneyStats(
        users_counted=users_counted,
        staff_counted=staff_counted,
        users_paid=users_paid,
        deposited_30d=deposited,
        charged_30d=-charged,
    )
```

Добавить импорты: `from dataclasses import dataclass`, `from sqlalchemy import or_`, `from bot.db.models import User`.

- [ ] **Шаг 5: Подставить в экран**

В хендлере статистики удалить прежний подсчёт `users_paid_ever`, `dep_30d`, `charge_30d` и взять их из `collect_money_stats`. Строки текста:

```python
        f"👤 Юзеров: <b>{users_total}</b>"
        + (f" (служебных {st.staff_counted})" if st.staff_counted else "")
        + " — "
        ...
        f"📈 Конверсия: <b>{st.users_paid}</b> из {st.users_counted} покупали "
        f"подписку ({conv_pct}%)\n"
        f"💰 За 30 дней: пополнений <b>{fmt_rub(st.deposited_30d)}</b>, "
        f"оплат подписки <b>{fmt_rub(st.charged_30d)}</b>\n\n"
```

где `conv_pct = round(st.users_paid * 100 / st.users_counted) if st.users_counted else 0`.

- [ ] **Шаг 6: Переключатель в карточке юзера**

В `bot/keyboards/inline/admin.py` в `user_card_kb` добавить параметр `is_staff: bool = False` и кнопку рядом с «⭐ Друг»:

```python
    # Служебный аккаунт: свои и проверяющие не считаются в статистике.
    kb.button(
        text="🧰 Служебный: ВКЛ" if is_staff else "🧰 Служебный: выкл",
        callback_data=f"{CB_PANEL}:staff:{user_id}:{page}",
    )
```

В `bot/handlers/admin/users.py` добавить хендлер по образцу переключателя «Друг» (`cb_panel_vip`), который переключает `user.is_staff`, коммитит и перерисовывает карточку. Во все вызовы `user_card_kb` добавить `is_staff=user.is_staff`.

- [ ] **Шаг 7: Прогнать тесты**

Запустить: `python -m pytest tests/test_stats.py tests/test_admin_nav.py -v`
Ожидается: PASS.

- [ ] **Шаг 8: Полный набор и коммит**

```bash
git add bot/db/models.py bot/handlers/admin/stats.py bot/handlers/admin/users.py bot/keyboards/inline/admin.py tests/test_stats.py
git commit -m "Статистика считает чужих людей, а не своих"
```

---

### Задача 5: Оплата звёздами Telegram

**Файлы:**
- Создать: `bot/handlers/stars.py`
- Изменить: `bot/services/pricing.py` (курс и наценка), `bot/config.py` (курс звезды)
- Изменить: `bot/db/models.py` (таблица звёздных платежей)
- Изменить: `bot/keyboards/inline/balance.py`, `bot/handlers/balance.py:107-124` (экран выбора способа)
- Изменить: `bot/__main__.py` или там, где регистрируются роутеры
- Тест: `tests/test_stars.py` (создать)

**Интерфейсы:**
- Потребляет из задачи 3: `pricing.DEPOSIT_BONUS_PERCENT`.
- Отдаёт: `pricing.stars_for_kopeks(kopeks: int) -> int`, модель `StarPayment(charge_id, user_id, amount_kopeks, stars, created_at)`.

- [ ] **Шаг 1: Написать падающий тест на пересчёт**

Создать `tests/test_stars.py`:

```python
"""Оплата звёздами: пересчёт и защита от двойного зачисления."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.services.pricing import monthly_price_kopeks, stars_for_kopeks


class TestStarPrice:
    def test_markup_is_twentyfive_percent(self) -> None:
        assert stars_for_kopeks(120_00) == 150
        assert stars_for_kopeks(160_00) == 200
        assert stars_for_kopeks(1080_00) == 1350

    def test_fraction_rounds_up(self) -> None:
        """Дробной звезды не бывает. Округление вниз означало бы, что сервис
        дарит долю звезды на каждой покупке."""
        assert stars_for_kopeks(90_00) == 113        # 112,5 → 113

    def test_typical_tariff_in_stars(self) -> None:
        assert stars_for_kopeks(monthly_price_kopeks(1, 1)) == 150
```

- [ ] **Шаг 2: Убедиться, что тест падает**

Запустить: `python -m pytest tests/test_stars.py -v`
Ожидается: FAIL — `stars_for_kopeks` не существует.

- [ ] **Шаг 3: Курс и наценка**

В `bot/config.py`:

```python
    # Оплата звёздами Telegram. Курс плавает (цена звезды привязана к доллару),
    # поэтому живёт в настройках, а не в коде. Наценка компенсирует то, что
    # звёзды доходят до владельца дороже рублей: вывод через Fragment, комиссии,
    # холд около трёх недель.
    star_price_kopeks: int = 100     # сколько копеек стоит одна звезда
    star_markup_percent: int = 25
```

В `bot/services/pricing.py`:

```python
def stars_for_kopeks(kopeks: int) -> int:
    """Во сколько звёзд обходится сумма в копейках, с наценкой за способ.

    Округление ВВЕРХ до целой звезды: дробной звезды не бывает, а округление
    вниз дарило бы юзеру долю звезды на каждой покупке.
    """
    with_markup = kopeks * (100 + settings.star_markup_percent)
    return -(-with_markup // (100 * settings.star_price_kopeks))
```

- [ ] **Шаг 4: Прогнать тест пересчёта**

Запустить: `python -m pytest tests/test_stars.py -k StarPrice -v`
Ожидается: PASS.

- [ ] **Шаг 5: Таблица звёздных платежей**

В `bot/db/models.py`:

```python
class StarPayment(Base):
    """Оплата звёздами. Хранится ради двух вещей: защиты от двойного
    зачисления (Telegram может доставить событие повторно) и возврата —
    `charge_id` это единственное, чем звёзды возвращаются юзеру."""

    __tablename__ = "star_payments"

    charge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount_kopeks: Mapped[int] = mapped_column(Integer)
    stars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
```

Новую таблицу создаёт `create_all` при старте — миграции не нужно.

- [ ] **Шаг 6: Написать падающий тест на идемпотентность**

Дописать в `tests/test_stars.py`:

```python
class TestStarCredit:
    async def test_credits_balance_once(self, session: AsyncSession) -> None:
        """Повторно доставленный платёж не должен зачислиться дважды."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4301, username="u", full_name="U"
        )
        await session.commit()

        first = await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-1",
            amount_kopeks=120_00, stars=150,
        )
        await session.commit()
        second = await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-1",
            amount_kopeks=120_00, stars=150,
        )
        await session.commit()

        assert first is True
        assert second is False, "повторная доставка зачислила деньги второй раз"
        assert user.balance_kopeks == 120_00

    async def test_no_bonus_on_stars(self, session: AsyncSession) -> None:
        """У звёзд своя наценка 25 % — бонус поверх неё был бы
        взаимоисключающим."""
        from bot.services import stars as stars_svc

        user = await repo.get_or_create_user(
            session, tg_id=4302, username="u", full_name="U"
        )
        await session.commit()

        await stars_svc.credit_star_payment(
            session, user_id=user.id, charge_id="ch-2",
            amount_kopeks=100_00, stars=125,
        )
        await session.commit()

        rows = await repo.list_balance_txs(session, user.id, limit=10)
        assert {r.kind for r in rows} == {"deposit"}
        assert user.balance_kopeks == 100_00
```

- [ ] **Шаг 7: Сервис зачисления**

Создать `bot/services/stars.py`:

```python
"""Зачисление оплаты звёздами на баланс.

Звёзды пополняют баланс, а не покупают подписку напрямую: покупка,
автопродление, реферальные начисления и история операций уже ходят через
баланс, и второй путь пришлось бы дублировать в каждом из этих мест.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import AuditAction, StarPayment


async def credit_star_payment(
    session: AsyncSession, *, user_id: int, charge_id: str,
    amount_kopeks: int, stars: int,
) -> bool:
    """Зачисляет оплату звёздами. True — зачислили, False — этот платёж уже был.

    Идемпотентность по `charge_id`: Telegram может доставить событие оплаты
    повторно, и без этой проверки баланс вырос бы дважды. Коммит — на
    вызывающем.

    Бонуса за способ у звёзд нет: у них своя наценка 25 %, и бонус поверх неё
    был бы взаимоисключающим.
    """
    if await session.get(StarPayment, charge_id) is not None:
        logger.info("Star payment {} already credited, skipping", charge_id)
        return False
    session.add(StarPayment(
        charge_id=charge_id, user_id=user_id,
        amount_kopeks=amount_kopeks, stars=stars,
    ))
    await repo.add_balance_tx(
        session, user_id, amount_kopeks, "deposit",
        note=f"Пополнение звёздами ({stars} ⭐)",
    )
    user = await repo.get_user_by_id(session, user_id)
    await repo.log_action(
        session, AuditAction.BALANCE_TOPUP,
        actor_tg_id=user.tg_id if user is not None else None,
        target_user_id=user_id,
        amount_kopeks=amount_kopeks,
        details=f"Пополнение баланса звёздами ({stars} ⭐)",
    )
    logger.info("Star payment {}: user {} +{} kopeks", charge_id, user_id, amount_kopeks)
    return True
```

- [ ] **Шаг 8: Прогнать тесты зачисления**

Запустить: `python -m pytest tests/test_stars.py -v`
Ожидается: PASS.

- [ ] **Шаг 9: Хендлеры оплаты**

Создать `bot/handlers/stars.py`:

```python
"""Оплата звёздами Telegram: счёт, подтверждение, зачисление."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.keyboards.inline import CB_BAL, back_to_menu
from bot.services import stars as stars_svc
from bot.services.pricing import fmt_rub, stars_for_kopeks

router = Router(name="stars")

_PAYLOAD_PREFIX = "stars"


@router.callback_query(F.data.startswith(f"{CB_BAL}:star:"))
async def cb_star_invoice(call: CallbackQuery, session: AsyncSession) -> None:
    raw = call.data.rsplit(":", 1)[-1]
    if not raw.isdigit():
        await call.answer("Некорректная сумма.", show_alert=True)
        return
    kopeks = int(raw) * 100
    user = await repo.get_or_create_user(
        session, tg_id=call.from_user.id,
        username=call.from_user.username, full_name=call.from_user.full_name,
    )
    await session.commit()
    stars = stars_for_kopeks(kopeks)
    await call.message.answer_invoice(
        title="Пополнение баланса",
        description=(
            f"На баланс зачислим {fmt_rub(kopeks)}. "
            f"Наценка за оплату звёздами — {settings.star_markup_percent}%."
        ),
        payload=f"{_PAYLOAD_PREFIX}:{user.id}:{kopeks}",
        currency="XTR",
        prices=[LabeledPrice(label=fmt_rub(kopeks), amount=stars)],
    )
    await call.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    # Отвечать обязательно и в течение 10 секунд, иначе Telegram отменит
    # платёж. Проверять здесь нечего: сумма и получатель заданы нами.
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, session: AsyncSession) -> None:
    sp = message.successful_payment
    parts = (sp.invoice_payload or "").split(":")
    if len(parts) != 3 or parts[0] != _PAYLOAD_PREFIX:
        logger.error("Unexpected successful_payment payload: {}", sp.invoice_payload)
        return
    user_id, kopeks = int(parts[1]), int(parts[2])
    credited = await stars_svc.credit_star_payment(
        session, user_id=user_id, charge_id=sp.telegram_payment_charge_id,
        amount_kopeks=kopeks, stars=sp.total_amount,
    )
    await session.commit()
    if not credited:
        return
    user = await repo.get_user_by_id(session, user_id)
    await message.answer(
        f"✅ Баланс пополнен на <b>{fmt_rub(kopeks)}</b>.\n"
        f"💰 Сейчас на балансе: <b>{fmt_rub(user.balance_kopeks)}</b>",
        reply_markup=back_to_menu(),
    )
```

Зарегистрировать роутер в `bot/handlers/__init__.py` сразу после `dp.include_router(balance.router)` (строка 32) и добавить `stars` в импорт модулей в шапке файла.

- [ ] **Шаг 10: Экран выбора способа**

В `bot/handlers/balance.py` переписать `cb_bal_deposit` так, чтобы он показывал выбор способа, а не сразу суммы:

```python
@router.callback_query(F.data == f"{CB_BAL}:dep")
async def cb_bal_deposit(call: CallbackQuery, session: AsyncSession) -> None:
    if not cryptopay.enabled():
        await call.answer("Пополнение временно недоступно.", show_alert=True)
        return
    await call.message.edit_text(
        "➕ <b>Пополнение баланса</b>\n\n"
        f"💎 <b>@CryptoBot</b> — оплата в рублях, крипту можно купить с карты "
        f"прямо там. Начислим <b>+{DEPOSIT_BONUS_PERCENT['cryptobot']}%</b> сверху.\n\n"
        f"⭐ <b>Звёзды Telegram</b> — оплата в два касания, не выходя из "
        f"Telegram. Дороже на {settings.star_markup_percent}%: звёзды доходят "
        "до нас через вывод с комиссиями и трёхнедельной задержкой, наценка "
        "это и покрывает.\n"
        "<i>Отдельно: Apple и App Store берут свою долю при покупке самих "
        "звёзд в приложении — это не наша комиссия, мы её не получаем. "
        "Дешевле покупать звёзды не через приложение.</i>",
        reply_markup=deposit_methods_kb(),
    )
    await call.answer()
```

В `bot/keyboards/inline/balance.py` добавить `deposit_methods_kb()` с двумя кнопками (`{CB_BAL}:dep:cb` и `{CB_BAL}:dep:stars`) и «Назад», а экран сумм разделить на два: прежний для CryptoBot и такой же для звёзд, где на кнопках рядом с рублями стоит цена в звёздах.

- [ ] **Шаг 11: Полный набор и коммит**

Запустить: `python -m pytest`
Ожидается: падают только 2 теста `tests/test_qrgen.py`.

```bash
git add bot/config.py bot/services/pricing.py bot/services/stars.py bot/handlers/stars.py bot/handlers/balance.py bot/keyboards/inline/balance.py bot/db/models.py tests/test_stars.py
git commit -m "Оплата звёздами Telegram пополняет баланс"
```

---

### Задача 6: README и выкатка

**Файлы:**
- Изменить: `README.md`
- Изменить: `.env.example`

- [ ] **Шаг 1: README**

Дописать в разделе возможностей: способы пополнения (CryptoBot с бонусом, звёзды с наценкой), почему звёзды идут на баланс, флаг служебного аккаунта и что он меняет в статистике. В таблицу переменных окружения добавить `STAR_PRICE_KOPEKS` и `STAR_MARKUP_PERCENT`. В дереве файлов добавить `bot/handlers/stars.py` и `bot/services/stars.py`.

- [ ] **Шаг 2: Полный набор**

Запустить: `python -m pytest`
Ожидается: падают только 2 теста `tests/test_qrgen.py`. Записать итоговое число зелёных.

- [ ] **Шаг 3: Слияние и выкатка**

```bash
git add README.md .env.example
git commit -m "README: новый прайс, способы пополнения, служебные аккаунты"
git fetch origin && git rebase origin/main
python -m pytest
git push origin etap-d-dengi:main
ssh klopas 'git -C /root/myvpn-bot pull --ff-only && systemctl restart myvpn-bot && sleep 8 && systemctl is-active myvpn-bot'
```

- [ ] **Шаг 4: Проверить прод**

```bash
ssh klopas 'journalctl -u myvpn-bot --since "5 minutes ago" --no-pager | tail -30'
```

Ожидается: строка про добавленную колонку `is_staff`, создание таблицы `star_payments`, `Bot started`, ноль ошибок. Дождаться тика планировщика (5 минут) и убедиться, что он прошёл без исключений.

- [ ] **Шаг 5: Проставить пометки служебным**

Через админку бота включить «🧰 Служебный» у `repflez1`, `UNDRY123` и `plategabottest`. Проверить, что экран статистики после этого показывает «Конверсия: 0 из 3».

---

## Самопроверка плана

- **Покрытие спеки.** Прайс — задачи 1–2. Витрина и старт конструктора — задача 2. Бонусы за способ пополнения — задача 3. Честная статистика — задача 4. Звёзды — задача 5. Platega — вынесена за границы плана явно, отдельным разделом. Действующие подписчики: отдельной работы не требуют, автопродление само считает по текущему прайсу; рассылки решено не делать. Пробный период не трогается.
- **Типы и имена.** `monthly_price_kopeks`/`term_price_kopeks` сигнатуры не меняются. Новые имена: `settings.price_first_rub`, `settings.star_price_kopeks`, `settings.star_markup_percent`, `pricing.deposit_bonus_kopeks`, `pricing.DEPOSIT_BONUS_PERCENT`, `pricing.stars_for_kopeks`, `stars.credit_star_payment`, `User.is_staff`, `StarPayment`, `stats.collect_money_stats`, `stats.MoneyStats`, `balance._START_DEVICES`, `balance._START_BYPASS`, `balance._extend_intro`. Использованы в задачах ровно под этими именами.
- **Риск переименования настройки — проверен и закрыт.** `PRICE_BASE_RUB` становится `PRICE_FIRST_RUB`; если бы старое имя стояло в проде, после выкатки оно перестало бы действовать и цена молча вернулась бы к умолчанию. Проверено 8.08: `grep -E "^PRICE|^TRIAL|^REFERRAL" /root/myvpn-bot/.env` на klopas не находит ничего — все цены берутся из умолчаний в коде. Действий не требуется, но если к моменту реализации `.env` изменится, проверить снова.
