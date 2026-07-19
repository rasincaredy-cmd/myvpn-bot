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
    trial_devices: int = 2
    trial_days: int = 7
    # Лимит трафика триала в ГБ на подписку (0 = безлимит).
    trial_traffic_gb: int = 10

    # Контакт поддержки/связи с админом (напр. "@vlad" или "https://t.me/...").
    # Пусто → в тексте помощи предложим написать через /start у админа.
    support_contact: str = ""

    # ── Блок «RU напрямую» ──────────────────────────────────────────────────
    # Напрямую (мимо туннеля) идут RU-блоки с prefixlen <= этого порога.
    # /18 = 66% RU-адресов, ~2100 маршрутов, ~31КБ — ЕДИНСТВЕННОЕ проверенно
    # работающее значение на телефоне Влада (Android, Amnezia). Эмпирика 19.07.2026:
    # /21 (8496 маршрутов, 132КБ) — импорт ок, но туннель не стартует
    # («останавливается»); /32 (21643 маршрута, 348КБ) — краш приложения при
    # импорте. Потолок клиента где-то между 2141 и 8496 маршрутами. mail.ru/сбер
    # (/21), ozon/wildberries (/22) при /18 едут через VPN — осознанная жертва
    # ради стабильности; хочешь доменный сплит как в XRay — это смена протокола,
    # не крутилка здесь.
    ru_direct_max_prefixlen: int = 18

    # ── Блок «Баланс»: оплата через Crypto Pay (@CryptoBot) ────────────────
    # Токен приложения Crypto Pay. Пусто = оплата выключена: разделы пополнения
    # и продления скрыты, работает только ручное начисление админом.
    cryptopay_token: str = ""
    # Реф-награда: % от КАЖДОГО пополнения реферала, падает на баланс пригласившего.
    referral_percent: int = 15
    # Цены, ₽/мес: база (1 устройство + 1 обход БС) и каждое следующее.
    price_base_rub: int = 90
    price_extra_device_rub: int = 30
    price_extra_bypass_rub: int = 30

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
