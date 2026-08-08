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
  if echo "$ufw_status" | grep -q "Status: active"; then
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
  elif echo "$ufw_status" | grep -q "${PANEL_PORT}.*ALLOW.*Anywhere"; then
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
