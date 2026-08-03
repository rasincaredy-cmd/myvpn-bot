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
