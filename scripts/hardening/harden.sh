#!/usr/bin/env bash
# Эталон безопасного сервера проекта. Идемпотентен: повторный запуск
# приводит сервер к нужному состоянию и ничего не ломает.
#
#   harden.sh check         — проверить соответствие эталону, ничего не менять
#   harden.sh plan          — показать, что изменится
#   harden.sh apply-journal  — поставить потолок журналу и обрезать его
#   harden.sh apply-fail2ban — банилка перебора с белым списком
#
# Спека: docs/superpowers/specs/2026-08-08-zashchita-serverov-design.md
set -uo pipefail

VPN_SUBNET="10.8.0.0/24"
BYPASS_SUBNET="10.66.66.0/24"
PANEL_PORT=6769
JOURNAL_CAP="500M"

# Собственный внешний адрес: бот ходит по SSH сам на себя через него.
# Поле берём по имени "src", а не по номеру колонки — при маршруте без
# шлюза (прямой роутинг) позиция $7 съезжает и молча подставляет мусор.
is_ipv4() { [[ "$1" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; }

OWN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
is_ipv4 "$OWN_IP" || OWN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
is_ipv4 "$OWN_IP" || OWN_IP=""

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
  # ВНИМАНИЕ: не писать `sshd -T | grep -q ...`. Скрипт работает под
  # `set -o pipefail`, а `grep -q` закрывает трубу на первом совпадении —
  # источник получает SIGPIPE, весь конвейер становится «неуспешным», и
  # НАЙДЕННАЯ строка читается как ненайденная. Проверка начинает врать
  # ровно наоборот. Поэтому вывод сначала в переменную, потом сравнение.
  local sshd_conf
  sshd_conf="$(sshd -T 2>/dev/null)"
  if grep -qx "passwordauthentication no" <<<"$sshd_conf"; then
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

  local jail="/etc/fail2ban/jail.local"
  if [ ! -f "$jail" ]; then
    fail "нет файла белого списка банилки (jail.local) — забанит бота"
    return
  fi

  # В белом списке обязаны быть все три адреса: свой (иначе банилка
  # забанит бота — он ходит по SSH сам на себя), подсеть VPN и подсеть
  # обхода. Не хватает хотя бы одного — считаем это несоответствием, а
  # не наличием "хоть чего-то".
  local missing=""
  if [ -z "$OWN_IP" ] || ! grep -qF -- "$OWN_IP" "$jail"; then
    missing="${missing}OWN_IP "
  fi
  grep -qF -- "$VPN_SUBNET" "$jail" || missing="${missing}VPN_SUBNET "
  grep -qF -- "$BYPASS_SUBNET" "$jail" || missing="${missing}BYPASS_SUBNET "

  if [ -z "$missing" ]; then
    ok "белый список банилки на месте"
  else
    fail "в белом списке банилки не хватает: ${missing}— забанит бота или обрежет обход"
  fi
}

check_firewall() {
  local ufw_status ufw_active=0
  ufw_status="$(ufw status 2>/dev/null)"
  if grep -q "Status: active" <<<"$ufw_status"; then
    ok "фаервол включён"
    ufw_active=1
  else
    fail "фаервол выключен"
  fi
  if grep -q '^DEFAULT_FORWARD_POLICY="ACCEPT"' /etc/default/ufw 2>/dev/null; then
    ok "форвард разрешён (VPN будет маршрутизировать)"
  else
    fail "форвард НЕ разрешён — у клиентов не будет интернета"
  fi
  # Пока фаервол выключен, ufw status ничего не печатает и правил ALLOW
  # в выводе нет вовсе — тогда grep по ALLOW ничего не находит и молча
  # соврёт "не открыта". На деле без фаервола панель открыта всем портом.
  if [ "$ufw_active" -eq 0 ]; then
    fail "фаервол выключен — панель x-ui открыта всему интернету"
  elif grep -q "${PANEL_PORT}.*ALLOW.*Anywhere" <<<"$ufw_status"; then
    fail "панель x-ui открыта всему интернету"
  else
    ok "панель x-ui не открыта наружу"
  fi
}

check_journal() {
  # Одного правильного конфига мало: конфиг мог примениться, а сама
  # служба после этого не подняться — тогда журналирование мертво, а
  # проверка конфига этого не увидит и соврёт "OK".
  if systemctl is-active --quiet systemd-journald; then
    ok "служба журнала (systemd-journald) активна"
  else
    fail "служба журнала (systemd-journald) НЕ активна — журналирование мертво"
  fi
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

cmd_apply_journal() {
  echo "=== потолок журнала ${JOURNAL_CAP} ==="
  local conf=/etc/systemd/journald.conf
  # "Копии ещё нет" — норма (идемпотентность, ничего не делаем повторно).
  # А вот если файл уже есть, но cp реально не смог скопировать (нет
  # места, нет прав) — это настоящая ошибка, и молчать нельзя: дальше
  # конфиг правился бы без возможности откатиться.
  if [ ! -f "${conf}.bak" ]; then
    if ! cp "$conf" "${conf}.bak"; then
      fail "не удалось сделать резервную копию ${conf} — конфиг НЕ трогаю"
      return 1
    fi
  fi
  if grep -qE "^#?SystemMaxUse=" "$conf"; then
    sed -i "s/^#\?SystemMaxUse=.*/SystemMaxUse=${JOURNAL_CAP}/" "$conf"
  else
    printf '\nSystemMaxUse=%s\n' "$JOURNAL_CAP" >> "$conf"
  fi
  if ! systemctl restart systemd-journald; then
    fail "служба журнала (systemd-journald) не перезапустилась после правки конфига"
    return 1
  fi
  # vacuum-size не учитывает активный, ещё не заротированный файл —
  # без принудительной ротации потолок не достигается за один проход
  # и на сервере остаётся немного выше JOURNAL_CAP.
  journalctl --rotate 2>&1 | tail -2
  journalctl --vacuum-size="$JOURNAL_CAP" 2>&1 | tail -2
  echo "стало: $(journalctl --disk-usage 2>/dev/null)"
}

cmd_apply_fail2ban() {
  echo "=== банилка перебора ==="
  if ! systemctl is-active --quiet fail2ban; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban >/dev/null 2>&1 \
      || { fail "не удалось установить fail2ban"; return 1; }
  fi

  # Белый список — самое важное здесь. Без собственного адреса банилка
  # забанит САМОГО БОТА: он ходит по SSH на сервер через его же внешний
  # адрес. Подсети VPN и обхода — путь администратора внутрь, который
  # должен выжить, даже если внешний адрес сменился.
  if [ -z "$OWN_IP" ]; then
    fail "не удалось определить собственный адрес — без него белый список опасен"
    return 1
  fi

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

  systemctl enable fail2ban >/dev/null 2>&1
  if ! systemctl restart fail2ban; then
    fail "fail2ban не запустился"
    return 1
  fi
  sleep 3
  if ! systemctl is-active --quiet fail2ban; then
    fail "fail2ban не активен после запуска"
    return 1
  fi
  ok "банилка запущена, белый список: ${OWN_IP} ${VPN_SUBNET} ${BYPASS_SUBNET}"
  fail2ban-client status sshd 2>&1 | sed 's/^/  /'
  return 0
}

# --- Выключение входа по паролю -------------------------------------------
#
# Пароль гасится ОТДЕЛЬНЫМ файлом настроек, а не правкой sshd_config.
# Отсюда важное следствие для отката: вернуть доступ можно только УДАЛИВ
# этот файл. Восстановление старого sshd_config из копии его не тронет —
# и «страховка» окажется фиктивной.
SSHD_DROPIN=/etc/ssh/sshd_config.d/99-hardening.conf

# Автооткат: сервер сам вернёт настройки, если через 10 минут никто
# не подтвердил, что доступ жив. Ставится ДО изменения.
arm_rollback() {
  local unit="$1" cmd="$2"
  systemctl stop "${unit}.timer" 2>/dev/null || true
  systemctl reset-failed "$unit" 2>/dev/null || true
  if systemd-run --on-active=10min --unit="$unit" \
       /bin/bash -c "$cmd" >/dev/null 2>&1; then
    echo "автооткат вооружён: ${unit} сработает через 10 минут"
    return 0
  fi
  fail "не удалось вооружить автооткат ${unit} — опасный шаг делать НЕЛЬЗЯ"
  return 1
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
  return 0
}

# Доказать вход по ключу ДО того, как гасить пароль.
verify_key_login() {
  local key="${1:-/root/.ssh/bot_server1}"
  [ -f "$key" ] || { echo "нет файла ключа $key"; return 1; }
  # Тот же капкан, что в check_password_off: под pipefail `... | grep -q`
  # ломается от SIGPIPE. Здесь это особенно опасно — от результата зависит,
  # можно ли гасить пароль. Поэтому забираем вывод в переменную.
  local out
  out="$(ssh -n -i "$key" -o StrictHostKeyChecking=no -o PasswordAuthentication=no \
      -o PubkeyAuthentication=yes -o BatchMode=yes -o ConnectTimeout=10 \
      root@127.0.0.1 'echo ok' 2>/dev/null)"
  [ "$out" = "ok" ]
}

cmd_disable_password() {
  echo "=== выключение входа по паролю ==="
  if ! verify_key_login "${1:-}"; then
    fail "вход по ключу НЕ работает — пароль оставлен включённым"
    return 1
  fi
  ok "вход по ключу подтверждён"

  # Откат удаляет ровно то, что мы создаём, и поднимает sshd обратно.
  arm_rollback rollback-sshd "rm -f ${SSHD_DROPIN}; systemctl restart ssh" || return 1

  mkdir -p /etc/ssh/sshd_config.d
  cat > "$SSHD_DROPIN" <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF

  if ! sshd -t 2>&1; then
    fail "конфиг sshd невалиден — откатываю немедленно"
    rm -f "$SSHD_DROPIN"
    cmd_rollback_cancel
    return 1
  fi
  if ! systemctl restart ssh; then
    fail "sshd не перезапустился — откатываю немедленно"
    rm -f "$SSHD_DROPIN"
    systemctl restart ssh || true
    cmd_rollback_cancel
    return 1
  fi
  echo "пароль выключен. Проверь вход В НОВОЙ сессии и вызови: $0 rollback-cancel"
  return 0
}

case "${1:-}" in
  check) cmd_check ;;
  plan)  cmd_plan ;;
  apply-journal) cmd_apply_journal ;;
  apply-fail2ban) cmd_apply_fail2ban ;;
  disable-password) shift; cmd_disable_password "${1:-}" ;;
  rollback-cancel)  cmd_rollback_cancel ;;
  *) echo "использование: $0 {check|plan|apply-journal|apply-fail2ban|disable-password|rollback-cancel}" >&2; exit 2 ;;
esac
