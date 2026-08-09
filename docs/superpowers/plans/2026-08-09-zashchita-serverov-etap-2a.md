# Защита серверов, этап 2A: бот сам приводит серверы к эталону

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить бота самому приводить сервер к эталону безопасности: автоматически при добавлении нового сервера и по кнопке в админке для уже работающих.

**Architecture:** Новый сервис `bot/services/hardening.py` — единственное место, которое знает, как доставить сценарий-эталон на сервер, запустить его команды в правильном порядке и разобрать результат. Сценарий `scripts/hardening/harden.sh` уже написан и обкатан на боевом сервере (этап 1); здесь он получает правки под запуск без человека. Мастер установки вызывает сервис в конце, карточка сервера в админке — по кнопке.

**Tech Stack:** Python 3.11 (на сервере), aiogram, asyncssh, SQLAlchemy async, pytest.

## Global Constraints

- **Два окружения, интерпретаторы называются по-разному:** на телефоне (Termux, тут пишется код и гоняются тесты) — **`python`**; на сервере — **`/usr/bin/python3.11`** (команды `python` там нет). Тесты: `python -m pytest`.
- Ветка: создать `etap-2a-zashchita` от `main`. В `main` вливать только после приёмки.
- Боевой сервер: `31.77.157.162`, ssh-алиас `klopas`. **Он уже приведён к эталону** — `harden.sh check` даёт полное соответствие. Ничего на нём не ломать; проверки, меняющие состояние, запускать только там, где это явно указано.
- Сценарий-эталон в репозитории: `scripts/hardening/harden.sh`. Бот обязан брать его из репозитория, а не хранить копию текста в коде — иначе копии разъедутся.
- **Порядок шагов приведения к эталону менять нельзя:** сбор статистики → журнал → банилка → фаервол → ключ для бота → выключение пароля. Пароль гасится последним и только после того, как ключ записан в базу и подтверждён.
- `apply-firewall` принимает обязательные порты строго в формате `tcp/NNN` / `udp/NNN`. Голое число приведёт к отказу включать фаервол.
- Секреты (приватные ключи, пароли) не логировать и не показывать в сообщениях бота.
- В тестах импортировать `bot.*` можно прямо на верхнем уровне: `tests/conftest.py` выставляет `BOT_TOKEN`/`ENCRYPTION_KEY`/`DB_URL` до любых импортов. Возня с `sys.path` нужна только для `scripts.*`, для `bot.*` — нет.
- Формат вывода сценария зафиксирован: `ok()` печатает `OK   текст`, `fail()` — `FAIL текст`, итог — строка `ИТОГ: ...`. Разбор в коде опирается ровно на это.
- Комментарии и текст сообщений — на русском, как во всём проекте.
- Файл `bot/services/amnezia.py` уже 556 строк — новую логику класть в `bot/services/hardening.py`, а не наращивать его.

---

### Task 1: Умение записать файл на сервер

`SSHClient` умеет читать файл (`read_file`), но не умеет записывать. Сценарий-эталон занимает ~700 строк и содержит кавычки обоих видов — передавать его через `printf '%s' '...'`, как это делает `_write_server_conf`, ненадёжно.

**Files:**
- Modify: `bot/services/ssh.py`
- Test: `tests/test_ssh_write_file.py`

**Interfaces:**
- Produces: `SSHClient.write_file(path: str, content: str, *, mode: int = 0o600) -> None` — пишет файл через SFTP и выставляет права.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_ssh_write_file.py`:

```python
"""Запись файла на сервер идёт через SFTP, а не через кавычки в шелле."""
import inspect

from bot.services.ssh import SSHClient


def test_write_file_exists() -> None:
    assert hasattr(SSHClient, "write_file"), "нет метода записи файла"


def test_write_file_signature() -> None:
    sig = inspect.signature(SSHClient.write_file)
    assert "path" in sig.parameters
    assert "content" in sig.parameters
    assert "mode" in sig.parameters, "права на файл должны задаваться явно"


def test_write_file_uses_sftp() -> None:
    # Через шелл сценарий с кавычками обоих видов не передать надёжно.
    src = inspect.getsource(SSHClient.write_file)
    assert "sftp" in src.lower(), "запись должна идти через SFTP"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_ssh_write_file.py -v`
Expected: FAIL — `нет метода записи файла`.

- [ ] **Step 3: Добавить метод**

В `bot/services/ssh.py`, рядом с `read_file`, добавить:

```python
    async def write_file(self, path: str, content: str, *, mode: int = 0o600) -> None:
        """Записать файл на сервер через SFTP.

        Через шелл (`printf '%s' '...'`) большой текст с кавычками обоих
        видов передать надёжно нельзя — экранирование ломается. SFTP
        передаёт байты как есть.
        """
        if self._conn is None:
            raise SSHError("нет соединения")
        async with self._conn.start_sftp_client() as sftp:  # type: ignore[union-attr]
            async with sftp.open(path, "w") as f:
                await f.write(content)
        await self.run(f"chmod {mode:o} {path}", check=True)
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_ssh_write_file.py -v`
Expected: PASS, три теста.

- [ ] **Step 5: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/ssh.py tests/test_ssh_write_file.py
git commit -m "SSH: запись файла через SFTP

Сценарий-эталон ~700 строк с кавычками обоих видов — через шелл
передать надёжно нельзя.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Правки сценария под запуск без человека

Финальное ревью этапа 1 оставило три вещи, которые обязаны быть закрыты ДО того, как сценарий начнёт запускать бот.

**Files:**
- Modify: `scripts/hardening/harden.sh`
- Test: `tests/test_hardening.py`

**Interfaces:**
- Produces: сценарий, у которого `apply-firewall` корректно сверяет ограниченные по адресу правила, `apply-fail2ban` ставит нужную зависимость и не падает от гонки при старте джейла, а `plan` не показывает приватно-привязанные порты как «останутся открытыми наружу».

- [ ] **Step 1: Написать падающие страж-тесты**

Добавить в конец `tests/test_hardening.py`:

```python
def test_firewall_verifies_restricted_rules(text: str) -> None:
    """N2: сверка после включения не должна отвергать собственные
    ограниченные правила.

    Порт, слушающий на приватном адресе, получает правило вида
    `ufw allow from 10.8.0.0/24 to 10.8.0.1 port 53` — в выводе `ufw status`
    такая строка начинается с адреса назначения, а не с `53/udp`. Сверка,
    привязанная к началу строки, не найдёт правило, решит что порт не
    открыт, вернёт ошибку и ОСТАВИТ автооткат вооружённым — через 10 минут
    фаервол выключится на корректно настроенном сервере.
    """
    assert "rule_present" in text


def test_fail2ban_installs_systemd_backend_dependency(text: str) -> None:
    # N3: джейл с backend=systemd не стартует без python3-systemd, и
    # apply-fail2ban честно возвращает ошибку — то есть автоматика встанет
    # на ровном месте. Зависимость надо ставить вместе с fail2ban.
    assert "python3-systemd" in text


def test_fail2ban_waits_for_jail(text: str) -> None:
    # N3: опрос джейла сразу после старта на нагруженной машине даёт
    # ложный отказ. Нужен повтор, а не единственная попытка после sleep.
    assert "jail_ready" in text


def test_plan_marks_private_bound_ports(text: str) -> None:
    # Команда plan существует ради одного — показать, что изменится.
    # Приватно-привязанные порты apply-firewall наружу НЕ откроет, значит
    # показывать их в списке «останутся открытыми наружу» — враньё.
    assert "только изнутри" in text
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening.py -v`
Expected: четыре новых теста FAIL, остальные PASS.

- [ ] **Step 3: Починить сверку правил (N2)**

В `scripts/hardening/harden.sh`, в `cmd_apply_firewall`, заменить проверку присутствия порта в выводе `ufw status` на функцию, которая понимает обе формы правила. Добавить рядом с другими вспомогательными функциями:

```bash
# Правило может быть записано двумя способами: обычное начинается с
# "NNN/proto", а ограниченное по адресу — с адреса назначения, и порт
# стоит дальше в строке. Сверка, привязанная к началу строки, второе
# не находит и объявляет корректно открытый порт закрытым.
rule_present() {
  local status="$1" num="$2" proto="$3"
  grep -qE "(^|[[:space:]])${num}/${proto}([[:space:]]|$)" <<<"$status" && return 0
  grep -qE "[[:space:]]${num}([[:space:]]|/${proto}[[:space:]])" <<<"$status" && return 0
  return 1
}
```

и использовать `rule_present "$ufw_after" "$num" "$proto"` вместо прежней проверки.

- [ ] **Step 4: Починить банилку (N3)**

В `cmd_apply_fail2ban` при установке пакета ставить и зависимость systemd-бэкенда, а ожидание джейла сделать с повтором:

```bash
    DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban python3-systemd >/dev/null 2>&1 \
      || { fail "не удалось установить fail2ban"; return 1; }
```

```bash
# Джейл поднимается не мгновенно, и на нагруженной машине единственная
# попытка сразу после старта даёт ложный отказ.
jail_ready() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    fail2ban-client status sshd >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
```

и вызывать `jail_ready` вместо `sleep 3` с одиночной проверкой.

- [ ] **Step 5: Починить вывод plan**

В `cmd_plan` порты, слушающие на приватных адресах, показывать отдельной пометкой, а не в списке «останутся открытыми наружу»: для таких строк дописывать `— только изнутри` и не считать их открываемыми.

- [ ] **Step 6: Проверить синтаксис и тесты**

Run: `cd /root/myvpn-bot && bash -n scripts/hardening/harden.sh && python -m pytest tests/test_hardening.py -v`
Expected: синтаксис чистый, все тесты PASS.

- [ ] **Step 7: Прогнать на боевом сервере (только чтение)**

Run:
```bash
scp /root/myvpn-bot/scripts/hardening/harden.sh klopas:/root/harden.sh
ssh klopas '/root/harden.sh check; echo "код: $?"'
ssh klopas '/root/harden.sh plan'
```
Expected: `check` по-прежнему даёт полное соответствие и код `0`; в `plan` приватно-привязанных портов на этом сервере нет, список тот же, что и раньше.
**Если `check` перестал быть зелёным — остановиться, это регресс.**

- [ ] **Step 8: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/hardening/harden.sh tests/test_hardening.py
git commit -m "Эталон: правки под запуск без человека

Сверка правил понимает ограниченные по адресу разрешения (иначе
корректный сервер объявлялся бы незащищённым и автооткат выключал бы
фаервол). Банилка ставит зависимость systemd-бэкенда и ждёт джейл с
повтором. plan больше не выдаёт приватно-привязанные порты за открытые.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Сервис приведения к эталону

Единственное место, которое знает, как доставить сценарий и разобрать его вывод.

**Files:**
- Create: `bot/services/hardening.py`
- Test: `tests/test_hardening_service.py`

**Interfaces:**
- Consumes: `SSHClient.write_file` (задача 1), `SSHClient.run`.
- Produces:
  - `SCRIPT_PATH: Path` — путь к `scripts/hardening/harden.sh` в репозитории;
  - `REMOTE_PATH = "/root/harden.sh"`;
  - `@dataclass(frozen=True, slots=True) HardeningReport` с полями `compliant: bool`, `ok: list[str]`, `failed: list[str]`, `raw: str`;
  - `parse_check(stdout: str, exit_code: int) -> HardeningReport`;
  - `async upload(ssh: SSHClient) -> None`;
  - `async check(ssh: SSHClient) -> HardeningReport`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_hardening_service.py`:

```python
"""Разбор вывода сценария-эталона."""
from bot.services.hardening import SCRIPT_PATH, parse_check


def test_script_is_taken_from_repo() -> None:
    # Копия текста сценария в коде разъедется с самим сценарием.
    assert SCRIPT_PATH.is_file(), "сценарий-эталон не найден в репозитории"
    assert SCRIPT_PATH.name == "harden.sh"


def test_parse_all_green() -> None:
    out = (
        "=== проверка соответствия эталону ===\n"
        "собственный адрес: 1.2.3.4\n"
        "OK   вход по паролю выключен\n"
        "OK   фаервол включён\n"
        "\n"
        "ИТОГ: сервер соответствует эталону\n"
    )
    report = parse_check(out, 0)
    assert report.compliant is True
    assert report.failed == []
    assert "вход по паролю выключен" in report.ok


def test_parse_with_failures() -> None:
    out = (
        "OK   фаервол включён\n"
        "FAIL вход по паролю РАЗРЕШЁН\n"
        "FAIL банилки перебора нет\n"
        "ИТОГ: есть несоответствия (см. FAIL выше)\n"
    )
    report = parse_check(out, 1)
    assert report.compliant is False
    assert report.failed == ["вход по паролю РАЗРЕШЁН", "банилки перебора нет"]


def test_nonzero_exit_is_not_compliant_even_without_fail_lines() -> None:
    # Сценарий мог упасть до печати проверок — молча считать это
    # соответствием нельзя.
    report = parse_check("что-то пошло не так\n", 2)
    assert report.compliant is False
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_service.py -v`
Expected: FAIL — `ModuleNotFoundError: bot.services.hardening`.

- [ ] **Step 3: Написать сервис**

Создать `bot/services/hardening.py`:

```python
"""Приведение сервера к эталону безопасности.

Единственное место, которое знает, как доставить сценарий-эталон на
сервер, запустить его команды в нужном порядке и разобрать результат.
Сам сценарий живёт в репозитории (`scripts/hardening/harden.sh`) — копию
его текста в коде держать нельзя, копии разъезжаются.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from bot.services.ssh import SSHClient, SSHError

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
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_service.py -v`
Expected: PASS, четыре теста.

- [ ] **Step 5: Проверить на боевом сервере, что разбор совпадает с реальностью**

Run:
```bash
scp /root/myvpn-bot/bot/services/hardening.py klopas:/root/myvpn-bot/bot/services/hardening.py
ssh -n klopas 'cd /root/myvpn-bot && /usr/bin/python3.11 - <<PY
import asyncio, sys
sys.path.insert(0, ".")
from bot.db import repo
from bot.db.base import session_scope
from bot.services.hardening import check
from bot.services.ssh import SSHClient

async def main():
    async with session_scope() as s:
        servers = await repo.list_all_servers(s)
        server = servers[0]
        creds = repo.creds_from_server(server)
    async with SSHClient(creds) as ssh:
        report = await check(ssh)
    print("соответствует:", report.compliant)
    print("зелёных:", len(report.ok), "красных:", len(report.failed))
    print("красные:", report.failed)

asyncio.run(main())
PY'
```
Креды берутся боевой сборкой (`repo.creds_from_server`) — проверяется ровно тот
путь, которым ходит бот. `ssh -n` обязателен: без него вложенный `ssh` съедает
остаток heredoc.
Expected: `соответствует: True`, красных `0`, зелёных больше десяти.

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/hardening.py tests/test_hardening_service.py
git commit -m "Сервис: доставка сценария-эталона и разбор его проверки

Сценарий берётся из репозитория, а не хранится копией в коде.
Ненулевой код возврата — несоответствие, даже если строк FAIL не видно:
сценарий мог упасть раньше, чем начал печатать.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Заведение ключа для бота

Чтобы бот мог выключить пароль на новом сервере, у него должен появиться собственный ключ — сгенерированный на сервере, проверенный и записанный в базу. Порядок в точности как в этапе 1: пароль гасится только после того, как вход по ключу доказан.

**Files:**
- Modify: `bot/services/hardening.py`
- Test: `tests/test_hardening_service.py`

**Interfaces:**
- Consumes: `bot.services.crypto.encrypt`, `bot.db.repo.get_server`.
- Produces: `async ensure_bot_key(ssh: SSHClient, session, server_id: int) -> bool` — генерирует ключ (если его ещё нет), кладёт в `authorized_keys`, проверяет вход по нему отдельным подключением, записывает приватную часть в `servers.ssh_key_enc` и зануляет `ssh_key_passphrase_enc`. Возвращает `True`, если после вызова бот заходит по ключу. Пароль в базе НЕ трогает.
- Produces: `KEY_PATH = "/root/.ssh/bot_server1"`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_hardening_service.py`:

```python
def test_ensure_bot_key_does_not_touch_password() -> None:
    # Пароль — единственный путь на сервер, пока ключ не доказан.
    # Его стирание на этом шаге лишает возможности откатиться.
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "ssh_password_enc" not in src, (
        "пароль нельзя трогать до подтверждённого входа по ключу"
    )


def test_ensure_bot_key_clears_stale_passphrase() -> None:
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "ssh_key_passphrase_enc" in src, (
        "фраза-пароль от старого ключа должна зануляться"
    )


def test_ensure_bot_key_commits_before_returning() -> None:
    """Ключ обязан лежать в базе ДО того, как гасится пароль.

    Следующим шагом `harden` выключает вход по паролю. Если ключ к этому
    моменту записан только в память сессии и бот упадёт (или сессия
    откатится) — пароль уже выключен, а ключа в базе нет: сервер потерян
    для бота навсегда.
    """
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "commit" in src, "ключ не фиксируется в базе до выключения пароля"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_service.py -v`
Expected: FAIL — `cannot import name 'ensure_bot_key'`.

- [ ] **Step 3: Написать функцию**

Добавить в `bot/services/hardening.py`:

```python
KEY_PATH = "/root/.ssh/bot_server1"


async def ensure_bot_key(ssh: SSHClient, session, server_id: int) -> bool:
    """Завести боту собственный ключ и записать его в базу.

    Пароль намеренно не трогаем: он остаётся рабочим путём на сервер,
    пока вход по ключу не доказан. Гасит пароль отдельный шаг — и только
    после успеха этой функции.
    """
    from bot.db import repo
    from bot.services.crypto import encrypt

    gen = (
        f"[ -f {KEY_PATH} ] || ssh-keygen -t ed25519 -N '' "
        f"-C 'myvpn-bot' -f {KEY_PATH} >/dev/null 2>&1"
    )
    await ssh.run(gen, check=True)
    await ssh.run(f"chmod 600 {KEY_PATH}", check=True)
    await ssh.run(
        f"grep -qF -- \"$(cat {KEY_PATH}.pub)\" /root/.ssh/authorized_keys "
        f"|| cat {KEY_PATH}.pub >> /root/.ssh/authorized_keys",
        check=True,
    )

    # Доказательство: отдельное подключение строго по ключу, пароль запрещён.
    probe = await ssh.run(
        f"ssh -n -i {KEY_PATH} -o StrictHostKeyChecking=no "
        f"-o PasswordAuthentication=no -o BatchMode=yes -o ConnectTimeout=10 "
        f"root@127.0.0.1 'echo ok'"
    )
    if "ok" not in probe.stdout:
        logger.warning("Вход по ключу не подтверждён на сервере id={}", server_id)
        return False

    private = await ssh.read_file(KEY_PATH)
    server = await repo.get_server(session, server_id)
    if server is None:
        return False
    server.ssh_key_enc = encrypt(private)
    server.ssh_key_passphrase_enc = None
    # Фиксируем немедленно: следующим шагом гасится пароль, и ключ обязан
    # быть в базе ДО этого, а не «когда-нибудь при закрытии сессии».
    await session.commit()
    return True
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_service.py -v`
Expected: PASS, семь тестов.

- [ ] **Step 5: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/hardening.py tests/test_hardening_service.py
git commit -m "Сервис: заведение ключа для бота

Ключ генерируется на сервере, вход по нему доказывается отдельным
подключением, и только потом приватная часть уходит в базу. Пароль
не трогается — он единственный путь назад, пока ключ не подтверждён.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Полное приведение к эталону

Оркестровка: порядок шагов, обработка отказов, понятный отчёт.

**Files:**
- Modify: `bot/services/hardening.py`
- Test: `tests/test_hardening_service.py`

**Interfaces:**
- Consumes: `upload`, `check`, `ensure_bot_key`.
- Produces: `async harden(ssh: SSHClient, session, server_id: int, *, wg_port: int, progress) -> HardeningReport`. `progress` — та же асинхронная функция-уведомление, что в мастере установки: `async def progress(text: str) -> None`. Порядок шагов: `apply-stats` → `apply-journal` → `apply-fail2ban` → `apply-firewall tcp/22 udp/<wg_port>` → `ensure_bot_key` → `disable-password`. Возвращает итоговый отчёт `check`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_hardening_service.py`:

```python
def test_harden_order_password_last() -> None:
    """Порядок шагов — это и есть безопасность.

    Выключение пароля обязано идти после заведения ключа, а фаервол —
    после того, как нужные порты уже разрешены. Перестановка любого из
    этих шагов оставляет сервер без доступа.
    """
    import inspect

    from bot.services.hardening import harden

    src = inspect.getsource(harden)
    order = [
        src.index("apply-stats"),
        src.index("apply-journal"),
        src.index("apply-fail2ban"),
        src.index("apply-firewall"),
        src.index("ensure_bot_key"),
        src.index("disable-password"),
    ]
    assert order == sorted(order), "порядок шагов приведения к эталону нарушен"


def test_harden_passes_wg_port_in_required_format() -> None:
    # apply-firewall принимает строго tcp/NNN и udp/NNN; голое число
    # приведёт к отказу включать фаервол.
    import inspect

    from bot.services.hardening import harden

    src = inspect.getsource(harden)
    assert "udp/{" in src or 'udp/' in src
    assert "tcp/22" in src
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_service.py -v`
Expected: FAIL — `cannot import name 'harden'`.

- [ ] **Step 3: Написать оркестровку**

Добавить в `bot/services/hardening.py`:

```python
async def harden(
    ssh: SSHClient,
    session,
    server_id: int,
    *,
    wg_port: int,
    progress,
) -> HardeningReport:
    """Привести сервер к эталону.

    Порядок шагов менять нельзя: фаервол включается после того, как порты
    уже слушают, ключ заводится до выключения пароля, пароль гасится
    последним. Любая перестановка оставляет сервер без доступа.
    """
    from bot.db import repo

    await upload(ssh)

    await progress("Включаю сбор статистики...")
    await ssh.run(f"{REMOTE_PATH} apply-stats")

    await progress("Ограничиваю размер журнала...")
    await ssh.run(f"{REMOTE_PATH} apply-journal")

    await progress("Ставлю защиту от перебора паролей...")
    await ssh.run(f"{REMOTE_PATH} apply-fail2ban")

    await progress("Включаю фаервол...")
    fw = await ssh.run(f"{REMOTE_PATH} apply-firewall tcp/22 udp/{wg_port}")
    if not fw.ok:
        await progress("⚠️ Фаервол включить не удалось, остальное продолжаю")
        logger.warning("apply-firewall failed on server id={}: {}", server_id, fw.stdout)

    await progress("Завожу ключ для бота...")
    if await ensure_bot_key(ssh, session, server_id):
        await progress("Выключаю вход по паролю...")
        off = await ssh.run(f"{REMOTE_PATH} disable-password {KEY_PATH}")
        if off.ok:
            # Пароль больше не работает как способ входа — хранить его в базе
            # смысла нет, а утечь он может. Стираем только после
            # подтверждённого успеха.
            server = await repo.get_server(session, server_id)
            if server is not None:
                server.ssh_password_enc = None
                await session.commit()
        else:
            await progress("⚠️ Вход по паролю выключить не удалось")
            logger.warning("disable-password failed on server id={}", server_id)
    else:
        await progress("⚠️ Вход по ключу не подтверждён — пароль оставлен включённым")

    return await check(ssh)
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_service.py -v`
Expected: PASS, девять тестов.

- [ ] **Step 5: Коммит**

```bash
cd /root/myvpn-bot
git add bot/services/hardening.py tests/test_hardening_service.py
git commit -m "Сервис: полное приведение сервера к эталону

Порядок шагов зафиксирован тестом: фаервол после того, как порты
слушают; ключ до выключения пароля; пароль последним. Отказ отдельного
шага не обрывает остальные, но честно попадает в отчёт.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Кнопка «Защита» в карточке сервера

**Files:**
- Modify: `bot/keyboards/inline/servers.py` (функция `server_card`, строка 34)
- Modify: `bot/handlers/servers/card.py`
- Test: `tests/test_hardening_button.py`

**Interfaces:**
- Consumes: `bot.services.hardening.check`, `bot.services.hardening.harden`.
- Produces: кнопка `🛡 Защита` с `callback_data=f"{CB_SERVERS}:harden:{server_id}"`, экран с результатом проверки и кнопкой `🔧 Привести в порядок` (`{CB_SERVERS}:hardenrun:{server_id}`).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_hardening_button.py`:

```python
"""Кнопка защиты в карточке сервера."""
from bot.keyboards.inline.servers import server_card


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_has_protection_button() -> None:
    markup = server_card(1)
    assert any("Защита" in t for t in _texts(markup))


def test_protection_button_leads_to_check_not_apply() -> None:
    # Первое нажатие обязано только ПОКАЗАТЬ состояние. Применение —
    # отдельным подтверждением: случайный тык не должен трогать сервер.
    markup = server_card(1)
    datas = [d for d in _datas(markup) if d and "harden" in d]
    assert datas, "нет кнопки защиты"
    assert all("hardenrun" not in d for d in datas), (
        "из карточки нельзя сразу применять — только проверка"
    )
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_button.py -v`
Expected: FAIL — кнопки нет.

- [ ] **Step 3: Добавить кнопку в клавиатуру**

В `bot/keyboards/inline/servers.py`, в функцию `server_card`, **между тумблером
«🔒 Приватный» и «🗑 Удалить»** добавить:

```python
    kb.button(text="🛡 Защита", callback_data=f"{CB_SERVERS}:harden:{server_id}")
```

Место выбрано не случайно: раскладка задана `kb.adjust(2, 2, 2, 2, 1, 1, 1, 1)` —
первые восемь кнопок стоят парами, дальше по одной. Вставка в середину пар
разорвала бы их и перетасовала всю карточку; в хвосте, где кнопки идут по одной,
новая просто становится своей строкой. `adjust` менять не нужно.

- [ ] **Step 4: Добавить обработчики**

В `bot/handlers/servers/card.py` добавить два обработчика — показ состояния и применение.

Проверка прав отдельно не нужна: `AdminFilter` висит на родительском роутере
(`bot/handlers/servers/__init__.py`, `router.callback_query.filter(AdminFilter())`),
так что оба обработчика уже закрыты от посторонних.

```python
@router.callback_query(F.data.startswith(f"{CB_SERVERS}:harden:"))
async def cb_server_harden(call: CallbackQuery, session: AsyncSession) -> None:
    """Показать, соответствует ли сервер эталону. Ничего не меняет."""
    server_id = int(call.data.split(":")[2])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return

    await call.answer("Проверяю...")
    creds = repo.creds_from_server(server)
    try:
        async with SSHClient(creds) as ssh:
            report = await hardening.check(ssh)
    except (SSHError, OSError) as exc:
        await call.message.edit_text(
            f"🛡 <b>Защита сервера</b>\n\nНе удалось подключиться: {exc}",
            reply_markup=back_to_servers_kb(),
        )
        return

    if report.compliant:
        text = "🛡 <b>Защита сервера</b>\n\nСервер соответствует эталону."
    else:
        problems = "\n".join(f"• {p}" for p in report.failed)
        text = (
            "🛡 <b>Защита сервера</b>\n\n"
            f"Найдено несоответствий: {len(report.failed)}\n\n{problems}"
        )

    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB

    kb = IKB()
    if not report.compliant:
        kb.button(
            text="🔧 Привести в порядок",
            callback_data=f"{CB_SERVERS}:hardenrun:{server_id}",
        )
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith(f"{CB_SERVERS}:hardenrun:"))
async def cb_server_harden_run(call: CallbackQuery, session: AsyncSession) -> None:
    """Привести сервер к эталону. Меняет состояние сервера."""
    server_id = int(call.data.split(":")[2])
    server = await repo.get_server(session, server_id)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return

    await call.answer("Работаю, это займёт пару минут")
    msg = await call.message.edit_text("🛡 Привожу сервер в порядок...")

    async def progress(text: str) -> None:
        try:
            await msg.edit_text(f"🛡 {text}")
        except Exception:  # noqa: BLE001 — «сообщение не изменилось» не важно
            pass

    creds = repo.creds_from_server(server)
    try:
        async with SSHClient(creds) as ssh:
            report = await hardening.harden(
                ssh, session, server_id, wg_port=server.wg_port, progress=progress
            )
    except (SSHError, OSError) as exc:
        await msg.edit_text(
            f"🛡 <b>Защита сервера</b>\n\nСорвалось: {exc}",
            reply_markup=back_to_servers_kb(),
        )
        return

    if report.compliant:
        text = "🛡 <b>Готово</b>\n\nСервер соответствует эталону."
    else:
        problems = "\n".join(f"• {p}" for p in report.failed)
        text = (
            "🛡 <b>Частично</b>\n\nОсталось несоответствий: "
            f"{len(report.failed)}\n\n{problems}"
        )
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB

    kb = IKB()
    kb.button(text="« К серверу", callback_data=f"{CB_SERVERS}:open:{server_id}")
    await msg.edit_text(text, reply_markup=kb.as_markup())
```

Про импорты: `back_to_servers_kb`, `SSHClient`, `SSHError`, `repo` и `CB_SERVERS`
в этом файле уже есть. Строитель клавиатур в нём везде импортируется локально,
внутри функции, как `IKB` — держимся той же манеры. Добавить в начало файла надо
ровно одну строку, к существующему `from bot.services import amnezia`:

```python
from bot.services import amnezia, hardening
```

- [ ] **Step 5: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening_button.py -v`
Expected: PASS, два теста.

- [ ] **Step 6: Прогнать весь набор — обработчики регистрируются при импорте**

Run: `cd /root/myvpn-bot && python -m pytest -q --tb=short 2>&1 | tail -5`
Expected: падений не больше, чем было (два предсуществующих в `tests/test_qrgen.py` из-за отсутствия PIL).

- [ ] **Step 7: Коммит**

```bash
cd /root/myvpn-bot
git add bot/keyboards/inline/servers.py bot/handlers/servers/card.py tests/test_hardening_button.py
git commit -m "Админка: кнопка «Защита» в карточке сервера

Первое нажатие только показывает состояние — применение отдельным
подтверждением, чтобы случайный тык не менял боевой сервер.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Новый сервер защищается сам

**Files:**
- Modify: `bot/handlers/install.py` (шаг запуска установки, функция `step_run`, строка 280)
- Test: `tests/test_install_hardens.py`

**Interfaces:**
- Consumes: `bot.services.hardening.harden`.
- Produces: после успешной установки AmneziaWG мастер вызывает `harden(...)` и дописывает результат в итоговое сообщение.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_install_hardens.py`:

```python
"""Новый сервер должен защищаться сам, без отдельной кнопки."""
import inspect

from bot.handlers import install


def test_install_calls_hardening() -> None:
    src = inspect.getsource(install)
    assert "hardening" in src, (
        "мастер установки не приводит новый сервер к эталону — "
        "каждый новый сервер поднимался бы с открытым паролем"
    )


def test_hardening_runs_after_vpn_is_up() -> None:
    """Порядок важен: фаервол строится от слушающих портов.

    Если привести сервер к эталону до подъёма VPN, порт VPN не будет
    слушать в этот момент и не попадёт в правила — сервер поднимется с
    VPN, к которому нельзя подключиться.
    """
    src = inspect.getsource(install)
    assert src.index("install_amneziawg") < src.index("hardening.harden")
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_install_hardens.py -v`
Expected: FAIL — `hardening` в мастере не упоминается.

- [ ] **Step 3: Встроить вызов в мастер**

В `bot/handlers/install.py`, в `step_run`, **после `await session.commit()`,
которым сервер переводится в `READY`** (строка ~357), и до отправки итогового
сообщения, добавить:

```python
    # Новый сервер обязан подниматься уже защищённым: пароль выключен,
    # фаервол включён, банилка работает. Вызывается ПОСЛЕ подъёма VPN —
    # фаервол строится от портов, которые реально слушают, и порт VPN
    # должен уже слушать к этому моменту.
    #
    # Отдельное подключение — не оплошность: блок `async with SSHClient`
    # выше уже закрыт, а внутри `harden` бот заводит себе ключ и гасит
    # пароль, так что тянуть старое соединение через полминуты работы
    # смысла нет.
    await progress("Привожу сервер к эталону безопасности...")
    try:
        async with SSHClient(creds) as ssh:
            report = await hardening.harden(
                ssh, session, server.id, wg_port=data["wg_port"], progress=progress
            )
        if report.compliant:
            security_line = "🛡 Защита: сервер соответствует эталону"
        else:
            security_line = (
                "⚠️ Защита: осталось несоответствий — "
                f"{len(report.failed)} (см. кнопку «🛡 Защита» в карточке)"
            )
    except Exception as exc:  # noqa: BLE001 — сервер уже установлен, о проблеме сообщаем
        logger.warning("Приведение к эталону сорвалось: {}", exc)
        security_line = "⚠️ Защита: не удалась, проверь кнопкой «🛡 Защита»"
```

Ловим `Exception` целиком осознанно: VPN на этот момент уже установлен и
работает, и провал защиты не должен превращать успешную установку в
«не удалось». Админ увидит строку про защиту и починит кнопкой.

Итоговое сообщение об успехе дописать этой строкой:

```python
    await bot.send_message(
        chat_id,
        t.install_done.format(name=server.name) + f"\n\n{security_line}",
        reply_markup=main_menu(user.is_admin),
    )
```

Импорт: в начало файла, к существующему `from bot.services import amnezia` —
`from bot.services import amnezia, hardening`.

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_install_hardens.py -v`
Expected: PASS, два теста.

- [ ] **Step 5: Прогнать весь набор**

Run: `cd /root/myvpn-bot && python -m pytest -q --tb=short 2>&1 | tail -5`
Expected: падений не больше прежнего.

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add bot/handlers/install.py tests/test_install_hardens.py
git commit -m "Установка: новый сервер приводится к эталону автоматически

Вызывается после подъёма VPN — фаервол строится от портов, которые
реально слушают. Провал приведения не отменяет установку, но честно
попадает в итоговое сообщение.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Приёмка на боевом сервере

**Files:**
- Modify: `docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md`

- [ ] **Step 1: Задеплоить на боевой сервер**

Run:
```bash
ssh klopas 'git -C /root/myvpn-bot pull --ff-only && systemctl restart myvpn-bot && sleep 5 && systemctl is-active myvpn-bot'
```
Expected: `active`.
**Примечание:** ветку сначала влить в `main` и запушить (пуш делает Влад командой `! git -C /root/myvpn-bot push origin main`).

- [ ] **Step 2: Проверить кнопку в живом боте**

Открыть в боте карточку сервера → «🛡 Защита».
Expected: сообщение «Сервер соответствует эталону», кнопки «Привести в порядок» нет (нечего чинить).

- [ ] **Step 3: Убедиться, что бот жив и без ошибок**

Run: `ssh klopas 'journalctl -u myvpn-bot --since "-10min" --no-pager | grep -ciE "error|traceback"'`
Expected: `0`.

- [ ] **Step 4: Отметить в спеке**

Дописать в раздел «Статус» спеки `docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md`:

```markdown
Этап 2A (бот сам приводит серверы к эталону) выполнен: новый сервер
защищается в мастере установки, работающий — кнопкой «🛡 Защита» в карточке.
Осталось 2B — тревоги в телеграм.
```

- [ ] **Step 5: Коммит**

```bash
cd /root/myvpn-bot
git add docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md
git commit -m "Спека: этап 2A выполнен

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Что НЕ входит в этот план

Этап 2B, отдельным планом: тревоги в телеграм по порогам из спеки (упавший сервис, диск меньше 15%, сосед выше 20% дольше 10 минут, память на исходе, рост потерь, бот или админ в бане), сбор VPN-специфики поверх sysstat и защита от спама тревогами.

Отложено сознательно (из финального ревью этапа 1): проверка фаервола сверяется с записями ufw, а не с правилами ядра. Закрывать имеет смысл вместе с тревогами — там появится регулярный внешний взгляд на состояние сервера.
