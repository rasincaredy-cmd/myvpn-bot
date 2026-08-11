#!/usr/bin/env bash
# Учёт прилёта пакетов на порты обхода БС.
#
# Зачем. В логе демона обхода видны только те соединения, что дошли до разбора
# полезного пакета: он пишет «ключ выбран» на успех и «отказ» на плохой пакет.
# А пакеты DTLS-рукопожатия демон пропускает молча, без единой строки. Поэтому
# «до сервера не долетело ничего» и «долетело, но заглохло на рукопожатии»
# выглядят в его логе одинаково — пустотой. sysstat тоже не помогает: он ведёт
# историю по интерфейсу целиком, а на том же интерфейсе живёт обычный VPN.
# Этот скрипт снимает разницу там, где её ещё видно, — на уровне фаервола.
#
# Что делает. Ставит цепочку, которая только СЧИТАЕТ пакеты на портах обхода и
# ничего не решает: решение «пропустить» как принимали существующие правила,
# так и принимают. Раз в минуту дописывает прирост в файл дня. Строка пишется
# всегда, в том числе нулевая, — иначе «тихо» и «скрипт умер» опять сольются.
#
# Правила проверяются на каждом запуске: если их снесли (перезагрузка,
# `ufw reload`), они вернутся на следующей минуте. Счётчики при этом обнулятся,
# в строке будет reset=1 — прирост за ту минуту считать нельзя.
set -uo pipefail

CHAIN=WDTT_PROBE
LOGDIR=/var/log/wdtt-probe
STATEDIR=/var/lib/wdtt-probe
STATE="$STATEDIR/state"
RETENTION_DAYS=14
UNIT=/etc/systemd/system/wdtt.service
IPT=(iptables -w 5)

# Всплеск 10 августа дал 219 новых сессий в час. Лимит 60 строк в минуту на
# порт закрывает такой всплеск с запасом и не даёт залить журнал при сканах.
KNOCK_LIMIT="60/min"
KNOCK_BURST=120

mkdir -p "$LOGDIR" "$STATEDIR"

# Порты берём из файла службы обхода, а не константой: нода может слушать своё,
# и тогда молчащий счётчик врал бы про «ничего не прилетает».
detect_ports() {
    local line dtls wg
    line="$(grep -m1 '^ExecStart=' "$UNIT" 2>/dev/null)"
    dtls="$(sed -n 's/.*-listen [^:]*:\([0-9]\{1,\}\).*/\1/p' <<<"$line")"
    wg="$(sed -n 's/.*-wg-port \([0-9]\{1,\}\).*/\1/p' <<<"$line")"
    echo "${dtls:-56000} ${wg:-56001}"
}

read -r PORT_DTLS PORT_WG <<<"$(detect_ports)"
PORTS=("$PORT_DTLS" "$PORT_WG")

# Ожидаемое наполнение цепочки: на каждый порт счётное правило и правило
# журнала. Если состав разошёлся (руками поправили, половину снесли), цепочку
# пересобираем целиком — так состояние однозначно, а сброс счётчиков виден.
expected_rules=$(( ${#PORTS[@]} * 2 ))

rebuild_chain() {
    "${IPT[@]}" -F "$CHAIN" 2>/dev/null
    local p
    for p in "${PORTS[@]}"; do
        "${IPT[@]}" -A "$CHAIN" -p udp --dport "$p" \
            -m comment --comment "WDTT_PROBE_CNT_$p"
        "${IPT[@]}" -A "$CHAIN" -p udp --dport "$p" \
            -m conntrack --ctstate NEW \
            -m limit --limit "$KNOCK_LIMIT" --limit-burst "$KNOCK_BURST" \
            -j LOG --log-prefix "WDTT_KNOCK:$p " --log-level 6
    done
}

ensure_rules() {
    "${IPT[@]}" -n -L "$CHAIN" >/dev/null 2>&1 || "${IPT[@]}" -N "$CHAIN"
    # Первой в INPUT: считаем всё, что дошло до хоста, независимо от того,
    # пропустят пакет дальше или уронят. Вывод -C глушим весь: сборка iptables
    # поверх nftables печатает найденное правило в stdout, и оно бы засоряло
    # вывод ручного запуска.
    "${IPT[@]}" -C INPUT -j "$CHAIN" >/dev/null 2>&1 || "${IPT[@]}" -I INPUT 1 -j "$CHAIN"

    local have
    have="$("${IPT[@]}" -S "$CHAIN" 2>/dev/null | grep -c "^-A $CHAIN")"
    if [ "$have" -ne "$expected_rules" ]; then
        rebuild_chain
        return 1  # счётчики обнулены
    fi
    local p
    for p in "${PORTS[@]}"; do
        if ! "${IPT[@]}" -S "$CHAIN" 2>/dev/null | grep -q "WDTT_PROBE_CNT_$p"; then
            rebuild_chain
            return 1
        fi
    done
    return 0
}

read_counter() {  # порт -> «пакеты байты», пусто если правила нет
    "${IPT[@]}" -L "$CHAIN" -v -n -x 2>/dev/null \
        | awk -v c="WDTT_PROBE_CNT_$1" '$0 ~ c {print $1, $2; exit}'
}

prev_of() {  # порт -> «пакеты байты» с прошлого запуска
    [ -f "$STATE" ] || return 0
    awk -v p="$1" '$1 == p {print $2, $3; exit}' "$STATE"
}

ensure_rules
rebuilt=$?

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
out="$LOGDIR/$(date -u +%Y%m%d).log"
tmp_state="$(mktemp "$STATEDIR/.state.XXXXXX")"

for p in "${PORTS[@]}"; do
    read -r cur_pkt cur_byte <<<"$(read_counter "$p")"
    [ -n "${cur_pkt:-}" ] || continue
    read -r old_pkt old_byte <<<"$(prev_of "$p")"

    reset=$rebuilt
    if [ -z "${old_pkt:-}" ]; then
        # Первый запуск: прироста ещё нет, но запись нужна — она отмечает,
        # с какого момента истории можно верить.
        d_pkt=0; d_byte=0; reset=1
    elif [ "$cur_pkt" -lt "$old_pkt" ]; then
        d_pkt=$cur_pkt; d_byte=$cur_byte; reset=1
    else
        d_pkt=$(( cur_pkt - old_pkt ))
        d_byte=$(( cur_byte - old_byte ))
    fi

    echo "$now port=$p pkt=$d_pkt byte=$d_byte reset=$reset" >>"$out"
    echo "$p $cur_pkt $cur_byte" >>"$tmp_state"
done

mv -f "$tmp_state" "$STATE"
find "$LOGDIR" -maxdepth 1 -name '*.log' -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
exit 0
