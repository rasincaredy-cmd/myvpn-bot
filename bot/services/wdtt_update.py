"""Обновление программы резервного подключения на нодах — одной кнопкой.

Программа обхода живёт на каждой ноде отдельным файлом и обновляется только
руками: залить, перезапустить, проверить. Пока нод было две, это было терпимо;
с покупкой каждой новой страны разъезд версий становится вопросом времени, а
разъезд не виден вообще ниоткуда — снаружи «служба active» одинаково выглядит
и у свежей программы, и у полугодовалой.

Отсюда две части:
  • `probe` — что стоит на ноде: жива ли служба, отвечает ли управляющий сокет,
    какой отпечаток у программы и сколько на ней живых доступов;
  • `update` — залить эталон, перезапустить и УБЕДИТЬСЯ, что стало не хуже:
    сокет отвечает и доступы на месте. Не убедились — откат на бэкап.

Эталон — программа на ноде самого бота (`settings.wdtt_binary_path`): она же
раздаётся при установке нового сервера, и второго источника заводить нельзя,
иначе установка и обновление начнут ставить разное.

Перезапуск обрывает тех, кто прямо сейчас сидит через резервное подключение.
Пароли при этом не теряются — они лежат в /etc/wdtt и переживают рестарт, —
поэтому человеку достаточно переподключиться. Проверку «доступов столько же»
делаем всё равно: это единственный способ поймать программу, которая стартовала,
но потеряла базу.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from bot.config import settings
from bot.services.ssh import SSHClient, SSHError

# Сколько ждём, пока после рестарта поднимется управляющий сокет.
_READY_RETRIES = 8
_READY_DELAY = 2


@dataclass(frozen=True)
class Probe:
    """Что стоит на ноде прямо сейчас."""
    installed: bool
    active: bool
    socket_ok: bool
    sha256: str | None
    size: int
    accesses: int | None      # None — сокет не ответил, посчитать нечем

    @property
    def short(self) -> str:
        return (self.sha256 or "")[:8] or "—"


@dataclass(frozen=True)
class UpdateResult:
    """Итог обновления одной ноды. `changed` — программу действительно
    заменили; `rolled_back` — заменили и вернули обратно."""
    ok: bool
    changed: bool
    rolled_back: bool
    detail: str
    before: Probe | None = None
    after: Probe | None = None


def reference_sha256() -> str | None:
    """Отпечаток эталонной программы на ноде бота. None — файла нет."""
    path = Path(settings.wdtt_binary_path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _socket_answer(ssh: SSHClient, binary: str) -> tuple[bool, int | None]:
    """(отвечает ли управляющий сокет, сколько на нём доступов).

    Служба может быть active и при этом бесполезной: демон падает без активных
    паролей и поднимается заново по Restart=always. Поэтому спрашиваем сокет,
    а не systemd.
    """
    res = await ssh.run(f"{binary} ctl -op list", check=False, timeout=30)
    out = res.stdout.strip()
    if res.exit_code != 0 or '"ok":true' not in out.replace(" ", ""):
        return False, None
    try:
        import json

        data = json.loads(out.splitlines()[-1])
        return True, len(data.get("passwords", []))
    except (ValueError, IndexError):
        # Сокет ответил, но разобрать не вышло: считаем живым, число неизвестно.
        return True, None


async def probe(ssh: SSHClient, *, binary: str | None = None) -> Probe:
    """Снимок ноды: программа, служба, сокет, число доступов."""
    binary = binary or settings.wdtt_binary_path
    res = await ssh.run(
        f"test -f {binary} && sha256sum {binary} | cut -d' ' -f1 && "
        f"stat -c %s {binary} || echo MISSING",
        check=False,
        timeout=30,
    )
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if not lines or lines[0] == "MISSING":
        return Probe(False, False, False, None, 0, None)

    sha = lines[0]
    size = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else 0
    active = (await ssh.run("systemctl is-active wdtt", check=False)).stdout.strip() == "active"
    socket_ok, accesses = (await _socket_answer(ssh, binary)) if active else (False, None)
    return Probe(True, active, socket_ok, sha, size, accesses)


async def update(
    ssh: SSHClient,
    *,
    binary: str | None = None,
    force: bool = False,
    progress=None,
) -> UpdateResult:
    """Обновить программу обхода на ноде до эталонной. Идемпотентно.

    Порядок такой, чтобы в любой момент был путь назад:
      1. снимок «до» — с ним будем сравнивать результат;
      2. одинаковые отпечатки → не трогаем ничего (без `force`);
      3. бэкап рабочей программы рядом, с отпечатком в имени;
      4. заливка эталона и рестарт;
      5. проверка: сокет отвечает и доступов не меньше, чем было;
      6. не сошлось → возвращаем бэкап и рестартуем обратно.

    Шаг 5 — главный: «служба поднялась» ничего не доказывает, выдача доступов
    идёт через сокет, и молча онемевший сокет означает, что резервное
    подключение не работает ни у кого на этой ноде.

    Файл службы НЕ трогаем сознательно. В нём лежит пароль владельца (без
    активного пароля демон не стартует) и правила фаервола, дописанные на живых
    нодах руками, — перезапись службы стёрла бы и то и другое. Меняется только
    программа; пароли доступов лежат отдельно в /etc/wdtt и рестарт переживают.
    """
    binary = binary or settings.wdtt_binary_path
    ref = reference_sha256()
    if ref is None:
        return UpdateResult(
            False, False, False,
            f"на ноде бота нет эталона {binary} — обновлять нечем",
        )

    async def say(text: str) -> None:
        if progress is not None:
            await progress(text)

    before = await probe(ssh, binary=binary)
    if not before.installed:
        return UpdateResult(
            False, False, False,
            "программы на ноде нет — это установка, а не обновление",
            before=before,
        )
    if before.sha256 == ref and not force:
        return UpdateResult(True, False, False, "уже эталонная версия", before, before)

    backup = f"{binary}.bak-{before.short}"
    try:
        await say("Делаю бэкап...")
        await ssh.run(f"cp -f {binary} {backup}", check=True, timeout=60)

        await say("Заливаю новую версию...")
        # Во временный файл рядом: заливка поверх работающего файла на части
        # систем оканчивается «text file busy», и нода остаётся без программы.
        tmp = f"{binary}.new"
        await ssh.put_file(binary, tmp, mode=0o755)

        await say("Перезапускаю службу...")
        await ssh.run("systemctl stop wdtt", check=False, timeout=60)
        await ssh.run(f"mv -f {tmp} {binary}", check=True, timeout=60)
        await ssh.run("systemctl start wdtt", check=False, timeout=60)
    except SSHError as exc:
        logger.warning("wdtt update: сбой заливки: {}", exc)
        return UpdateResult(False, False, False, f"сбой заливки: {exc}", before=before)

    after = None
    for attempt in range(_READY_RETRIES):
        after = await probe(ssh, binary=binary)
        if after.socket_ok:
            break
        if attempt < _READY_RETRIES - 1:
            await asyncio.sleep(_READY_DELAY)

    lost = (
        after is not None
        and after.accesses is not None
        and before.accesses is not None
        and after.accesses < before.accesses
    )
    if after is not None and after.socket_ok and not lost:
        return UpdateResult(True, True, False, "обновлено", before, after)

    # Не взлетело — возвращаем то, что работало.
    reason = "сокет молчит" if after is None or not after.socket_ok else (
        f"доступов стало меньше ({before.accesses} → {after.accesses})"
    )
    await say(f"Не взлетело ({reason}) — откатываю...")
    try:
        await ssh.run("systemctl stop wdtt", check=False, timeout=60)
        await ssh.run(f"cp -f {backup} {binary}", check=True, timeout=60)
        await ssh.run("systemctl start wdtt", check=False, timeout=60)
    except SSHError as exc:
        logger.error("wdtt update: откат не удался: {}", exc)
        return UpdateResult(
            False, True, False,
            f"{reason}; ОТКАТ НЕ УДАЛСЯ: {exc} — бэкап лежит в {backup}",
            before, after,
        )

    restored = None
    for attempt in range(_READY_RETRIES):
        restored = await probe(ssh, binary=binary)
        if restored.socket_ok:
            break
        if attempt < _READY_RETRIES - 1:
            await asyncio.sleep(_READY_DELAY)

    detail = f"{reason} — откатил на прежнюю версию"
    if restored is None or not restored.socket_ok:
        detail = f"{reason}; после отката сокет тоже молчит — нужны руки"
    logger.warning("wdtt update: {}", detail)
    return UpdateResult(False, True, True, detail, before, restored)
