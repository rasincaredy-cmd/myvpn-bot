# Защита серверов, этап 1: сценарий-эталон и закрытие боевого сервера

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Написать идемпотентный сценарий-эталон и привести им боевой сервер `31.77.157.162` к безопасному состоянию, не потеряв доступ и не уронив выдачу конфигов клиентам.

**Architecture:** Один самодостаточный shell-сценарий `scripts/hardening/harden.sh` с подкомандами `check` (проверить соответствие эталону, ничего не меняя), `plan` (показать, что изменится) и отдельными командами применения по частям. Самодостаточность — обязательное свойство: на этапе 2 бот будет заливать этот файл на сервер одним куском. Роль тестов играют два механизма: подкоманда `check` (падает до применения, проходит после — TDD на уровне инфраструктуры) и pytest-страж, проверяющий, что из сценария не исчезли критичные защиты.

**Tech Stack:** bash, Ubuntu 22.04 (systemd 249), ufw, fail2ban, systemd-journald, sysstat (уже стоит), pytest + asyncssh (для скриптов на стороне бота).

## Global Constraints

- Тесты запускаются **`python -m pytest`** (termux-питон). `python3` в этом окружении **без** `cryptography` — им тесты не запускать.
- Спека: `docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md`.
- Ветка: `docs/zashchita-serverov` (уже существует, в ней лежит спека). В `main` ничего не вливать — идёт согласование в банке.
- Боевой сервер: `31.77.157.162`, ssh-алиас `klopas`, садится в `/root` → для git на сервере использовать `git -C`.
- Прод-база бота: `/root/myvpn-bot/data/vpn_bot.sqlite3` на сервере. Читать только `sqlite3 -readonly`, кроме задачи 4, где запись делается скриптом с бэкапом.
- **Белый список для банилки (обязателен всегда):** собственный внешний адрес сервера, `10.8.0.0/24`, `10.66.66.0/24`. Бот ходит по SSH сам на себя через внешний адрес — без этого банилка забанит бота и остановит выдачу конфигов.
- **`DEFAULT_FORWARD_POLICY="ACCEPT"` в `/etc/default/ufw` обязателен** — по умолчанию ufw дропает форвард, и VPN подключается, но интернета у клиентов нет.
- Порты, открытые наружу на 08.08.2026: tcp/22, udp/585, udp/56000, udp/56001, tcp/8443, tcp/2096, tcp/16044, tcp/20476, tcp/6769 (панель x-ui — закрыть от интернета, оставить из VPN).
- Каждый опасный шаг (задачи 5 и 7) обязан ставить автооткат через `systemd-run --on-active=10min` **до** внесения изменения.
- Пароль от SSH выключается только после доказанного входа по ключу — порядок из спеки не переставлять.

---

### Task 1: Сценарий-эталон, режим проверки

Создаёт сценарий и его подкоманду `check`. Ничего на сервере не меняет — безопасно запускать на боевом. На этом шаге `check` должен показать, что сервер эталону **не** соответствует.

**Files:**
- Create: `scripts/hardening/harden.sh`
- Test: `tests/test_hardening.py`

**Interfaces:**
- Produces: `scripts/hardening/harden.sh` с подкомандами `check` и `plan` (остальные — `apply-journal`, `apply-fail2ban`, `apply-firewall`, `disable-password`, `rollback-cancel` — добавляются задачами 3, 5, 6, 7). Код возврата `check`: `0` — сервер соответствует эталону, `1` — есть несоответствия. Каждая строка вывода `check` начинается с `OK ` или `FAIL `. Функции, на которые опираются поздние задачи: `listening_ports`, `ok`, `fail`; константы `VPN_SUBNET`, `BYPASS_SUBNET`, `PANEL_PORT`, `JOURNAL_CAP`, `OWN_IP`.

- [ ] **Step 1: Написать страж-тест**

Создать `tests/test_hardening.py`:

```python
"""Страж: из сценария-эталона не должны исчезнуть критичные защиты.

Каждая проверка здесь стоит за конкретной аварией, которая случается,
если соответствующий кусок сценария потерять.
"""
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hardening" / "harden.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists() -> None:
    assert SCRIPT.is_file(), "сценарий-эталон пропал"


def test_forward_policy_accept(text: str) -> None:
    # Без этого ufw дропает форвард: VPN подключается, интернета у клиента нет.
    assert 'DEFAULT_FORWARD_POLICY="ACCEPT"' in text


def test_whitelist_has_vpn_subnets(text: str) -> None:
    # Без белого списка банилка забанит самого бота (он ходит по SSH сам на себя).
    assert "10.8.0.0/24" in text
    assert "10.66.66.0/24" in text


def test_whitelist_has_own_external_ip(text: str) -> None:
    # Собственный адрес сервера вычисляется, а не хардкодится.
    assert "OWN_IP" in text
```

Остальные страж-проверки (автооткат, порядок «ключ до пароля», панель только
из VPN) добавляются в задачах 5 и 7 — вместе с кодом, который они охраняют.
Писать их здесь нельзя: они бы падали не потому, что что-то сломано,
а потому что этих кусков сценария ещё нет.

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening.py -v`
Expected: FAIL — `test_script_exists` падает, файла нет.

- [ ] **Step 3: Написать сценарий**

Создать `scripts/hardening/harden.sh` (режим `check` + каркас; остальные команды наполняются в задачах 5–7):

```bash
#!/usr/bin/env bash
# Эталон безопасного сервера проекта. Идемпотентен: повторный запуск
# приводит сервер к нужному состоянию и ничего не ломает.
#
#   harden.sh check     — проверить соответствие эталону, ничего не менять
#   harden.sh plan      — показать, что изменится
#
# Спека: docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md
set -uo pipefail

VPN_SUBNET="10.8.0.0/24"
BYPASS_SUBNET="10.66.66.0/24"
PANEL_PORT=6769
JOURNAL_CAP="500M"

# Собственный внешний адрес: бот ходит по SSH сам на себя через него.
OWN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
[ -z "$OWN_IP" ] && OWN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

FAILED=0
ok()   { echo "OK   $*"; }
fail() { echo "FAIL $*"; FAILED=1; }

# Порты, которые реально слушают наружу. Фаервол строится от них,
# а не от списка из головы — иначе легко забыть нужный и отрезать сервис.
listening_ports() {
  ss -tulnH 2>/dev/null | awk '
    $5 !~ /^127\./ && $5 !~ /^\[::1\]/ {
      n = split($5, a, ":"); port = a[n]
      if (port ~ /^[0-9]+$/) print ($1 == "udp" ? "udp/" : "tcp/") port
    }' | sort -u
}

check_password_off() {
  if sshd -T 2>/dev/null | grep -qx "passwordauthentication no"; then
    ok "вход по паролю выключен"
  else
    fail "вход по паролю РАЗРЕШЁН"
  fi
}

check_fail2ban() {
  if systemctl is-active --quiet fail2ban; then
    ok "банилка перебора работает"
  else
    fail "банилки перебора нет"
  fi
  if [ -f /etc/fail2ban/jail.local ] && grep -q "$VPN_SUBNET" /etc/fail2ban/jail.local; then
    ok "белый список банилки на месте"
  else
    fail "в белом списке банилки нет подсети VPN — забанит бота"
  fi
}

check_firewall() {
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    ok "фаервол включён"
  else
    fail "фаервол выключен"
  fi
  if grep -q '^DEFAULT_FORWARD_POLICY="ACCEPT"' /etc/default/ufw 2>/dev/null; then
    ok "форвард разрешён (VPN будет маршрутизировать)"
  else
    fail "форвард НЕ разрешён — у клиентов не будет интернета"
  fi
  if ufw status 2>/dev/null | grep -q "${PANEL_PORT}.*ALLOW.*Anywhere"; then
    fail "панель x-ui открыта всему интернету"
  else
    ok "панель x-ui не открыта наружу"
  fi
}

check_journal() {
  if grep -qE "^SystemMaxUse=${JOURNAL_CAP}" /etc/systemd/journald.conf 2>/dev/null; then
    ok "у журнала есть потолок ${JOURNAL_CAP}"
  else
    fail "у журнала нет потолка — растёт без ограничения"
  fi
}

check_stats() {
  if systemctl is-active --quiet sysstat-collect.timer; then
    ok "сбор статистики работает"
  else
    fail "сбор статистики не работает"
  fi
}

cmd_check() {
  echo "=== проверка соответствия эталону ==="
  echo "собственный адрес: ${OWN_IP:-НЕ ОПРЕДЕЛЁН}"
  check_password_off
  check_fail2ban
  check_firewall
  check_journal
  check_stats
  echo
  if [ "$FAILED" -eq 0 ]; then
    echo "ИТОГ: сервер соответствует эталону"
  else
    echo "ИТОГ: есть несоответствия (см. FAIL выше)"
  fi
  return "$FAILED"
}

cmd_plan() {
  echo "=== что будет сделано (ничего не меняется) ==="
  echo "белый список банилки: ${OWN_IP} ${VPN_SUBNET} ${BYPASS_SUBNET}"
  echo "останутся открытыми наружу порты:"
  listening_ports | grep -v "tcp/${PANEL_PORT}" | sed 's/^/  /'
  echo "будет закрыт от интернета и разрешён только из VPN:"
  echo "  tcp/${PANEL_PORT} (панель x-ui)"
  echo "потолок журнала: ${JOURNAL_CAP} (сейчас $(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[MG]' | tail -1))"
}

case "${1:-}" in
  check) cmd_check ;;
  plan)  cmd_plan ;;
  *) echo "использование: $0 {check|plan}" >&2; exit 2 ;;
esac
```

- [ ] **Step 4: Сделать исполняемым и проверить синтаксис**

Run: `cd /root/myvpn-bot && chmod +x scripts/hardening/harden.sh && bash -n scripts/hardening/harden.sh && echo "синтаксис ок"`
Expected: `синтаксис ок`

- [ ] **Step 5: Запустить страж-тест**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening.py -v`
Expected: PASS, все 4 теста.

- [ ] **Step 6: Прогнать check на боевом сервере — он должен показать несоответствия**

Run:
```bash
scp /root/myvpn-bot/scripts/hardening/harden.sh klopas:/root/harden.sh
ssh klopas 'chmod +x /root/harden.sh && /root/harden.sh check; echo "код возврата: $?"'
```
Expected: несколько строк `FAIL` (пароль разрешён, банилки нет, фаервол выключен, у журнала нет потолка), `OK` у сбора статистики, код возврата `1`.

- [ ] **Step 7: Прогнать plan и глазами проверить список портов**

Run: `ssh klopas '/root/harden.sh plan'`
Expected: в списке остающихся портов присутствуют tcp/22, udp/585, udp/56000, udp/56001, tcp/8443, tcp/2096, tcp/16044, tcp/20476; tcp/6769 — в строке про VPN.
**Если какого-то порта не хватает или появился незнакомый — остановиться и разобраться, дальше не идти.**

- [ ] **Step 8: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/hardening/harden.sh tests/test_hardening.py
git commit -m "Сценарий-эталон: проверка соответствия и сухой прогон

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Проверка, что бот может зайти на сервер

Скрипт берёт креды из базы ровно так же, как это делает бот, и пробует подключиться. Нужен, чтобы после смены пароля на ключ убедиться в работоспособности **бота**, а не только своего ssh.

**Files:**
- Create: `scripts/check_server_ssh.py`
- Test: `tests/test_check_server_ssh.py`

**Interfaces:**
- Consumes: `bot.db.repo.servers`, `bot.services.crypto.decrypt`, `bot.services.ssh.SSHClient`, `SSHCredentials` (существуют).
- Produces: `scripts/check_server_ssh.py`, запуск `python scripts/check_server_ssh.py <server_id>`; печатает `OK <host>: вход по <ключу|паролю>` и выходит с `0`, либо печатает ошибку и выходит с `1`. Функция `build_credentials(server) -> SSHCredentials` — используется тестом.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_check_server_ssh.py`:

```python
"""Скрипт проверки должен собирать креды так же, как бот."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_server_ssh import build_credentials


def test_prefers_key_over_password(monkeypatch) -> None:
    import scripts.check_server_ssh as mod

    monkeypatch.setattr(mod, "decrypt", lambda blob: None if blob is None else blob.decode())
    server = SimpleNamespace(
        host="10.0.0.1", ssh_port=22, ssh_user="root",
        ssh_password_enc=b"pwd", ssh_key_enc=b"KEY", ssh_key_passphrase_enc=None,
    )
    creds = build_credentials(server)
    assert creds.private_key == "KEY"
    assert creds.password is None, "при наличии ключа пароль слаться не должен"


def test_falls_back_to_password(monkeypatch) -> None:
    import scripts.check_server_ssh as mod

    monkeypatch.setattr(mod, "decrypt", lambda blob: None if blob is None else blob.decode())
    server = SimpleNamespace(
        host="10.0.0.1", ssh_port=22, ssh_user="root",
        ssh_password_enc=b"pwd", ssh_key_enc=None, ssh_key_passphrase_enc=None,
    )
    creds = build_credentials(server)
    assert creds.password == "pwd"
    assert creds.private_key is None
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_check_server_ssh.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.check_server_ssh`.

- [ ] **Step 3: Написать скрипт**

Создать `scripts/check_server_ssh.py`:

```python
"""Проверка, что бот может зайти на сервер его же кредами.

    python scripts/check_server_ssh.py <server_id>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from bot.db.base import async_session_maker
from bot.db.models import Server
from bot.services.crypto import decrypt
from bot.services.ssh import SSHClient, SSHCredentials, SSHError


def build_credentials(server) -> SSHCredentials:
    """Ключ приоритетнее пароля — так же, как это делает бот."""
    key = decrypt(server.ssh_key_enc)
    password = decrypt(server.ssh_password_enc)
    return SSHCredentials(
        host=server.host,
        port=server.ssh_port,
        username=server.ssh_user,
        password=None if key else password,
        private_key=key,
        key_passphrase=decrypt(server.ssh_key_passphrase_enc),
    )


async def main(server_id: int) -> int:
    async with async_session_maker() as session:
        server = (
            await session.execute(select(Server).where(Server.id == server_id))
        ).scalar_one_or_none()
    if server is None:
        print(f"сервера с id={server_id} нет в базе")
        return 1

    creds = build_credentials(server)
    how = "ключу" if creds.private_key else "паролю"
    try:
        async with SSHClient(creds) as ssh:
            result = await ssh.run("echo alive")
    except (SSHError, OSError) as exc:
        print(f"ОШИБКА {server.host}: не удалось подключиться по {how}: {exc}")
        return 1
    if not result.ok or "alive" not in result.stdout:
        print(f"ОШИБКА {server.host}: подключились, но команда не отработала")
        return 1
    print(f"OK {server.host}: вход по {how}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("использование: python scripts/check_server_ssh.py <server_id>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(int(sys.argv[1]))))
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_check_server_ssh.py -v`
Expected: PASS, оба теста.

- [ ] **Step 5: Проверить на боевом сервере, что бот сейчас ходит паролем**

Run:
```bash
ssh klopas 'cd /root/myvpn-bot && python scripts/check_server_ssh.py 1'
```
Expected: `OK 31.77.157.162: вход по паролю`

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/check_server_ssh.py tests/test_check_server_ssh.py
git commit -m "Скрипт проверки: может ли бот зайти на сервер своими кредами

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Потолок журнала

Самый безопасный шаг — делаем первым из применяющих, заодно уменьшаем шум в логах. Ничем не рискует: журнал не влияет на доступ.

**Files:**
- Modify: `scripts/hardening/harden.sh` (добавить команду `apply-journal`)

**Interfaces:**
- Consumes: константа `JOURNAL_CAP` из задачи 1.
- Produces: подкоманда `apply-journal`.

- [ ] **Step 1: Убедиться, что check по журналу сейчас падает**

Run: `ssh klopas '/root/harden.sh check 2>&1 | grep -i журнал'`
Expected: `FAIL у журнала нет потолка — растёт без ограничения`

- [ ] **Step 2: Добавить команду в сценарий**

В `scripts/hardening/harden.sh` перед блоком `case` добавить:

```bash
cmd_apply_journal() {
  echo "=== потолок журнала ${JOURNAL_CAP} ==="
  local conf=/etc/systemd/journald.conf
  cp -n "$conf" "${conf}.bak" 2>/dev/null || true
  if grep -qE "^#?SystemMaxUse=" "$conf"; then
    sed -i "s/^#\?SystemMaxUse=.*/SystemMaxUse=${JOURNAL_CAP}/" "$conf"
  else
    printf '\nSystemMaxUse=%s\n' "$JOURNAL_CAP" >> "$conf"
  fi
  systemctl restart systemd-journald
  journalctl --vacuum-size="$JOURNAL_CAP" 2>&1 | tail -2
  echo "стало: $(journalctl --disk-usage 2>/dev/null)"
}
```

В блоке `case` добавить строку: `apply-journal) cmd_apply_journal ;;`
и обновить строку использования: `{check|plan|apply-journal}`.

- [ ] **Step 3: Проверить синтаксис и страж**

Run: `cd /root/myvpn-bot && bash -n scripts/hardening/harden.sh && python -m pytest tests/test_hardening.py -q`
Expected: синтаксис без ошибок, тесты PASS.

- [ ] **Step 4: Применить на сервере**

Run:
```bash
scp /root/myvpn-bot/scripts/hardening/harden.sh klopas:/root/harden.sh
ssh klopas 'chmod +x /root/harden.sh && /root/harden.sh apply-journal'
```
Expected: журнал ужимается, итоговый размер меньше 500 МБ (было 3,9 ГБ).

- [ ] **Step 5: Проверить, что check по журналу стал зелёным**

Run: `ssh klopas '/root/harden.sh check 2>&1 | grep -i журнал'`
Expected: `OK   у журнала есть потолок 500M`

- [ ] **Step 6: Убедиться, что сервисы живы**

Run: `ssh klopas 'systemctl is-active myvpn-bot wdtt x-ui mtproto8443'`
Expected: четыре строки `active`.

- [ ] **Step 7: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/hardening/harden.sh
git commit -m "Эталон: потолок журнала 500M

Journald рос без ограничения и занял 3,9 ГБ — почти половину диска.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Ключ для бота вместо пароля

Самая опасная задача. Ключ кладётся на сервер и **в базу бота**, пароль при этом ещё работает. Выключение пароля — следующая задача, отдельно.

**Files:**
- Create: `scripts/set_server_key.py`
- Test: `tests/test_set_server_key.py`

**Interfaces:**
- Consumes: `bot.services.crypto.encrypt`, `bot.db.repo.servers`.
- Produces: `scripts/set_server_key.py`, запуск `python scripts/set_server_key.py <server_id> <путь_к_приватному_ключу>`; шифрует ключ, пишет в `servers.ssh_key_enc`, **пароль не трогает** (его чистит задача 5 после доказанного входа по ключу).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_set_server_key.py`:

```python
"""Ключ должен ложиться в базу зашифрованным, пароль остаётся на месте."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.set_server_key import prepare_update


def test_key_is_encrypted_and_password_untouched(monkeypatch) -> None:
    import scripts.set_server_key as mod

    monkeypatch.setattr(mod, "encrypt", lambda text: b"ENC:" + text.encode())
    update = prepare_update("PRIVATE-KEY-BODY")
    assert update["ssh_key_enc"] == b"ENC:PRIVATE-KEY-BODY"
    assert "ssh_password_enc" not in update, (
        "пароль нельзя стирать здесь — сначала надо доказать вход по ключу"
    )


def test_empty_key_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        prepare_update("   ")
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_set_server_key.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.set_server_key`.

- [ ] **Step 3: Написать скрипт**

Создать `scripts/set_server_key.py`:

```python
"""Положить приватный ssh-ключ сервера в базу бота (зашифрованным).

    python scripts/set_server_key.py <server_id> <путь_к_приватному_ключу>

Пароль намеренно НЕ трогается: он остаётся рабочим, пока вход по ключу
не доказан. Гасит пароль отдельный шаг плана.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import update as sa_update

from bot.db.base import async_session_maker
from bot.db.models import Server
from bot.services.crypto import encrypt


def prepare_update(private_key: str) -> dict:
    if not private_key or not private_key.strip():
        raise ValueError("пустой ключ")
    return {"ssh_key_enc": encrypt(private_key)}


async def main(server_id: int, key_path: Path) -> int:
    private_key = key_path.read_text(encoding="utf-8")
    values = prepare_update(private_key)
    async with async_session_maker() as session:
        await session.execute(
            sa_update(Server).where(Server.id == server_id).values(**values)
        )
        await session.commit()
    print(f"ключ записан для сервера id={server_id}; пароль оставлен как был")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("использование: python scripts/set_server_key.py <server_id> <путь_к_ключу>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2]))))
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_set_server_key.py -v`
Expected: PASS, оба теста.

- [ ] **Step 5: Сделать бэкап базы перед записью**

Run:
```bash
ssh klopas 'cp /root/myvpn-bot/data/vpn_bot.sqlite3 /root/vpn_bot.pre-hardening.sqlite3 && ls -lh /root/vpn_bot.pre-hardening.sqlite3'
```
Expected: файл создан, размер ненулевой.

- [ ] **Step 6: Сгенерировать ключ и положить публичную часть на сервер**

Run:
```bash
ssh klopas 'ssh-keygen -t ed25519 -N "" -C "myvpn-bot@server1" -f /root/.ssh/bot_server1 <<< y >/dev/null 2>&1; \
  grep -qF "$(cat /root/.ssh/bot_server1.pub)" /root/.ssh/authorized_keys || cat /root/.ssh/bot_server1.pub >> /root/.ssh/authorized_keys; \
  wc -l < /root/.ssh/authorized_keys'
```
Expected: число ключей в `authorized_keys` увеличилось на 1 (было 2 → стало 3).

- [ ] **Step 7: Доказать, что вход по этому ключу работает**

Run:
```bash
ssh klopas 'ssh -i /root/.ssh/bot_server1 -o StrictHostKeyChecking=no -o PasswordAuthentication=no -o BatchMode=yes root@127.0.0.1 "echo вход-по-ключу-работает"'
```
Expected: `вход-по-ключу-работает`
**Если не сработало — остановиться. Дальше идти нельзя.**

- [ ] **Step 8: Записать ключ в базу бота**

Run:
```bash
ssh klopas 'cd /root/myvpn-bot && python scripts/set_server_key.py 1 /root/.ssh/bot_server1'
```
Expected: `ключ записан для сервера id=1; пароль оставлен как был`

- [ ] **Step 9: Проверить, что бот теперь ходит именно по ключу**

Run: `ssh klopas 'cd /root/myvpn-bot && python scripts/check_server_ssh.py 1'`
Expected: `OK 31.77.157.162: вход по ключу`
**Если написало «вход по паролю» или ошибку — остановиться и чинить, пароль пока не выключать.**

- [ ] **Step 10: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/set_server_key.py tests/test_set_server_key.py
git commit -m "Скрипт: положить ssh-ключ сервера в базу бота

Пароль намеренно не трогается — он гасится отдельно, после того
как вход по ключу доказан.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Выключение входа по паролю с автооткатом

**Files:**
- Modify: `scripts/hardening/harden.sh` (добавить `verify_key_login`, `disable-password`, `rollback-cancel`)

**Interfaces:**
- Produces: подкоманды `disable-password` (ставит автооткат на 10 минут, проверяет вход по ключу, гасит пароль) и `rollback-cancel` (отменяет автооткат после успешной проверки).

- [ ] **Step 1: Дописать страж-тесты (сначала падают)**

Добавить в конец `tests/test_hardening.py`:

```python
def test_dangerous_steps_have_rollback(text: str) -> None:
    # Автооткат ставится ДО изменения, иначе потеря доступа необратима.
    assert "systemd-run" in text
    assert "--on-active=10min" in text


def test_password_disabled_only_after_key_check(text: str) -> None:
    # Порядок из спеки: сначала доказать вход по ключу, потом гасить пароль.
    key_check = text.index("verify_key_login")
    disable = text.index("PasswordAuthentication no")
    assert key_check < disable, "выключение пароля стоит раньше проверки ключа"
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening.py -v`
Expected: два новых теста FAIL (`systemd-run` и `verify_key_login` в сценарии ещё нет), четыре прежних PASS.

- [ ] **Step 3: Добавить код в сценарий**

В `scripts/hardening/harden.sh` перед блоком `case` добавить:

```bash
# Автооткат: сервер сам вернёт настройки, если через 10 минут
# никто не подтвердил, что доступ жив.
arm_rollback() {
  local unit="$1" cmd="$2"
  systemctl stop "${unit}.timer" 2>/dev/null || true
  systemd-run --on-active=10min --unit="$unit" \
    /bin/bash -c "$cmd" >/dev/null 2>&1
  echo "автооткат вооружён: ${unit} сработает через 10 минут"
}

cmd_rollback_cancel() {
  local n=0
  for unit in rollback-sshd rollback-ufw; do
    if systemctl stop "${unit}.timer" 2>/dev/null; then
      systemctl reset-failed "$unit" 2>/dev/null || true
      echo "автооткат отменён: ${unit}"
      n=$((n+1))
    fi
  done
  [ "$n" -eq 0 ] && echo "активных автооткатов не было"
}

# Доказать вход по ключу ДО того, как гасить пароль.
verify_key_login() {
  local key="${1:-/root/.ssh/bot_server1}"
  [ -f "$key" ] || { echo "нет файла ключа $key"; return 1; }
  ssh -i "$key" -o StrictHostKeyChecking=no -o PasswordAuthentication=no \
      -o BatchMode=yes -o ConnectTimeout=10 root@127.0.0.1 'echo ok' 2>/dev/null \
      | grep -qx ok
}

cmd_disable_password() {
  echo "=== выключение входа по паролю ==="
  if ! verify_key_login "${1:-}"; then
    echo "ОТМЕНА: вход по ключу не работает, пароль оставлен включённым"
    return 1
  fi
  echo "вход по ключу подтверждён"

  cp -n /etc/ssh/sshd_config /etc/ssh/sshd_config.bak 2>/dev/null || true
  arm_rollback rollback-sshd \
    "cp /etc/ssh/sshd_config.bak /etc/ssh/sshd_config; systemctl restart ssh"

  local drop=/etc/ssh/sshd_config.d/99-hardening.conf
  mkdir -p /etc/ssh/sshd_config.d
  cat > "$drop" <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
  if ! sshd -t 2>&1; then
    echo "ОТМЕНА: конфиг sshd невалиден, откатываю"
    rm -f "$drop"
    cmd_rollback_cancel
    return 1
  fi
  systemctl restart ssh
  echo "пароль выключен. Проверь вход в НОВОЙ сессии и вызови: $0 rollback-cancel"
}
```

В блоке `case` добавить:
```
  disable-password) shift; cmd_disable_password "${1:-}" ;;
  rollback-cancel)  cmd_rollback_cancel ;;
```
и обновить строку использования.

- [ ] **Step 4: Проверить синтаксис и страж**

Run: `cd /root/myvpn-bot && bash -n scripts/hardening/harden.sh && python -m pytest tests/test_hardening.py -q`
Expected: синтаксис ок; страж PASS, включая `test_password_disabled_only_after_key_check`.

- [ ] **Step 5: Залить сценарий и выключить пароль**

Run:
```bash
scp /root/myvpn-bot/scripts/hardening/harden.sh klopas:/root/harden.sh
ssh klopas 'chmod +x /root/harden.sh && /root/harden.sh disable-password /root/.ssh/bot_server1'
```
Expected: `вход по ключу подтверждён`, `автооткат вооружён: rollback-sshd сработает через 10 минут`, `пароль выключен`.

- [ ] **Step 6: В НОВОЙ сессии убедиться, что доступ жив**

Run: `ssh -o ControlMaster=no klopas 'echo связь-жива; sshd -T | grep -x "passwordauthentication no"'`
Expected: `связь-жива` и `passwordauthentication no`.
**Если связи нет — ничего не делать 10 минут, сервер откатится сам.**

- [ ] **Step 7: Убедиться, что бот по-прежнему ходит на сервер**

Run: `ssh klopas 'cd /root/myvpn-bot && python scripts/check_server_ssh.py 1'`
Expected: `OK 31.77.157.162: вход по ключу`
**Если ошибка — НЕ отменять автооткат. Просто подождать 10 минут: сервер сам вернёт прежний конфиг sshd и пароль снова заработает. Отмена автооткta здесь закрепила бы поломку.**

- [ ] **Step 8: Отменить автооткат**

Run: `ssh klopas '/root/harden.sh rollback-cancel'`
Expected: `автооткат отменён: rollback-sshd`

- [ ] **Step 9: Стереть пароль из базы — он больше не работает**

Run:
```bash
ssh klopas 'cd /root/myvpn-bot && python - <<PY
import asyncio, sys
sys.path.insert(0, ".")
from sqlalchemy import update as sa_update
from bot.db.base import async_session_maker
from bot.db.models import Server

async def main():
    async with async_session_maker() as s:
        await s.execute(sa_update(Server).where(Server.id == 1).values(ssh_password_enc=None))
        await s.commit()
    print("пароль стёрт из базы")

asyncio.run(main())
PY'
```
Expected: `пароль стёрт из базы`

- [ ] **Step 10: Финальная проверка бота и check**

Run: `ssh klopas 'cd /root/myvpn-bot && python scripts/check_server_ssh.py 1 && /root/harden.sh check 2>&1 | grep -i парол'`
Expected: `OK ... вход по ключу` и `OK   вход по паролю выключен`.

- [ ] **Step 11: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/hardening/harden.sh
git commit -m "Эталон: выключение входа по паролю с автооткатом

Пароль гасится только после доказанного входа по ключу; до изменения
вооружается автооткат на 10 минут, чтобы нельзя было отрезать себя.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Банилка перебора с белым списком

**Files:**
- Modify: `scripts/hardening/harden.sh` (добавить `apply-fail2ban`)

**Interfaces:**
- Consumes: `OWN_IP`, `VPN_SUBNET`, `BYPASS_SUBNET` из задачи 1.
- Produces: подкоманда `apply-fail2ban`.

- [ ] **Step 1: Добавить код в сценарий**

В `scripts/hardening/harden.sh` перед блоком `case` добавить:

```bash
cmd_apply_fail2ban() {
  echo "=== банилка перебора ==="
  DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban >/dev/null 2>&1 \
    || { echo "не удалось установить fail2ban"; return 1; }

  # Белый список: без собственного адреса банилка забанит самого бота —
  # он ходит по SSH сам на себя через внешний адрес.
  cat > /etc/fail2ban/jail.local <<CONF
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 ${OWN_IP} ${VPN_SUBNET} ${BYPASS_SUBNET}
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled = true
CONF

  systemctl enable --now fail2ban >/dev/null 2>&1
  systemctl restart fail2ban
  sleep 3
  fail2ban-client status sshd 2>&1 | sed 's/^/  /'
}
```

В блоке `case` добавить: `apply-fail2ban) cmd_apply_fail2ban ;;` и обновить строку использования.

- [ ] **Step 2: Проверить синтаксис и страж**

Run: `cd /root/myvpn-bot && bash -n scripts/hardening/harden.sh && python -m pytest tests/test_hardening.py -q`
Expected: синтаксис ок, страж PASS (`test_whitelist_has_vpn_subnets`, `test_whitelist_has_own_external_ip`).

- [ ] **Step 3: Применить на сервере**

Run:
```bash
scp /root/myvpn-bot/scripts/hardening/harden.sh klopas:/root/harden.sh
ssh klopas 'chmod +x /root/harden.sh && /root/harden.sh apply-fail2ban'
```
Expected: статус джейла `sshd`, число уже забаненных адресов больше нуля (переборщики стучатся постоянно).

- [ ] **Step 4: Убедиться, что белый список реально применился**

Run: `ssh klopas 'fail2ban-client get sshd ignoreip'`
Expected: в списке присутствуют `10.8.0.0/24`, `10.66.66.0/24` и внешний адрес сервера.
**Если подсетей VPN нет — остановиться: банилка может забанить бота.**

- [ ] **Step 5: Убедиться, что ни бот, ни ты не забанены**

Run:
```bash
ssh klopas 'cd /root/myvpn-bot && python scripts/check_server_ssh.py 1; fail2ban-client status sshd | grep -i "banned IP"'
```
Expected: `OK ... вход по ключу`; в списке забаненных нет адреса сервера и адресов из `10.8.0.0/24`.

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/hardening/harden.sh
git commit -m "Эталон: банилка перебора с белым списком

В белом списке собственный адрес сервера и подсети VPN/обхода — без них
банилка забанила бы самого бота и остановила выдачу конфигов.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Фаервол с автооткатом

Последний и второй по опасности шаг. Правила строятся от реально слушающих портов.

**Files:**
- Modify: `scripts/hardening/harden.sh` (добавить `apply-firewall`)

**Interfaces:**
- Consumes: `listening_ports`, `arm_rollback`, `PANEL_PORT`, `VPN_SUBNET`, `BYPASS_SUBNET`.
- Produces: подкоманда `apply-firewall`.

- [ ] **Step 1: Дописать страж-тест про панель (сначала падает)**

Добавить в конец `tests/test_hardening.py`:

```python
def test_panel_restricted_to_vpn(text: str) -> None:
    # Панель x-ui открывается только из VPN, а не всему интернету.
    assert '"$VPN_SUBNET" to any port "$PANEL_PORT"' in text
    assert '"$BYPASS_SUBNET" to any port "$PANEL_PORT"' in text
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `cd /root/myvpn-bot && python -m pytest tests/test_hardening.py::test_panel_restricted_to_vpn -v`
Expected: FAIL — правила для панели в сценарии ещё нет.

- [ ] **Step 3: Добавить код в сценарий**

В `scripts/hardening/harden.sh` перед блоком `case` добавить:

```bash
cmd_apply_firewall() {
  echo "=== фаервол ==="
  DEBIAN_FRONTEND=noninteractive apt-get install -y ufw >/dev/null 2>&1 \
    || { echo "не удалось установить ufw"; return 1; }

  # Автооткат ДО любых изменений.
  arm_rollback rollback-ufw "ufw --force disable"

  # Без этого ufw дропает форвард: VPN подключается, а интернета у клиента нет.
  sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
  grep -q '^DEFAULT_FORWARD_POLICY="ACCEPT"' /etc/default/ufw \
    || echo 'DEFAULT_FORWARD_POLICY="ACCEPT"' >> /etc/default/ufw

  ufw --force reset >/dev/null 2>&1
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw default allow routed  >/dev/null

  # Открываем ровно то, что слушает наружу, кроме панели.
  local port proto num
  while read -r port; do
    proto="${port%%/*}"; num="${port##*/}"
    [ "$num" = "$PANEL_PORT" ] && continue
    ufw allow "${num}/${proto}" >/dev/null
    echo "  открыт ${num}/${proto}"
  done < <(listening_ports)

  # Панель управления — только изнутри VPN.
  ufw allow from "$VPN_SUBNET" to any port "$PANEL_PORT" proto tcp >/dev/null
  ufw allow from "$BYPASS_SUBNET" to any port "$PANEL_PORT" proto tcp >/dev/null
  echo "  панель ${PANEL_PORT} — только из ${VPN_SUBNET} и ${BYPASS_SUBNET}"

  ufw --force enable >/dev/null
  ufw status verbose | head -12
  echo "Проверь связь и вызови: $0 rollback-cancel"
}
```

В блоке `case` добавить: `apply-firewall) cmd_apply_firewall ;;` и обновить строку использования.

- [ ] **Step 4: Проверить синтаксис и страж**

Run: `cd /root/myvpn-bot && bash -n scripts/hardening/harden.sh && python -m pytest tests/test_hardening.py -q`
Expected: синтаксис ок; страж PASS, включая `test_forward_policy_accept` и `test_panel_restricted_to_vpn`.

- [ ] **Step 5: Сухой прогон — посмотреть список портов глазами**

Run:
```bash
scp /root/myvpn-bot/scripts/hardening/harden.sh klopas:/root/harden.sh
ssh klopas 'chmod +x /root/harden.sh && /root/harden.sh plan'
```
Expected: в списке tcp/22, udp/585, udp/56000, udp/56001, tcp/8443, tcp/2096, tcp/16044, tcp/20476.
**Если чего-то не хватает — остановиться.**

- [ ] **Step 6: Применить**

Run: `ssh klopas '/root/harden.sh apply-firewall'`
Expected: `Status: active`, перечисленные правила, вооружённый автооткат.

- [ ] **Step 7: Проверить в НОВОЙ сессии, что связь и сервисы живы**

Run:
```bash
ssh -o ControlMaster=no klopas 'echo связь-жива; systemctl is-active myvpn-bot wdtt x-ui mtproto8443'
```
Expected: `связь-жива` и четыре `active`.
**Если связи нет — ждать 10 минут, фаервол выключится сам.**

- [ ] **Step 8: Проверить, что VPN у клиентов реально работает (форвард не сломан)**

Run:
```bash
ssh klopas 'awg show awg0 | grep -c "latest handshake"; ping -c 3 -W 3 10.8.0.2 | tail -2'
```
Expected: число пиров больше нуля; пинг до клиента проходит без потерь.
**Потери 100% означают сломанный форвард — немедленно `ssh klopas "ufw disable"`.**

- [ ] **Step 9: Проверить, что панель снаружи закрыта**

Run:
```bash
timeout 8 curl -s -o /dev/null -w "снаружи код: %{http_code}\n" http://31.77.157.162:6769/ || echo "снаружи недоступна — верно"
ssh klopas 'timeout 8 curl -s -o /dev/null -w "изнутри код: %{http_code}\n" http://127.0.0.1:6769/'
```
Expected: снаружи недоступна (таймаут или пустой код), изнутри отвечает.

- [ ] **Step 10: Отменить автооткат**

Run: `ssh klopas '/root/harden.sh rollback-cancel'`
Expected: `автооткат отменён: rollback-ufw`

- [ ] **Step 11: Коммит**

```bash
cd /root/myvpn-bot
git add scripts/hardening/harden.sh
git commit -m "Эталон: фаервол от реально слушающих портов, панель только из VPN

DEFAULT_FORWARD_POLICY=ACCEPT обязателен: иначе ufw дропает форвард и
у клиентов VPN подключается, но интернета нет. Автооткат на 10 минут.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Итоговая приёмка

**Files:**
- Modify: `docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md` (отметить, что этап 1 выполнен)

- [ ] **Step 1: Полная проверка соответствия эталону**

Run: `ssh klopas '/root/harden.sh check; echo "код возврата: $?"'`
Expected: все строки `OK`, `ИТОГ: сервер соответствует эталону`, код возврата `0`.

- [ ] **Step 2: Проверить, что бот работает и обслуживает клиентов**

Run:
```bash
ssh klopas 'cd /root/myvpn-bot && python scripts/check_server_ssh.py 1'
ssh klopas 'systemctl is-active myvpn-bot; journalctl -u myvpn-bot --since "-10min" --no-pager | grep -icE "error|traceback" || echo "ошибок нет"'
```
Expected: `OK ... вход по ключу`, `active`, `ошибок нет`.

- [ ] **Step 3: Убедиться, что перебор действительно перестал проходить**

Run: `ssh klopas 'fail2ban-client status sshd | sed "s/^/  /"'`
Expected: ненулевое число забаненных адресов.

- [ ] **Step 4: Прогнать весь тестовый набор проекта**

Run: `cd /root/myvpn-bot && python -m pytest -q 2>&1 | tail -5`
Expected: падений не больше, чем было до начала работы (в Termux штатно падают 2 теста из-за отсутствия PIL — они не связаны с этим планом).

- [ ] **Step 5: Отметить в спеке выполнение этапа 1**

В конец `docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md` добавить:

```markdown
## Статус

Этап 1 (сценарий-эталон + закрытие боевого сервера) выполнен.
Этап 2 (вызов из бота для новых серверов, кнопка в админке, тревоги
в телеграм) — отдельный план.
```

- [ ] **Step 6: Коммит**

```bash
cd /root/myvpn-bot
git add docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md
git commit -m "Спека: этап 1 защиты серверов выполнен

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Что НЕ входит в этот план

Второй этап, отдельным планом после приёмки первого:

- вызов сценария-эталона из мастера установки — новые серверы поднимаются защищёнными автоматически;
- кнопка «Проверить защиту» в админке для работающих серверов;
- тревоги в телеграм по порогам из спеки (упавший сервис, диск меньше 15%, сосед выше 20% дольше 10 минут, память на исходе, рост потерь, бот или админ в бане);
- сбор VPN-специфики поверх sysstat: потери на буферах приёма, давность рукопожатий, число забаненных.
