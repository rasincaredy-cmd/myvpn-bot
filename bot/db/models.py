from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


class ServerStatus(StrEnum):
    PENDING = "pending"
    INSTALLING = "installing"
    READY = "ready"
    FAILED = "failed"


class PeerStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Предупреждать пользователя о скором истечении его конфигов (можно выключить).
    expiry_warn_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # --- Подписка (Блок 9) ---
    # Лимит устройств у юзера. server_default="2" → существующие юзеры при миграции
    # получают 2 (грандфазер). Новым ставим триал в get_or_create_user.
    sub_max_devices: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    # Срок подписки. NULL = без ограничения по времени (грандфазер/бессрочно).
    # Единый таймер сервиса: истёк → планировщик отзывает ВСЕ устройства юзера.
    sub_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Лимит трафика на подписку (не на пир). NULL = безлимит. Расход считается
    # суммарно по всем пирам юзера за текущий период:
    #   расход_периода = Σ(peer.traffic_used_bytes) − sub_traffic_base_bytes.
    # base — снимок суммарного трафика на начало периода; сбрасывается при продлении.
    sub_traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sub_traffic_base_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    # Лимит активных доступов обхода БС у юзера (по умолчанию 2).
    sub_max_bypass: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    # Битовая маска отправленных предупреждений о скором истечении ПОДПИСКИ
    # (биты = scheduler.WARN_OFFSETS_HOURS). Сбрасывается при продлении срока.
    sub_warn_flags: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    peers: Mapped[list["Peer"]] = relationship(back_populates="user")


class Server(Base):
    """VPN-сервер, к которому бот подключается по SSH и где стоит AmneziaWG."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")

    # Зашифрованные креды (Fernet). Не логировать, не показывать пользователю.
    ssh_password_enc: Mapped[bytes | None] = mapped_column()
    ssh_key_enc: Mapped[bytes | None] = mapped_column()
    ssh_key_passphrase_enc: Mapped[bytes | None] = mapped_column()

    wg_port: Mapped[int] = mapped_column(Integer)
    wg_interface: Mapped[str] = mapped_column(String(32), default="awg0")
    wg_subnet: Mapped[str] = mapped_column(String(32), default="10.8.0.0/24")

    # Серверные данные AmneziaWG (нужны для генерации peer-конфигов).
    server_public_key: Mapped[str | None] = mapped_column(String(64))
    server_endpoint: Mapped[str | None] = mapped_column(String(255))

    # Параметры обфускации AmneziaWG (Jc/Jmin/Jmax/S1/S2/H1..H4).
    awg_params_json: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ServerStatus] = mapped_column(
        String(16), default=ServerStatus.PENDING
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    # owner_tg_id — кем сервер установлен. С Блока 8 это лишь пометка: серверы —
    # общий пул сервиса, любой админ управляет всеми (владения больше нет).
    owner_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Локация (Блок 8): страна с флагом для витрины «🌍 Локации», напр. «🇩🇪 Германия».
    # NULL — не задана (показываем имя сервера как fallback).
    location: Mapped[str | None] = mapped_column(String(64))

    # Обход белых списков (wdtt / proxy-turn-vk): включён ли демон на этом сервере
    # и его порты "dtls,wg,tun". Выдача wdtt-доступов доступна только при wdtt_enabled.
    wdtt_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    wdtt_ports: Mapped[str | None] = mapped_column(
        String(32), default="56000,56001,9000"
    )

    peers: Mapped[list["Peer"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class Peer(Base):
    """Клиентский peer на конкретном сервере, выданный конкретному Telegram-юзеру."""

    __tablename__ = "peers"
    __table_args__ = (
        UniqueConstraint("server_id", "ip", name="uq_peer_server_ip"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Устройство, к которому относится пир (Блок 9). NULL — легаси-пиры до миграции.
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )

    label: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(String(64))

    # Приватник peer'а нужен для регенерации конфига позже — храним зашифрованным.
    private_key_enc: Mapped[bytes] = mapped_column()

    status: Mapped[PeerStatus] = mapped_column(String(16), default=PeerStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at:           Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    traffic_limit_bytes:  Mapped[int | None]       = mapped_column(BigInteger)

    # Учёт трафика с защитой от сброса счётчика awg (ребут / awg-quick down-up).
    # traffic_used_bytes     — накопленный трафик (rx+tx) за всё время пира;
    # traffic_last_raw_bytes — последнее сырое показание awg, чтобы считать дельту.
    traffic_used_bytes:     Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    traffic_last_raw_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    # Битовая маска уже отправленных предупреждений об истечении.
    # Биты соответствуют порогам scheduler.WARN_OFFSETS_HOURS (бит 0 = 24ч, бит 1 = 1ч).
    # Сбрасывается при смене срока действия пира.
    expiry_warn_flags: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    server: Mapped[Server] = relationship(back_populates="peers")
    user: Mapped[User] = relationship(back_populates="peers")


class Device(Base):
    """Устройство пользователя (Блок 9) — единица, которую лимитирует подписка.

    Группирует конфиги: сейчас (1 сервер) это один WG-пир; при мультилокации —
    по пиру на локацию. Доступы обхода БС (WdttAccess) привязываются к устройству
    отдельно, по кнопке, с выбором сервера.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(64))
    status: Mapped[PeerStatus] = mapped_column(String(16), default=PeerStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Invite(Base):
    """Одноразовый токен для друзей: /start <token> привязывает peer к новому юзеру."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE")
    )
    issued_by_tg_id: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str | None] = mapped_column(String(64))
    used_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WdttAccess(Base):
    """Доступ обхода белых списков (wdtt): пароль на wdtt-сервере + wdtt://-ссылка.

    В отличие от Peer, WG-ключи/IP генерит сам wdtt-сервер при первом коннекте,
    поэтому здесь храним только выданную ссылку (с паролем) и сам пароль — оба
    зашифрованы Fernet, т.к. это секреты.
    """

    __tablename__ = "wdtt_accesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Устройство, к которому привязан доступ обхода (Блок 9). NULL — легаси.
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    label: Mapped[str] = mapped_column(String(64))
    # Платформа доступа (android/ios/pc) — показываем в карточке. NULL — легаси.
    platform: Mapped[str | None] = mapped_column(String(16))

    # Fernet-шифрование: ссылка содержит пароль-секрет. Не логировать, не показывать сырыми.
    uri_enc: Mapped[bytes] = mapped_column()
    password_enc: Mapped[bytes] = mapped_column()

    status: Mapped[PeerStatus] = mapped_column(String(16), default=PeerStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Битовая маска отправленных предупреждений об истечении (как у Peer).
    expiry_warn_flags: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
