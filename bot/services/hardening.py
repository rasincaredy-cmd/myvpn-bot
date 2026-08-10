"""Приведение сервера к эталону безопасности.

Единственное место, которое знает, как доставить сценарий-эталон на
сервер, запустить его команды в нужном порядке и разобрать результат.
Сам сценарий живёт в репозитории (`scripts/hardening/harden.sh`) — копию
его текста в коде держать нельзя, копии разъезжаются.
"""
from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import asyncssh
from loguru import logger

from bot.services.ssh import SSHClient, SSHCredentials, SSHError

SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "hardening" / "harden.sh"
REMOTE_PATH = "/root/harden.sh"


@dataclass(frozen=True, slots=True)
class HardeningReport:
    compliant: bool
    ok: list[str]
    failed: list[str]
    raw: str


def parse_check(stdout: str, exit_code: int) -> HardeningReport:
    """Разобрать вывод `harden.sh check`.

    Ненулевой код возврата означает несоответствие даже если строк FAIL
    не видно: сценарий мог упасть раньше, чем начал печатать проверки.
    """
    ok: list[str] = []
    failed: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("OK "):
            ok.append(stripped[3:].strip())
        elif stripped.startswith("FAIL "):
            failed.append(stripped[5:].strip())
    return HardeningReport(
        compliant=exit_code == 0 and not failed,
        ok=ok,
        failed=failed,
        raw=stdout,
    )


async def upload(ssh: SSHClient) -> None:
    """Положить свежую копию сценария на сервер."""
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    await ssh.write_file(REMOTE_PATH, content, mode=0o700)


async def check(ssh: SSHClient) -> HardeningReport:
    """Проверить соответствие эталону. Ничего на сервере не меняет."""
    await upload(ssh)
    res = await ssh.run(f"{REMOTE_PATH} check")
    return parse_check(res.stdout, res.exit_code)


# Путь захардкожен под root — как и REMOTE_PATH выше: весь проект (и сам бот,
# и то, что он ставит на серверах) работает от root, второго пользователя нет.
KEY_PATH = "/root/.ssh/bot_server1"


async def ensure_bot_key(ssh: SSHClient, session, server_id: int) -> bool:
    """Завести боту собственный ключ и записать его в базу.

    Пароль намеренно не трогаем: он остаётся рабочим путём на сервер,
    пока вход по ключу не доказан. Гасит пароль отдельный шаг — и только
    после успеха этой функции.

    Доказательство входа — НЕ проверка "с сервера на самого себя" через
    127.0.0.1 (она ничего не говорит о реальном пути бота: внешний адрес,
    ufw, политики sshd в петлю не попадают), а отдельное подключение
    ровно так, как ходит сам бот — asyncssh, по адресу и порту из базы,
    строго ключом, без пароля в кредах вовсе.
    """
    from bot.db import repo
    from bot.services.crypto import encrypt

    server = await repo.get_server(session, server_id)
    if server is None:
        logger.warning("ensure_bot_key: сервер id={} не найден в базе", server_id)
        return False

    # 1. Ключ на сервере — генерируем, если ещё нет. /root/.ssh может не
    # существовать вовсе на свежем сервере (бот зашёл паролем, каталог
    # никто не создавал) — ssh-keygen сам родительскую директорию не
    # создаёт и молча падает "No such file or directory", поэтому каталог
    # готовим заранее, до генерации. Ошибку ssh-keygen не глушим:
    # check=True поднимет SSHError с внятным stderr.
    await ssh.run("mkdir -p /root/.ssh && chmod 700 /root/.ssh", check=True)
    gen = (
        f"[ -f {KEY_PATH} ] || ssh-keygen -t ed25519 -N '' "
        f"-C 'myvpn-bot' -f {KEY_PATH}"
    )
    await ssh.run(gen, check=True)
    await ssh.run(f"chmod 600 {KEY_PATH}", check=True)

    # 2. authorized_keys — устойчиво. Права выставляем явно, а не полагаемся
    # на umask (на боевом сервере sshd работает с StrictModes yes: при
    # umask 0000 файл ляжет 0666 и sshd молча проигнорирует все ключи в
    # нём, включая уже работающие чужие). Сравнение — по всей строке
    # (-qxF), а не по подстроке: иначе ключ-подстрока другого ключа даёт
    # ложное совпадение и не позволяет дописаться. Дозапись — через
    # printf с явными переводами строки по обе стороны: если в файле нет
    # финального \n, "cat >>" склеит ботовый ключ с последней строкой и
    # сломает оба — и ботовый, и тот, что был последним (на боевом сервере
    # это чужой ключ "termux" телефона Влада, ломать его нельзя).
    await ssh.run(
        "touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys",
        check=True,
    )
    pub = (await ssh.run(f"cat {KEY_PATH}.pub", check=True)).stdout.strip()
    if not pub:
        logger.warning(
            "ensure_bot_key: пустой публичный ключ на сервере id={}", server_id
        )
        return False
    quoted_pub = shlex.quote(pub)
    await ssh.run(
        f"grep -qxF -- {quoted_pub} /root/.ssh/authorized_keys "
        f"|| printf '\\n%s\\n' {quoted_pub} >> /root/.ssh/authorized_keys",
        check=True,
    )

    # 3. Приватная часть обязана реально парситься как ключ — иначе после
    # выключения пароля бот не сможет зайти вообще никак (и паролем уже
    # нельзя, и битым ключом тоже).
    private = await ssh.read_file(KEY_PATH)
    try:
        asyncssh.import_private_key(private)
    except (asyncssh.KeyImportError, ValueError) as exc:
        logger.warning(
            "ensure_bot_key: приватный ключ не распознан на сервере id={}: {}",
            server_id,
            exc,
        )
        return False

    # 4. Доказательство: отдельное подключение по реальным host/port/user
    # сервера, строго ключом. Пароль в креды не кладём вовсе — если бы
    # положили, asyncssh при отказе ключа молча откатился бы на пароль,
    # и проба соврала бы про то, что доказывает.
    creds = SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        private_key=private,
    )
    try:
        async with SSHClient(creds) as probe:
            result = await probe.run("echo ok")
    except (SSHError, OSError, asyncssh.Error) as exc:
        logger.warning(
            "ensure_bot_key: вход по ключу не подтверждён на сервере id={}: {}",
            server_id,
            exc,
        )
        return False
    if result.exit_code != 0 or result.stdout.strip() != "ok":
        logger.warning(
            "ensure_bot_key: вход по ключу не подтверждён на сервере id={}",
            server_id,
        )
        return False

    # 5. Ключ доказан — фиксируем в базе немедленно: следующим шагом
    # гасится пароль, и ключ обязан быть в базе ДО этого, а не
    # «когда-нибудь при закрытии сессии».
    server.ssh_key_enc = encrypt(private)
    server.ssh_key_passphrase_enc = None
    await session.commit()
    return True


async def harden(
    ssh: SSHClient,
    session,
    server_id: int,
    *,
    wg_port: int,
    progress,
    extra_ports: Sequence[str] = (),
) -> HardeningReport:
    """Привести сервер к эталону.

    Порядок шагов менять нельзя: фаервол включается после того, как порты
    уже слушают, ключ заводится до выключения пароля, пароль гасится
    последним. Любая перестановка оставляет сервер без доступа.

    `extra_ports` — порты вида `udp/56000`, которые обязаны остаться
    открытыми (обход БС). Передавать их можно ТОЛЬКО когда служба уже
    слушает: `apply-firewall` считает такой порт обязательным и вовсе
    откажется включать фаервол, если он молчит.
    """
    from bot.db import repo

    await upload(ssh)

    # Critical-1: отказ шага обязан дожить до ИТОГОВОГО отчёта, а не только
    # до строки прогресса, которую тут же затирает следующая. Итог строится
    # из `check`, а `check` видит лишь текущее состояние сервера — она не
    # знает, например, что настройка применилась, но самопроверка после неё
    # не прошла и на сервере остался вооружённый автооткат. Копим отказы
    # здесь и подмешиваем их в отчёт.
    steps_failed: list[str] = []

    async def run_step(command: str, title: str, note: str) -> bool:
        """Выполнить шаг и запомнить отказ. Возвращает успех шага."""
        res = await ssh.run(f"{REMOTE_PATH} {command}")
        if res.ok:
            return True
        steps_failed.append(note)
        logger.warning(
            "hardening: шаг {} не удался на сервере id={} (код {})",
            command,
            server_id,
            res.exit_code,
        )
        await progress(f"⚠️ {title}")
        return False

    await progress("Включаю сбор статистики...")
    await run_step("apply-stats", "Сбор статистики включить не удалось",
                   "сбор статистики включить не удалось")

    await progress("Ограничиваю размер журнала...")
    await run_step("apply-journal", "Потолок журнала поставить не удалось",
                   "потолок размера журнала поставить не удалось")

    await progress("Ставлю защиту от перебора паролей...")
    await run_step("apply-fail2ban", "Банилку перебора поднять не удалось",
                   "банилку перебора (fail2ban) поднять не удалось")

    # Important-2: порт SSH берём из данных сервера, а не из константы.
    # Мастер установки спрашивает порт у админа, и на сервере с ssh на 2222
    # обязательный `tcp/22` не слушает — сценарий откажется включать фаервол
    # вообще когда-либо, ни при установке, ни по кнопке.
    server = await repo.get_server(session, server_id)
    ssh_port = server.ssh_port if server is not None else 22

    await progress("Включаю фаервол...")
    required = f"tcp/{ssh_port} udp/{wg_port}"
    if extra_ports:
        required += " " + " ".join(extra_ports)
    await run_step(
        f"apply-firewall {required}",
        "Фаервол включить не удалось, остальное продолжаю",
        "фаервол включить не удалось",
    )

    await progress("Завожу ключ для бота...")
    if await ensure_bot_key(ssh, session, server_id):
        await progress("Выключаю вход по паролю...")
        if await run_step(
            f"disable-password {KEY_PATH}",
            "Вход по паролю выключить не удалось",
            "вход по паролю выключить не удалось",
        ):
            # Пароль больше не работает как способ входа — хранить его в базе
            # смысла нет, а утечь он может. Стираем только после
            # подтверждённого успеха.
            server = await repo.get_server(session, server_id)
            if server is not None:
                server.ssh_password_enc = None
                await session.commit()
    else:
        steps_failed.append("вход по ключу не подтверждён — пароль оставлен включённым")
        await progress("⚠️ Вход по ключу не подтверждён — пароль оставлен включённым")

    report = await check(ssh)
    if not steps_failed:
        return report
    # HardeningReport заморожен — собираем новый на основе того, что вернула
    # проверка, и снимаем «соответствует эталону»: хоть один шаг провалился.
    return HardeningReport(
        compliant=False,
        ok=report.ok,
        failed=[*report.failed, *steps_failed],
        raw=report.raw,
    )
