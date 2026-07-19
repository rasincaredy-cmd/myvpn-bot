# MyVPN — Telegram-бот для AmneziaWG

Telegram-бот, который ставит **AmneziaWG** на твой VPS и раздаёт peer-конфиги через чат и одноразовые инвайт-ссылки.

Бот не пропускает трафик через себя — это «пульт управления». Сам VPN живёт на VPS, который ты добавляешь через `/install`. Один бот может управлять несколькими серверами одновременно.

```
┌──────────────────┐   SSH    ┌──────────────────┐  WireGuard/UDP  ┌──────────┐
│ Бот @MyVPN_bot   │ ───────► │  VPN-VPS         │  ◄────────────  │  Клиент  │
│  (этот процесс)  │          │  AmneziaWG awg0  │                 │ AmneziaVPN│
└─────────▲────────┘          └──────────────────┘                 └──────────┘
          │ HTTPS
     Telegram (ты)
```

## Что умеет

- **`/install`** — по SSH ставит на чистый Ubuntu/Debian VPS: PPA `amnezia/ppa`, `amneziawg` + `amneziawg-tools`, заголовки ядра, DKMS-модуль; генерирует серверные ключи и параметры обфускации (Jc/Jmin/Jmax/S1/S2/H1..H4); настраивает iptables/UFW; поднимает интерфейс `awg0` и включает автозапуск.
- **`/newpeer`** — заходит на готовый сервер, генерирует ключи peer'а прямо на VPS, аллоцирует IP в `10.8.0.0/24`, добавляет peer через `awg set`, присылает в чат `.conf` и QR-код.
- **`/invite`** — одноразовая ссылка `t.me/<bot>?start=<token>` для друга. Друг открывает → бот сам создаёт ему peer и присылает конфиг.
- **Каскадное удаление сервера** — при удалении из меню бот заходит по SSH и **снимает с VPS всё, что устанавливал**: останавливает `awg0`, выключает systemd-юнит, `apt purge amneziawg*`, удаляет `/etc/amnezia/amneziawg/`, закрывает UFW-порт.
- **`/stats`** — счётчики юзеров / серверов / peer'ов / инвайтов.
- **Учёт трафика** — накопленный трафик пира хранится в БД и **переживает ребут VPS** (счётчик `awg` при рестарте обнуляется — бот ловит сброс и суммирует дельты), поэтому лимиты считаются честно.
- **Отзыв с ревайвом** — при истечении подписки устройства и доступы обхода отключаются, но их конфиги хранятся 30 дней; при продлении всё оживает само: WG-пиры возвращаются на сервер с теми же ключами/IP, wdtt-пароли восстанавливаются на сервере обхода (`ctl add -password`) — юзеру ничего не нужно перенастраивать.
- **Распределение по серверам** — юзер выбирает только локацию («🇳🇱 Нидерланды»), конкретный сервер внутри неё бот берёт сам: наименее загруженный по активным пирам (WG) или обходам (wdtt); заполненные сервера обхода (лимит `wdtt_max_accesses` в карточке сервера) юзерам не предлагаются. В имени конфига в приложении — локация без номера сервера; локации при добавлении сервера выбираются кнопками (нет дублей из-за опечаток).
- **Баланс, оплата и рефералка** — юзер пополняет баланс криптой через @CryptoBot (Crypto Pay, инвойсы в рублях; `CRYPTOPAY_TOKEN` в `.env`, пусто = выключено), подписка покупается с баланса: тариф 90₽/мес за 1 устройство + 1 обход БС, +30₽ за каждое следующее, скидки за срок (3/6/12 мес − 10/15/25%). Срок прибавляется к остатку, при истечении работает автопродление с баланса (отключаемо юзером). Реферальная ссылка `?start=ref_<id>`: пригласившему падает `REFERRAL_PERCENT` (15%) с каждого пополнения реферала. Все движения денег — в журнале `balance_txs` (копейки, без float); админ может начислять/списывать вручную (например, за перевод на карту).
- **Режим «RU напрямую»** — кнопка в карточке устройства выдаёт версию конфига с раздельным туннелированием на уровне WireGuard AllowedIPs: российские подсети (снапшот ipdeny в `bot/assets/ru_networks.txt`, блоки ≤ `/21` — порог `RU_DIRECT_MAX_PREFIXLEN`, ≈ 92% RU-адресов и все ключевые сервисы: mail.ru, сбер, vk, яндекс, госуслуги, reg.ru, avito) и LAN идут мимо VPN, весь остальной мир — через туннель. Работает в любом WG-клиенте; выдаётся только `.conf`-файлом (~8500 маршрутов, ~132 КБ — не влезают в QR и `vpn://`). Порог выше НЕ ставить: полное покрытие реестра (~21600 маршрутов, ~348 КБ) роняет клиент Amnezia при импорте; ориентир переваримого — 11216 маршрутов из amnezia-client#2248 (Android/iOS ок, Windows подключается минуты). Мелкие RU-подсети (ozon/wildberries в `/22` и мельче) едут через VPN — безопасное направление ошибки: заблокированный ресурс не окажется «напрямую». Обычные конфиги — полный туннель со строкой `AllowedIPs = 0.0.0.0/0, ::/0` ровно в том виде, в котором клиент Amnezia распознаёт полный туннель и разблокирует своё меню раздельного туннелирования.
- **Авто-миграции** — при старте бот сам добавляет недостающие колонки в существующую БД (`ALTER TABLE ADD COLUMN`), так что обновление на месте не ломает старую базу.
- **Безопасность**: SSH-креды шифруются Fernet, сообщения с паролем/ключом удаляются из чата сразу, FSM-storage — memory-only (после рестарта бота временные креды теряются).

## Требования к VPN-серверу

| Что | Требование |
| --- | --- |
| ОС | Ubuntu 20.04+ или Debian 11+ |
| Виртуализация | **KVM** (на OpenVZ/LXC своё ядро не загрузить — DKMS не сработает) |
| Доступ | root по SSH (пароль или ключ) |
| Cloud firewall | Открыть UDP-порт (по умолчанию `585`) у провайдера |

Это может быть **тот же VPS, что и бот** — он сходит по SSH сам на себя.

## Стек

aiogram 3.x · SQLAlchemy 2.0 async (SQLite/aiosqlite) · asyncssh · cryptography (Fernet) · pydantic-settings · loguru · qrcode

## Быстрый старт (хост бота)

Нужно: Ubuntu 22.04+/Debian с **Python 3.11+** (на 3.10 не запустится — в моделях используется `StrEnum`).

```bash
# 1. Клонировать
git clone https://github.com/<you>/myvpn-bot.git /opt/myvpn-bot
cd /opt/myvpn-bot

# 2. venv + зависимости
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. ENCRYPTION_KEY (Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. .env
cp .env.example .env
nano .env   # вписать BOT_TOKEN, ADMIN_IDS, ENCRYPTION_KEY

# 5. Запуск
python -m bot
```

Открой бота в Telegram → `/start` → «🛠 Установить VPN на VPS» → следуй мастеру.

## Запуск под systemd

```ini
# /etc/systemd/system/myvpn-bot.service
[Unit]
Description=MyVPN Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/myvpn-bot
EnvironmentFile=/opt/myvpn-bot/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/myvpn-bot/.venv/bin/python -m bot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now myvpn-bot
sudo journalctl -u myvpn-bot -f
```

## Конфигурация (`.env`)

| Переменная | Обязательная | По умолчанию | Что |
| --- | --- | --- | --- |
| `BOT_TOKEN` | да | — | Токен от @BotFather |
| `ADMIN_IDS` | да | — | Telegram user_id админов через запятую |
| `ENCRYPTION_KEY` | да | — | Fernet-ключ для шифрования SSH-кредов в БД |
| `DB_URL` | нет | `sqlite+aiosqlite:///./data/vpn_bot.sqlite3` | Можно Postgres: `postgresql+asyncpg://...` |
| `LOG_LEVEL` | нет | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `SSH_CONNECT_TIMEOUT` | нет | `20` | секунды |
| `SSH_COMMAND_TIMEOUT` | нет | `900` | секунды |
| `DEFAULT_AMNEZIA_PORT` | нет | `585` | UDP-порт по умолчанию для `/install` |

## Команды

| Команда | Кто | Что делает |
| ------- | --- | ---------- |
| `/start` | все | Главное меню; `/start <token>` погасит инвайт и выдаст конфиг |
| `/menu` | все | Открыть меню |
| `/help` | все | Справка |
| `/exit`, `/cancel` | все | Отменить любое FSM-действие |
| `/install` | админ | Поставить VPN на новый VPS |
| `/servers` | админ | Список серверов |
| `/newpeer` | админ | Создать peer-конфиг |
| `/invite` | админ | Одноразовая инвайт-ссылка |
| `/stats` | админ | Статистика |

## Что бот делает на VPN-сервере во время `/install`

| Шаг | Команда |
| --- | ------ |
| Обновить apt | `apt-get update && apt-get install -y software-properties-common iptables-persistent qrencode ...` |
| Подключить PPA | `add-apt-repository -y ppa:amnezia/ppa` |
| Headers и DKMS | `apt-get install -y dkms build-essential linux-headers-$(uname -r)` |
| Поставить AWG | `apt-get install -y amneziawg amneziawg-tools` |
| Собрать модуль | `dkms autoinstall -k $(uname -r) && modprobe amneziawg` |
| IP-forward | `net.ipv4.ip_forward=1` в `/etc/sysctl.conf` |
| Серверные ключи | `awg genkey \| awg pubkey` → `/etc/amnezia/amneziawg/server.{key,pub}` |
| Конфиг | `/etc/amnezia/amneziawg/awg0.conf` с обфускацией (Jc/Jmin/Jmax/S1/S2/H1..H4) |
| Файрвол | `ufw allow <port>/udp` (если ufw активен) + iptables-persistent |
| Старт | `awg-quick up awg0` + `systemctl enable awg-quick@awg0` |

## Что бот удаляет при удалении сервера

Зеркало установки — best-effort, ошибки одной команды не блокируют остальные:

```
awg-quick down awg0
systemctl disable awg-quick@awg0
apt purge -y amneziawg amneziawg-tools amneziawg-dkms
rm -rf /etc/amnezia/amneziawg
ufw delete allow <port>/udp
```

PPA и `net.ipv4.ip_forward` **не трогаем** — безвредны и могут быть нужны другим сервисам.

## Подключение клиента

⚠️ Обычный WireGuard-клиент **не подойдёт** — нужен AmneziaVPN-клиент (он умеет Jc/Jmin/H1..H4).

| Платформа | Откуда |
| --- | --- |
| iOS / macOS | App Store: «AmneziaVPN» |
| Android | Google Play / RuStore: «AmneziaVPN» |
| Windows / Linux | <https://amnezia.org/downloads> |

Импортировать `.conf` файлом или отсканировать QR из бота → «Подключиться».

## Структура

```
bot/
├── __main__.py          # точка входа (python -m bot)
├── config.py            # настройки из .env (pydantic-settings)
├── loader.py            # Bot + Dispatcher + storage
├── db/
│   ├── base.py          # async engine + session factory
│   ├── models.py        # User / Server / Peer / Invite
│   └── repo.py          # репозиторий + creds_from_server
├── handlers/
│   ├── common.py        # /start /menu /help /exit, deep-link инвайтов
│   ├── menu.py          # карточки серверов, каскадное удаление
│   ├── install.py       # FSM установки AmneziaWG
│   ├── configs.py       # выдача peer'ов и инвайты
│   └── admin.py         # /stats
├── middlewares/
│   ├── db.py            # session per update
│   └── throttle.py      # antiflood (TTL-кеш)
├── services/
│   ├── crypto.py        # Fernet encrypt/decrypt
│   ├── ssh.py           # asyncssh-обёртка
│   ├── amnezia.py       # install / uninstall / peer management
│   └── qrgen.py         # QR из .conf
├── states/install.py    # FSM-states
├── filters/admin.py     # AdminFilter (ADMIN_IDS)
├── keyboards/inline.py  # все inline-клавиатуры
├── texts/ru.py          # все тексты в одном месте
└── utils/
    ├── validators.py
    └── menu_commands.py
```

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

## Безопасность

- **SSH-креды** хранятся в БД только зашифрованными (Fernet/AES-128-CBC + HMAC). Без `ENCRYPTION_KEY` расшифровать невозможно.
- **Сообщения с паролем/ключом** удаляются из чата немедленно после получения.
- **FSM-storage** — memory-only: при рестарте бота временные креды теряются (сознательное решение — в Redis их не пишем).
- **Логи** — `loguru` с `diagnose=False` и осторожным превью команд, чтобы секреты не уезжали в stderr.
- **AdminFilter** — административные команды доступны только `ADMIN_IDS` из `.env`.
- **Antiflood** — TTL-кеш на 0.7 с против спама / случайного даблклика.

## Лицензия

MIT — см. [LICENSE](LICENSE).
