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
