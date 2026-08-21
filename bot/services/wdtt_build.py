"""Пересборка серверной части резервного подключения прямо на ноде бота.

Серверная часть — чужой проект (qWDTT) плюс ОДИН наш файл: управляющий канал,
через который бот выдаёт доступы. Раз чужой код обновляется, нам нужно уметь
брать свежий и накладывать на него нашу правку — иначе «не отстанем» держится
на том, что кто-то не поленился собрать всё руками у себя на телефоне.

Порядок: скачать их дерево → положить наш файл рядом → вписать две строки в их
`main.go` → собрать → убедиться, что собранное вообще запускается и наш канал
внутри → только потом сделать его эталоном (с бэкапом прежнего).

Наш файл лежит В РЕПОЗИТОРИИ (`server-patch/control.go`), а не на диске ноды:
до 21.08.2026 единственный экземпляр правки жил в одной папке на одной машине,
и потеря этой машины означала бы, что пересобрать нечего.

Две строки в чужой `main.go` ищутся по якорям. Не нашлись — сборка ОТМЕНЯЕТСЯ с
понятным текстом: значит, они переписали запуск, и правку надо переносить
руками. Молча собрать сервер без управляющего канала нельзя — бот перестанет
выдавать доступы, а выяснится это на первом же покупателе.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from loguru import logger

from bot.config import settings

REPO = "SpaceNeuroX/proxy-turn-vk-android"
SOURCE_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/master"

GO_BIN = Path("/usr/local/go/bin/go")
PATCH_FILE = Path(__file__).resolve().parent.parent.parent / "server-patch" / "control.go"

# Сборка идёт на живой ноде, где рядом крутятся бот и VPN, поэтому в самый
# низкий приоритет и в один поток: пусть лучше соберётся на минуту дольше.
_NICE = ["nice", "-n", "19"]
_BUILD_TIMEOUT = 900
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=180)


class PatchError(RuntimeError):
    """Чужой main.go изменился — наши якоря не нашлись."""


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    detail: str
    sha256: str = ""
    path: str = ""


# Якоря в чужом main.go. Первый — самое начало main(), второй — место после
# инициализации базы и WireGuard, где уже есть wgDev и ctx.
_ANCHOR_MAIN = "func main() {"
_ANCHOR_GOROUTINES = "\tgo statsLoop(ctx, *configDir)"

_CTL_DISPATCH = '''func main() {
\t// Правка myvpn-bot: режим клиента управляющего сокета —
\t// `wdtt-server ctl -op add|remove|unbind|list ...`. Обрабатываем ДО
\t// объявления серверных флагов: это отдельный вход, сервер не стартует.
\tif len(os.Args) > 1 && os.Args[1] == "ctl" {
\t\tos.Exit(runCtlClient(os.Args[2:]))
\t}
'''

_CTL_SERVE = '''\t// Правка myvpn-bot: управляющий сокет для провижнинга доступов.
\tgo serveControl(ctx, wgDev)

\tgo statsLoop(ctx, *configDir)'''


def apply_patch(main_go: str) -> str:
    """Вписывает в чужой main.go две наши строки. Идемпотентна.

    Чистая функция: её можно проверить тестом без сети, компилятора и сервера —
    а именно она ломается, когда чужой проект переписывают.
    """
    if "runCtlClient" in main_go and "serveControl" in main_go:
        return main_go  # уже пропатчен (собираем из ранее подготовленного дерева)

    if _ANCHOR_MAIN not in main_go:
        raise PatchError("в их main.go не нашлось начала main() — правку надо переносить руками")
    if _ANCHOR_GOROUTINES not in main_go:
        raise PatchError(
            "в их main.go не нашлось запуска фоновых задач — правку надо переносить руками"
        )
    patched = main_go.replace(_ANCHOR_MAIN, _CTL_DISPATCH, 1)
    patched = patched.replace(_ANCHOR_GOROUTINES, _CTL_SERVE, 1)
    return patched


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _download(dest: Path) -> None:
    async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as http:
        async with http.get(SOURCE_URL) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    f.write(chunk)


def _extract(archive: Path, into: Path) -> Path:
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(into, filter="data")
        except TypeError:  # питон без фильтров распаковки
            tar.extractall(into)
    roots = [p for p in into.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("в архиве не одна корневая папка — на это мы не рассчитывали")
    return roots[0]


async def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "не дождались завершения"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


async def _looks_alive(binary: Path) -> bool:
    """Собранное обязано запускаться и содержать НАШ управляющий канал.

    Проверяем режимом клиента: на ноде бота рядом живой сервер, и ответ придёт
    JSON-ом; на машине без сервера — жалобой на сокет. Оба ответа доказывают,
    что бинарь исполняется и наша правка в нём есть. Чужая сборка без правки
    ответила бы разбором флагов, а не этим.
    """
    code, out = await _run([str(binary), "ctl", "-op", "list"], binary.parent, 60)
    return out.lstrip().startswith("{") or "ctl:" in out


async def build(progress=None) -> BuildResult:
    """Скачать свежие исходники, наложить нашу правку и собрать.

    Эталон НЕ трогает: собрать и поставить — разные шаги, между ними человек
    смотрит на результат. Собранное кладётся рядом с эталоном и ждёт.
    """

    async def say(text: str) -> None:
        if progress is not None:
            await progress(text)

    if not GO_BIN.is_file():
        return BuildResult(False, f"на ноде нет компилятора ({GO_BIN})")
    if not PATCH_FILE.is_file():
        return BuildResult(False, f"в репозитории нет нашей правки ({PATCH_FILE.name})")

    workdir = Path(tempfile.mkdtemp(prefix="qwdtt-build-"))
    try:
        await say("Скачиваю исходники...")
        archive = workdir / "src.tar.gz"
        try:
            await _download(archive)
        except Exception as exc:
            return BuildResult(False, f"не скачались исходники: {exc}")

        tree = _extract(archive, workdir)
        server_dir = tree / "server"
        main_go = server_dir / "main.go"
        if not main_go.is_file():
            return BuildResult(False, "в их дереве нет server/main.go — проект перестроили")

        await say("Накладываю нашу правку...")
        shutil.copy2(PATCH_FILE, server_dir / "control.go")
        try:
            main_go.write_text(apply_patch(main_go.read_text(encoding="utf-8")), encoding="utf-8")
        except PatchError as exc:
            return BuildResult(False, str(exc))

        await say("Собираю (это займёт пару минут)...")
        out_binary = workdir / "wdtt-server"
        code, log = await _run(
            _NICE + [
                str(GO_BIN), "build", "-p", "1", "-trimpath",
                "-ldflags", "-s -w", "-o", str(out_binary), "./server",
            ],
            cwd=tree, timeout=_BUILD_TIMEOUT,
        )
        if code != 0 or not out_binary.is_file():
            logger.warning("wdtt build: сборка не прошла: {}", log[-2000:])
            return BuildResult(False, f"сборка не прошла: {log.strip()[-300:] or 'без вывода'}")

        if not await _looks_alive(out_binary):
            return BuildResult(False, "собранное не отвечает как наш сервер — не беру")

        # Готовое кладём рядом с эталоном, а не в /tmp: /tmp вычищается, а
        # между сборкой и заменой эталона проходит нажатие кнопки.
        keep = Path(settings.wdtt_binary_path).with_suffix(".built")
        shutil.copy2(out_binary, keep)
        keep.chmod(0o755)
        sha = _sha256(keep)
        logger.info("wdtt build: собрано {}", sha[:8])
        return BuildResult(True, "собрано", sha, str(keep))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def promote(built_path: str, progress=None) -> BuildResult:
    """Сделать собранное эталоном и перезапустить сервер НА НОДЕ БОТА.

    Перезапуск здесь обязателен, и вот почему. Эталон и рабочая программа ноды
    бота — это один и тот же файл: подменив его, мы получили бы ноду, которая в
    списке версий выглядит свежей (файл-то новый), а в памяти держит старый
    сервер до ближайшей перезагрузки. Кнопка обновления её бы не тронула —
    отпечатки совпадают.

    Контракт тот же, что у обновления чужих нод: не ответил управляющий сокет —
    возвращаем прежнюю программу из бэкапа и поднимаем её обратно.
    """

    async def say(text: str) -> None:
        if progress is not None:
            await progress(text)

    built = Path(built_path)
    reference = Path(settings.wdtt_binary_path)
    if not built.is_file():
        return BuildResult(False, "собранного файла уже нет — собери заново")

    backup = reference.with_name(reference.name + f".bak-{_sha256(reference)[:8]}") \
        if reference.is_file() else None
    if backup is not None:
        shutil.copy2(reference, backup)

    await say("Ставлю новую версию на ноде бота...")
    await _run(["systemctl", "stop", "wdtt"], reference.parent, 60)
    shutil.copy2(built, reference)
    reference.chmod(0o755)
    await _run(["systemctl", "start", "wdtt"], reference.parent, 60)

    for _ in range(8):
        await asyncio.sleep(2)
        if await _looks_alive(reference):
            sha = _sha256(reference)
            logger.info("wdtt build: эталон обновлён до {}", sha[:8])
            return BuildResult(True, "эталон обновлён, нода бота перезапущена", sha, str(reference))

    if backup is None:
        return BuildResult(False, "сокет молчит, а бэкапа нет — нужны руки")

    await say("Не взлетело — возвращаю прежнюю...")
    await _run(["systemctl", "stop", "wdtt"], reference.parent, 60)
    shutil.copy2(backup, reference)
    reference.chmod(0o755)
    await _run(["systemctl", "start", "wdtt"], reference.parent, 60)
    return BuildResult(False, "новая сборка не ожила — откатил ноду бота на прежнюю")
