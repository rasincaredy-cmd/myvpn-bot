from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: str = Field(..., min_length=10)
    # NoDecode: не давать pydantic'у JSON-парсить эту переменную окружения —
    # хотим, чтобы field_validator получил сырую строку "111,222".
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    db_url: str = "sqlite+aiosqlite:///./data/vpn_bot.sqlite3"

    encryption_key: str = Field(..., min_length=32)

    log_level: str = "INFO"

    ssh_connect_timeout: int = 20
    ssh_command_timeout: int = 900

    default_amnezia_port: int = 585

    # Обход белых списков (wdtt). Общий на весь сервис список ссылок на звонки VK
    # через запятую (без пробелов и без https). Одна и более: больше хешей — выше
    # лимит потоков и лучше распределение нагрузки. Пусто = фича выключена.
    wdtt_vk_hashes: str = ""
    wdtt_binary_path: str = "/usr/local/bin/wdtt-server"

    # Подписка/триал (Блок 9). Новым юзерам авто-выдаём триал.
    # 1 устройство: триал не должен быть жирнее платной базы (90₽ = 1+1).
    trial_devices: int = 1
    # Резервных подключений на триале. Без этой настройки триал брал умолчание
    # модели (2) и выходил жирнее платной базы 1+1 за 90 ₽ — юзеру незачем
    # платить за то, что бесплатно дают в большем объёме.
    trial_bypass: int = 1
    trial_days: int = 7
    # Лимит трафика триала в ГБ на подписку (0 = безлимит).
    trial_traffic_gb: int = 10

    # Контакт поддержки/связи с админом (напр. "@vlad" или "https://t.me/...").
    # Пусто → в тексте помощи предложим написать через /start у админа.
    support_contact: str = ""

    # ── Блок «Баланс»: оплата через Crypto Pay (@CryptoBot) ────────────────
    # Токен приложения Crypto Pay. Пусто = оплата выключена: разделы пополнения
    # и продления скрыты, работает только ручное начисление админом.
    cryptopay_token: str = ""
    # Реф-награда: % от КАЖДОГО пополнения реферала, падает на баланс пригласившего.
    referral_percent: int = 15
    # Цены, ₽/мес. Первая позиция (устройство ИЛИ резервное подключение) стоит
    # price_first_rub — это пол тарифа, дешевле не бывает. Каждая следующая
    # позиция прибавляется по своей цене. Формула только складывает: уйти в
    # минус, как могла прежняя (она вычитала неиспользуемое из базы), неоткуда.
    price_first_rub: int = 90
    price_extra_device_rub: int = 40
    price_extra_bypass_rub: int = 30

    # Оплата звёздами Telegram. Курс плавает (цена звезды привязана к доллару),
    # поэтому живёт в настройках, а не в коде. Наценка компенсирует то, что
    # звёзды доходят до владельца дороже рублей: вывод через Fragment, комиссии,
    # холд около трёх недель.
    star_price_kopeks: int = 100     # сколько копеек стоит одна звезда
    star_markup_percent: int = 25

    # Раз в сколько дней проверять живость внешних ссылок на обход-приложения
    # (_PLATFORMS: GitHub-релизы, TestFlight). При проблемах — алерт админам.
    # 0 = выключено.
    linkcheck_interval_days: int = 3

    # Сколько дней держим записи журнала действий. 0 = не чистить никогда.
    # 90 дней хватает, чтобы разобрать спор по оплате, и база не пухнет.
    audit_retention_days: int = 90

    # ── Блок «Бэкап» ─────────────────────────────────────────────────────────
    # Пароль шифрования бэкапов (БД + .env): хранить и ВНЕ VPS (менеджер паролей).
    # Пусто = бэкапы отключены. Пароль нужен и для восстановления — без него
    # бэкап не расшифровать.
    backup_password: str = ""
    # Час UTC для ночного бэкапа (планировщик пошлёт файл админам раз в день).
    backup_hour_utc: int = 3

    # ── Юридические документы (требование платёжного провайдера) ─────────────
    # Адреса страниц на telegra.ph. Пусто = кнопки в меню не показываются.
    legal_privacy_url: str = ""
    legal_terms_url: str = ""
    # Токен telegra.ph: нужен, чтобы ПРАВИТЬ уже опубликованные страницы (без
    # него правка создаёт новый адрес, а старый висит с устаревшим текстом).
    telegraph_token: str = ""
    # Премиум-эмодзи в текстах и на кнопках. Работают ТОЛЬКО если у владельца
    # бота активен Telegram Premium (правило Bot API от 09.02.2026); без него
    # Telegram молча выбрасывает их. Проверено 05.08.2026: прав нет, поэтому
    # по умолчанию выключено.
    premium_emoji_enabled: bool = False

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v: object) -> object:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @property
    def data_dir(self) -> Path:
        return Path("data")


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
