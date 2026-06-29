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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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

    owner_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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

    server: Mapped[Server] = relationship(back_populates="peers")
    user: Mapped[User] = relationship(back_populates="peers")


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
