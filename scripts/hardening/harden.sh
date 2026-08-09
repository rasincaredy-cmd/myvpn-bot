#!/usr/bin/env bash
# Эталон безопасного сервера проекта. Идемпотентен: повторный запуск
# приводит сервер к нужному состоянию и ничего не ломает.
#
#   harden.sh check         — проверить соответствие эталону, ничего не менять
#   harden.sh plan          — показать, что изменится
#   harden.sh apply-journal  — поставить потолок журналу и обрезать его
#   harden.sh apply-fail2ban — банилка перебора с белым списком
#   harden.sh apply-firewall [обязательный_порт ...]
#                            — фаервол от слушающих портов (с автооткатом).
#                              ssh обязателен всегда; доп. порты (например
#                              ещё не поднятый на момент вызова VPN-порт)
#                              передаются аргументами и тоже обязаны слушать
#                              до включения фаервола. Формат аргумента строго
#                              `tcp/NNN` или `udp/NNN` — голое число (`585`)
#                              не распознаётся и приведёт к отказу включать
#                              фаервол.
#   harden.sh apply-stats    — включить сбор статистики (sysstat)
#   harden.sh disable-password [путь_к_ключу]
#                            — выключить вход по паролю (с автооткатом)
#   harden.sh rollback-cancel — вручную снять автооткат. Запасной путь:
#                              apply-firewall и disable-password при
#                              успешной самопроверке снимают его сами.
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

# --- Арифметика над IPv4-адресами (для I1: адрес привязки порта) ----------
#
# Чистый bash, без ipcalc/python — сценарий обязан оставаться
# самодостаточным. `10#` перед октетом защищает от того, что bash
# трактует числа с ведущим нулём как восьмеричные (октет "08" иначе
# упал бы с ошибкой "value too great for base").
ip_to_int() {
  local IFS=. o1 o2 o3 o4
  read -r o1 o2 o3 o4 <<<"$1"
  echo $(( (10#$o1 << 24) + (10#$o2 << 16) + (10#$o3 << 8) + 10#$o4 ))
}

in_cidr() {
  local ip="$1" cidr="$2" net bits ip_i net_i mask
  is_ipv4 "$ip" || return 1
  net="${cidr%/*}"; bits="${cidr#*/}"
  ip_i=$(ip_to_int "$ip")
  net_i=$(ip_to_int "$net")
  mask=$(( (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  [ $(( ip_i & mask )) -eq $(( net_i & mask )) ]
}

# I1: служба, слушающая только на внутреннем адресе (например резолвер на
# 10.8.0.1:53), не должна получить правило "разрешить всем" — это открытый
# резолвер на сервере, который мы защищаем. VPN_SUBNET и BYPASS_SUBNET уже
# входят в 10.0.0.0/8, но проверяем и вообще частные диапазоны — мало ли
# какая ещё внутренняя служба на сервере слушает на приватном адресе.
is_private_ipv4() {
  local ip="$1"
  in_cidr "$ip" "10.0.0.0/8" && return 0
  in_cidr "$ip" "172.16.0.0/12" && return 0
  in_cidr "$ip" "192.168.0.0/16" && return 0
  return 1
}

# Порты, которые реально слушают наружу, вместе с адресом привязки.
# Фаервол строится от них, а не от списка из головы — иначе легко забыть
# нужный и отрезать сервис. Адрес нужен отдельно от порта: без него служба
# на внутреннем VPN-адресе получила бы правило "всем" (см. is_private_ipv4).
listening_ports() {
  ss -tulnH 2>/dev/null | awk '
    $5 !~ /^127\./ && $5 !~ /^\[::1\]/ {
      n = split($5, a, ":"); port = a[n]
      addr = substr($5, 1, length($5) - length(port) - 1)
      if (port ~ /^[0-9]+$/) print ($1 == "udp" ? "udp/" : "tcp/") port, addr
    }' | sort -u
}

# Порт, на котором реально слушает sshd (по эффективному конфигу, а не по
# догадке "наверняка 22") — нужен и для check, и для обязательного списка
# портов в apply-firewall.
current_ssh_port() {
  local p
  p="$(sshd -T 2>/dev/null | awk '$1=="port"{print $2; exit}')"
  [ -n "$p" ] && echo "$p" || echo 22
}

# Есть ли строка вида "proto/port" в списке строк "proto/port addr" —
# используется, чтобы проверить обязательные порты (C3), не заботясь про
# адрес привязки.
port_is_listening() {
  local want="$1" ports="$2"
  awk -v w="$want" '$1==w{f=1} END{exit !f}' <<<"$ports"
}

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

password_actually_off() {
  local out
  out="$(sshd -T 2>/dev/null)"
  grep -qx "passwordauthentication no" <<<"$out"
}

check_password_off() {
  # ВНИМАНИЕ: не писать `sshd -T | grep -q ...`. Скрипт работает под
  # `set -o pipefail`, а `grep -q` закрывает трубу на первом совпадении —
  # источник получает SIGPIPE, весь конвейер становится «неуспешным», и
  # НАЙДЕННАЯ строка читается как ненайденная. Проверка начинает врать
  # ровно наоборот. Поэтому вывод сначала в переменную, потом сравнение.
  if password_actually_off; then
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

  # I3: служба fail2ban может быть active, а конкретный джейл — не
  # подняться (типовая причина: backend=systemd и на сервере не хватает
  # зависимости для чтения журнала systemd). Тогда "банилка работает"
  # при нуле реальной защиты. Опрашиваем именно джейл и смотрим код
  # возврата команды, а не сам факт, что служба жива.
  if fail2ban-client status sshd >/dev/null 2>&1; then
    ok "джейл sshd поднят"
  else
    fail "джейл sshd НЕ поднят — банилка не защищает ssh"
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
  # verbose, а не короткий status: короткий вывод не содержит строку
  # "Default: ..." с политикой по умолчанию, которая нужна ниже (I2).
  ufw_status="$(ufw status verbose 2>/dev/null)"
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
    return
  fi
  # I2: включённый ufw с политикой "allow incoming" пропускает всё —
  # такой сервер раньше проходил check целиком и получал "соответствует
  # эталону", хотя фаервол фактически ничего не блокирует.
  if grep -qE '^Default: deny \(incoming\)' <<<"$ufw_status"; then
    ok "политика по умолчанию для входящих — запрещающая"
  else
    fail "политика по умолчанию для входящих НЕ запрещающая — фаервол ничего не блокирует"
  fi
  if grep -q "${PANEL_PORT}.*ALLOW.*Anywhere" <<<"$ufw_status"; then
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
  # I1/N4: apply-firewall НЕ открывает наружу порты, слушающие на приватном
  # адресе (см. is_private_ipv4 в cmd_apply_firewall) — показывать их в
  # списке "останутся открытыми наружу" было бы враньём. Разносим по двум
  # спискам заранее, а не фильтруем один общий.
  local ports port addr num open_lines="" private_lines=""
  ports="$(listening_ports)"
  while read -r port addr; do
    [ -z "$port" ] && continue
    num="${port##*/}"
    [ "$num" = "$PANEL_PORT" ] && continue
    if is_private_ipv4 "$addr"; then
      private_lines="${private_lines}  ${port} ${addr} — только изнутри, наружу открыт не будет"$'\n'
    else
      open_lines="${open_lines}  ${port} ${addr}"$'\n'
    fi
  done <<<"$ports"
  echo "останутся открытыми наружу порты (адрес привязки справа):"
  printf '%s' "$open_lines"
  if [ -n "$private_lines" ]; then
    echo "слушают только на приватном адресе (apply-firewall наружу их не откроет):"
    printf '%s' "$private_lines"
  fi
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

cmd_apply_fail2ban() {
  echo "=== банилка перебора ==="
  if ! systemctl is-active --quiet fail2ban; then
    # backend=systemd (см. jail.local ниже) не стартует без python3-systemd —
    # ставим зависимость сразу вместе с пакетом, а не по факту падения джейла.
    DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban python3-systemd >/dev/null 2>&1 \
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
  if ! systemctl is-active --quiet fail2ban; then
    fail "fail2ban не активен после запуска"
    return 1
  fi
  # I3 (та же самая история про "служба жива — джейл не обязательно"):
  # применение тоже обязано убедиться, что джейл реально поднялся, а не
  # только что демон стартовал. jail_ready повторяет опрос — единственная
  # попытка сразу после старта на нагруженной машине даёт ложный отказ.
  if ! jail_ready; then
    fail "джейл sshd не поднялся после запуска fail2ban"
    return 1
  fi
  ok "банилка запущена, белый список: ${OWN_IP} ${VPN_SUBNET} ${BYPASS_SUBNET}"
  fail2ban-client status sshd 2>&1 | sed 's/^/  /'
  return 0
}

cmd_apply_firewall() {
  echo "=== фаервол ==="
  if ! command -v ufw >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y ufw >/dev/null 2>&1 \
      || { fail "не удалось установить ufw"; return 1; }
  fi

  local ssh_port required rp num proto
  ssh_port="$(current_ssh_port)"
  # C3: ssh обязателен всегда — без него запертый сервер уже никак не
  # открыть. Остальные обязательные порты передаёт вызывающий (бот): точка
  # вызова по плану — сразу после установки VPN, когда интерфейс может
  # быть ещё не поднят. Мгновенный снимок listening_ports его тогда не
  # увидит, и молчаливая потеря порта означала бы сервер с VPN, к
  # которому нельзя подключиться.
  required="tcp/${ssh_port}"
  for rp in "$@"; do
    required="${required} ${rp}"
  done

  local ports
  ports="$(listening_ports)"
  if [ -z "$ports" ]; then
    fail "не удалось определить слушающие порты — включать фаервол вслепую НЕЛЬЗЯ"
    return 1
  fi

  local missing=""
  for rp in $required; do
    port_is_listening "$rp" "$ports" || missing="${missing}${rp} "
  done
  if [ -n "$missing" ]; then
    fail "обязательный порт не слушает: ${missing}— фаервол НЕ включаю"
    return 1
  fi

  # Автооткат ДО любых изменений: если что-то пойдёт не так и связь
  # пропадёт, сервер сам выключит фаервол через 10 минут.
  arm_rollback rollback-ufw "ufw --force disable" || return 1

  # Без этого ufw дропает транзитный трафик: VPN подключается, а интернета
  # у клиента нет. Самая частая авария при включении фаервола на VPN-узле.
  sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
  if ! grep -q '^DEFAULT_FORWARD_POLICY="ACCEPT"' /etc/default/ufw; then
    echo 'DEFAULT_FORWARD_POLICY="ACCEPT"' >> /etc/default/ufw
  fi

  ufw --force reset >/dev/null 2>&1
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw default allow routed >/dev/null

  # Открываем ровно то, что слушает наружу, кроме панели управления.
  local port addr
  while read -r port addr; do
    [ -z "$port" ] && continue
    proto="${port%%/*}"
    num="${port##*/}"
    [ "$num" = "$PANEL_PORT" ] && continue
    if is_private_ipv4 "$addr"; then
      # I1: служба на внутреннем адресе (например резолвер на 10.8.0.1:53)
      # не должна получить правило "всем" — это открытый резолвер на
      # сервере, который мы защищаем. Если адрес попадает конкретно в
      # VPN_SUBNET/BYPASS_SUBNET — открываем её так же, как панель: только
      # тем, кому и положено ходить внутрь. Для прочих приватных адресов
      # источник доступа неизвестен — правило просто не создаём (порт и
      # так недоступен снаружи, адрес приватный).
      if in_cidr "$addr" "$VPN_SUBNET" || in_cidr "$addr" "$BYPASS_SUBNET"; then
        ufw allow from "$VPN_SUBNET" to "$addr" port "$num" proto "$proto" >/dev/null
        ufw allow from "$BYPASS_SUBNET" to "$addr" port "$num" proto "$proto" >/dev/null
        echo "  ${num}/${proto} на ${addr} — только из ${VPN_SUBNET} и ${BYPASS_SUBNET}"
      else
        echo "  ${num}/${proto} слушает на внутреннем адресе ${addr} — наружу не открываю"
      fi
      continue
    fi
    ufw allow "${num}/${proto}" >/dev/null && echo "  открыт ${num}/${proto}"
  done <<<"$ports"

  # Панель управления — только изнутри VPN и обхода.
  ufw allow from "$VPN_SUBNET" to any port "$PANEL_PORT" proto tcp >/dev/null
  ufw allow from "$BYPASS_SUBNET" to any port "$PANEL_PORT" proto tcp >/dev/null
  echo "  панель ${PANEL_PORT} — только из ${VPN_SUBNET} и ${BYPASS_SUBNET}"

  if ! ufw --force enable >/dev/null; then
    fail "ufw не включился"
    return 1
  fi
  ok "фаервол включён"
  ufw status verbose 2>&1 | head -12 | sed 's/^/  /'

  # C3 (после включения): убеждаемся, что обязательные порты реально
  # попали в применённый набор правил, а не только должны были попасть.
  local ufw_after missing_after=""
  ufw_after="$(ufw status 2>/dev/null)"
  for rp in $required; do
    num="${rp##*/}"; proto="${rp%%/*}"
    rule_present "$ufw_after" "$num" "$proto" || missing_after="${missing_after}${rp} "
  done
  if [ -n "$missing_after" ]; then
    fail "после включения в правилах фаервола не хватает: ${missing_after}— автооткат ОСТАВЛЕН вооружённым"
    return 1
  fi

  # C1: сервер сам проверяет себя и, если всё хорошо, сам снимает
  # автооткат — иначе повторный прогон на уже настроенном сервере (штатный
  # сценарий для бота, без человека) неизбежно выключит фаервол через 10
  # минут, если никто не позвал rollback-cancel руками.
  #
  # Проверить НОВОЕ соединение к своему внешнему адресу изнутри той же
  # машины нельзя доверять: пакеты к собственному адресу маршрутизируются
  # ядром как RTN_LOCAL и приходят на вход с iif=lo — тем же путём, что и
  # обращение к 127.0.0.1, которое ufw (как и большинство фаерволов)
  # безусловно пропускает до пользовательских правил. Такая "проверка"
  # была бы точно такой же ложью, как `grep -q` под pipefail в
  # check_password_off: всегда зелёная, независимо от реальных правил.
  # Поэтому проверяем то, что действительно наблюдаемо изнутри честно:
  # правило ssh-порта в применённом наборе (уже сделано выше) и то, что
  # сам sshd не пострадал и продолжает слушать свой порт.
  if { systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; } \
     && ss -tlnH 2>/dev/null | awk -v p=":${ssh_port}\$" '$4 ~ p {f=1} END{exit !f}'; then
    ok "ssh-порт в правилах фаервола, sshd жив — снимаю автооткат"
    if ! rollback_cancel_unit rollback-ufw; then
      fail "не удалось снять автооткат — через 10 минут он выключит фаервол"
      return 1
    fi
    echo "автооткат снят, фаервол применён"
  else
    fail "sshd не активен или не слушает ${ssh_port}/tcp после включения фаервола — автооткат ОСТАВЛЕН вооружённым, проверь вручную и вызови: $0 rollback-cancel"
    return 1
  fi
  return 0
}

cmd_apply_stats() {
  echo "=== сбор статистики (sysstat) ==="
  # I6: check требует работающий sysstat-collect.timer, но раньше ни одна
  # apply-команда его не устанавливала — на проде это сделали руками, а
  # новый сервер никогда бы не стал "соответствующим эталону".
  if ! dpkg -s sysstat >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y sysstat >/dev/null 2>&1 \
      || { fail "не удалось установить sysstat"; return 1; }
  fi

  # Хранение — 30 дней, а не пакетный дефолт (обычно неделя).
  local conf=/etc/sysstat/sysstat
  if [ -f "$conf" ]; then
    if grep -qE '^HISTORY=' "$conf"; then
      sed -i 's/^HISTORY=.*/HISTORY=30/' "$conf"
    else
      printf '\nHISTORY=30\n' >> "$conf"
    fi
  fi

  # Шаг сбора — 2 минуты, а не пакетный дефолт (обычно 10): за 10 минут
  # короткий всплеск нагрузки может провалиться между двумя замерами.
  # Переопределяем systemd-таймер дропином, а не правкой юнита из пакета —
  # дропин переживёт обновление sysstat, юнит пакета — нет.
  mkdir -p /etc/systemd/system/sysstat-collect.timer.d
  cat > /etc/systemd/system/sysstat-collect.timer.d/override.conf <<'CONF'
[Timer]
OnCalendar=
OnCalendar=*:0/2
CONF
  # На системах, где sysstat всё ещё дёргается через cron (не systemd-
  # таймер), правим и его — иначе на такой системе шаг остался бы 10 мин.
  local cron=/etc/cron.d/sysstat
  if [ -f "$cron" ]; then
    sed -i -E 's#\*/[0-9]+(\s+\*\s+\*\s+\*\s+\*\s+root\s+command\s+-v\s+debian-sa1)#*/2\1#' "$cron"
  fi

  systemctl daemon-reload
  systemctl enable --now sysstat-collect.timer >/dev/null 2>&1
  systemctl enable --now sysstat >/dev/null 2>&1 || true

  if ! systemctl is-active --quiet sysstat-collect.timer; then
    fail "sysstat-collect.timer не активен после включения"
    return 1
  fi
  ok "сбор статистики включён (шаг 2 минуты, хранение 30 дней)"
  return 0
}

# --- Выключение входа по паролю -------------------------------------------
#
# Пароль гасится ОТДЕЛЬНЫМ файлом настроек, а не правкой sshd_config.
# Отсюда важное следствие для отката: вернуть доступ можно только УДАЛИВ
# этот файл. Восстановление старого sshd_config из копии его не тронет —
# и «страховка» окажется фиктивной.
#
# C2: имя файла — 00-, а не 99-. sshd берёт ПЕРВОЕ встреченное значение,
# а drop-in-файлы подключаются по алфавиту: типовой облачный
# 50-cloud-init.conf с "PasswordAuthentication yes" выигрывал бы у 99-.
# 00- гарантированно читается первым.
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-hardening.conf
# Имя из более ранней версии сценария — если осталось на сервере, лучше
# убрать: путаницы с дублем настроек быть не должно (хотя обе версии
# согласны в содержимом, так что сама по себе не опасна).
SSHD_DROPIN_LEGACY=/etc/ssh/sshd_config.d/99-hardening.conf

# I8: юнит ssh называется по-разному (ssh на Debian/Ubuntu, sshd на
# большинстве прочих дистрибутивов). Раньше рестарт был прибит к одному
# имени — на системе с другим юнитом рестарт молча не случался бы, и
# правка конфига не применялась вовсе.
restart_ssh_service() {
  systemctl restart ssh 2>/dev/null && return 0
  systemctl restart sshd 2>/dev/null && return 0
  return 1
}

# C2: на части систем строки Include для sshd_config.d в sshd_config нет
# вовсе, и весь drop-in каталог молча игнорируется целиком. Раньше это не
# проверялось: проверка синтаксиса и рестарт проходили зелёными, а пароль
# оставался включён — скрипт врал, что выключил его.
sshd_dropin_included() {
  grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' \
    /etc/ssh/sshd_config 2>/dev/null
}

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

# Снять автооткат одного юнита. Общий код для ручной отмены
# (cmd_rollback_cancel) и для автоматической (C1) после самопроверки.
rollback_cancel_unit() {
  local unit="$1"
  if systemctl stop "${unit}.timer" 2>/dev/null; then
    systemctl reset-failed "$unit" 2>/dev/null || true
    echo "автооткат отменён: ${unit}"
    return 0
  fi
  return 1
}

cmd_rollback_cancel() {
  local n=0
  for unit in rollback-sshd rollback-ufw; do
    rollback_cancel_unit "$unit" && n=$((n+1))
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

  # C2: если Include для sshd_config.d нет в sshd_config, наш drop-in
  # никогда не будет прочитан — sshd -t и рестарт пройдут зелёными, а
  # пароль так и останется включён. Не притворяемся, что всё в порядке.
  if ! sshd_dropin_included; then
    fail "в sshd_config нет Include для sshd_config.d — drop-in будет проигнорирован, пароль НЕ трогаю"
    return 1
  fi

  # Откат удаляет ровно то, что мы создаём, и поднимает sshd обратно.
  arm_rollback rollback-sshd "rm -f ${SSHD_DROPIN}; systemctl restart ssh 2>/dev/null || systemctl restart sshd" || return 1

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
  if ! restart_ssh_service; then
    fail "sshd не перезапустился — откатываю немедленно"
    rm -f "$SSHD_DROPIN"
    restart_ssh_service || true
    cmd_rollback_cancel
    return 1
  fi

  # C2: мало того, что рестарт прошёл — конфиг мог быть перебит другим
  # drop-in-файлом (типовой 50-cloud-init.conf), и пароль остался бы
  # включён при зелёном рестарте. Проверяем ФАКТ через sshd -T, а не
  # намерение.
  if ! password_actually_off; then
    fail "после рестарта sshd -T всё ещё показывает пароль включённым — откатываю немедленно"
    rm -f "$SSHD_DROPIN"
    restart_ssh_service || true
    cmd_rollback_cancel
    return 1
  fi
  ok "пароль выключен, sshd -T подтверждает"

  # C1: самопроверка входа по ключу ПОСЛЕ изменения — раз доступ жив,
  # автооткат можно снять сразу, не дожидаясь ручной команды. Для бота,
  # который будет запускать это без участия человека, забытая ручная
  # отмена через 10 минут вернула бы пароль обратно — гарантированная
  # авария при простом повторном прогоне на уже настроенном сервере.
  if verify_key_login "${1:-}"; then
    ok "вход по ключу подтверждён после изменения — снимаю автооткат"
    if ! rollback_cancel_unit rollback-sshd; then
      fail "не удалось снять автооткат — через 10 минут он вернёт вход по паролю"
      return 1
    fi
  else
    fail "вход по ключу НЕ подтверждён после изменения — автооткат ОСТАВЛЕН вооружённым"
    return 1
  fi

  # Старое имя файла из ранней версии сценария убираем ТОЛЬКО здесь —
  # когда новый уже применён и подтверждён. Удали мы его раньше и споткнись
  # на вооружении автоотката, сервер остался бы вообще без drop-in: пароль
  # тихо вернулся бы при ближайшем рестарте sshd.
  if [ -f "$SSHD_DROPIN_LEGACY" ]; then
    rm -f "$SSHD_DROPIN_LEGACY"
    echo "  убран старый файл настроек ${SSHD_DROPIN_LEGACY}"
  fi
  return 0
}

case "${1:-}" in
  check) cmd_check ;;
  plan)  cmd_plan ;;
  apply-journal) cmd_apply_journal ;;
  apply-fail2ban) cmd_apply_fail2ban ;;
  apply-firewall) shift; cmd_apply_firewall "$@" ;;
  apply-stats) cmd_apply_stats ;;
  disable-password) shift; cmd_disable_password "${1:-}" ;;
  rollback-cancel)  cmd_rollback_cancel ;;
  *) echo "использование: $0 {check|plan|apply-journal|apply-fail2ban|apply-firewall|apply-stats|disable-password|rollback-cancel}" >&2; exit 2 ;;
esac
