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
