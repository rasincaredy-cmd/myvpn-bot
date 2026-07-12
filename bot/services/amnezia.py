"""Установка, удаление и peer-менеджмент AmneziaWG на удалённом VPS через SSH."""
from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from loguru import logger

from bot.services.ssh import SSHClient, SSHError

WG_INTERFACE = "awg0"
WG_CONF_DIR = "/etc/amnezia/amneziawg"
WG_CONF_PATH = f"{WG_CONF_DIR}/{WG_INTERFACE}.conf"

# H1..H4 ОБЯЗАНЫ отличаться от magic-чисел WireGuard handshake (1..4),
# иначе обфускация ломает рукопожатие.
_FORBIDDEN_H = {1, 2, 3, 4}

ProgressCb = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class AmneziaParams:
    """Параметры обфускации AmneziaWG. Без них AWG == обычный WireGuard."""

    Jc: int
    Jmin: int
    Jmax: int
    S1: int
    S2: int
    H1: int
    H2: int
    H3: int
    H4: int

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "AmneziaParams":
        return cls(**json.loads(raw))

    def to_interface_block(self) -> str:
        return (
            f"Jc = {self.Jc}\n"
            f"Jmin = {self.Jmin}\n"
            f"Jmax = {self.Jmax}\n"
            f"S1 = {self.S1}\n"
            f"S2 = {self.S2}\n"
            f"H1 = {self.H1}\n"
            f"H2 = {self.H2}\n"
            f"H3 = {self.H3}\n"
            f"H4 = {self.H4}\n"
        )


def generate_amnezia_params() -> AmneziaParams:
    Jmin = secrets.randbelow(31) + 40           # 40..70
    Jmax = Jmin + secrets.randbelow(91) + 10    # Jmin+10..Jmin+100

    def _h() -> int:
        while True:
            v = secrets.randbelow(2_000_000_000) + 5
            if v not in _FORBIDDEN_H:
                return v

    h = set()
    while len(h) < 4:
        h.add(_h())
    H1, H2, H3, H4 = list(h)

    return AmneziaParams(
        Jc=secrets.randbelow(8) + 3,            # 3..10
        Jmin=Jmin,
        Jmax=Jmax,
        S1=secrets.randbelow(136) + 15,         # 15..150
        S2=secrets.randbelow(136) + 15,
        H1=H1, H2=H2, H3=H3, H4=H4,
    )


@dataclass(slots=True)
class InstallResult:
    server_public_key: str
    params: AmneziaParams
    endpoint: str
    interface: str
    subnet: str


# --- Install ----------------------------------------------------------------

async def _detect_default_iface(ssh: SSHClient) -> str:
    res = await ssh.run(
        "ip -o -4 route show to default | awk '{print $5}' | head -n1",
        check=True,
    )
    iface = res.stdout.strip()
    if not iface:
        raise SSHError("Не удалось определить дефолтный сетевой интерфейс")
    return iface


async def _ensure_apt_ready(ssh: SSHClient, progress: ProgressCb) -> None:
    await progress("Обновляю список пакетов apt...")
    await ssh.run("DEBIAN_FRONTEND=noninteractive apt-get update -y", check=True)
    await ssh.run(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "software-properties-common ca-certificates curl gnupg lsb-release "
        "iptables iptables-persistent qrencode",
        check=True,
    )


async def _install_amneziawg(ssh: SSHClient, progress: ProgressCb) -> None:
    await progress("Подключаю PPA <code>amnezia/ppa</code>...")
    await ssh.run(
        "add-apt-repository -y ppa:amnezia/ppa && "
        "DEBIAN_FRONTEND=noninteractive apt-get update -y",
        check=True,
    )
    # Headers под текущее ядро нужны DKMS, чтобы собрать модуль amneziawg.
    # На HWE-ядрах apt не подтягивает их сам — ставим явно.
    await progress("Ставлю заголовки ядра и DKMS...")
    await ssh.run(
        'DEBIAN_FRONTEND=noninteractive apt-get install -y '
        'dkms build-essential "linux-headers-$(uname -r)"',
        check=True,
    )
    await progress("Ставлю <code>amneziawg</code> + <code>amneziawg-tools</code>...")
    await ssh.run(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "amneziawg amneziawg-tools",
        check=True,
    )
    # Форсим сборку модуля под текущее ядро — если DKMS не собрал на этапе
    # установки пакета (headers подтянулись позже), это починит ситуацию.
    await progress("Собираю модуль ядра amneziawg через DKMS...")
    await ssh.run('dkms autoinstall -k "$(uname -r)" || true')
    res = await ssh.run("modprobe amneziawg")
    if not res.ok:
        raise SSHError(
            "Модуль ядра amneziawg не загрузился. "
            "Проверь: dkms status; uname -r; что доступны headers под это ядро. "
            f"stderr: {res.stderr.strip()[:500]}"
        )


async def _enable_ip_forward(ssh: SSHClient, progress: ProgressCb) -> None:
    await progress("Включаю IP-forwarding...")
    await ssh.run(
        "grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf "
        "&& sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf "
        "|| echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf",
        check=True,
    )
    await ssh.run("sysctl -p", check=True)


async def _generate_server_keys(ssh: SSHClient, progress: ProgressCb) -> tuple[str, str]:
    await progress("Генерирую серверные ключи AmneziaWG...")
    await ssh.run(f"mkdir -p {WG_CONF_DIR} && chmod 700 {WG_CONF_DIR}", check=True)
    await ssh.run(
        f"sh -c 'umask 077 && awg genkey | tee {WG_CONF_DIR}/server.key "
        f"| awg pubkey > {WG_CONF_DIR}/server.pub'",
        check=True,
    )
    priv = (await ssh.run(f"cat {WG_CONF_DIR}/server.key", check=True)).stdout.strip()
    pub = (await ssh.run(f"cat {WG_CONF_DIR}/server.pub", check=True)).stdout.strip()
    if not priv or not pub:
        raise SSHError("Не удалось сгенерировать серверные ключи")
    return priv, pub


def _build_server_conf(
    *,
    server_priv: str,
    wg_port: int,
    subnet_addr: str,
    iface_out: str,
    params: AmneziaParams,
) -> str:
    return (
        "[Interface]\n"
        f"Address = {subnet_addr}\n"
        f"ListenPort = {wg_port}\n"
        f"PrivateKey = {server_priv}\n"
        f"{params.to_interface_block()}"
        f"PostUp = iptables -A FORWARD -i %i -j ACCEPT; "
        f"iptables -A FORWARD -o %i -j ACCEPT; "
        f"iptables -t nat -A POSTROUTING -o {iface_out} -j MASQUERADE\n"
        f"PostDown = iptables -D FORWARD -i %i -j ACCEPT; "
        f"iptables -D FORWARD -o %i -j ACCEPT; "
        f"iptables -t nat -D POSTROUTING -o {iface_out} -j MASQUERADE\n"
    )


async def _write_server_conf(ssh: SSHClient, conf: str) -> None:
    quoted = conf.replace("'", "'\\''")
    await ssh.run(
        f"umask 077 && printf '%s' '{quoted}' > {WG_CONF_PATH}",
        check=True,
    )
    await ssh.run(f"chmod 600 {WG_CONF_PATH}", check=True)


async def _open_firewall(ssh: SSHClient, wg_port: int, progress: ProgressCb) -> None:
    await progress(f"Открываю порт {wg_port}/udp в UFW (если активен)...")
    await ssh.run(
        f"ufw status >/dev/null 2>&1 && ufw allow {wg_port}/udp || true"
    )
    await ssh.run("netfilter-persistent save || iptables-save > /etc/iptables/rules.v4 || true")


async def _bring_up(ssh: SSHClient, progress: ProgressCb) -> None:
    await progress("Поднимаю интерфейс <code>awg0</code>...")
    await ssh.run(f"awg-quick down {WG_INTERFACE} 2>/dev/null || true")
    await ssh.run(f"awg-quick up {WG_INTERFACE}", check=True)
    await ssh.run(f"systemctl enable awg-quick@{WG_INTERFACE}", check=True)


async def install_amneziawg(
    ssh: SSHClient,
    *,
    host: str,
    wg_port: int,
    subnet: str = "10.8.0.0/24",
    progress: ProgressCb,
) -> InstallResult:
    """Полный сценарий установки AmneziaWG на чистую Ubuntu 22.04+/Debian."""
    os_release = await ssh.run("cat /etc/os-release", check=True)
    if "ubuntu" not in os_release.stdout.lower() and "debian" not in os_release.stdout.lower():
        raise SSHError("Поддерживаются только Ubuntu/Debian-based дистрибутивы")

    iface_out = await _detect_default_iface(ssh)
    logger.info("Default iface on remote: {}", iface_out)

    await _ensure_apt_ready(ssh, progress)
    await _install_amneziawg(ssh, progress)
    await _enable_ip_forward(ssh, progress)

    server_priv, server_pub = await _generate_server_keys(ssh, progress)
    params = generate_amnezia_params()

    network, _, mask = subnet.partition("/")
    octets = network.split(".")
    octets[3] = "1"
    server_addr = f"{'.'.join(octets)}/{mask}"

    conf = _build_server_conf(
        server_priv=server_priv,
        wg_port=wg_port,
        subnet_addr=server_addr,
        iface_out=iface_out,
        params=params,
    )
    await progress("Пишу серверный конфиг...")
    await _write_server_conf(ssh, conf)
    await _open_firewall(ssh, wg_port, progress)
    await _bring_up(ssh, progress)

    res = await ssh.run(f"awg show {WG_INTERFACE}")
    if not res.ok:
        raise SSHError(f"Интерфейс {WG_INTERFACE} не поднялся: {res.stderr}")

    return InstallResult(
        server_public_key=server_pub,
        params=params,
        endpoint=f"{host}:{wg_port}",
        interface=WG_INTERFACE,
        subnet=subnet,
    )


# --- Uninstall --------------------------------------------------------------

async def uninstall_amneziawg(
    ssh: SSHClient, *, wg_port: int, progress: ProgressCb
) -> list[str]:
    """Снимает с сервера всё, что поставила install_amneziawg.

    Намеренно best-effort — каждая команда независима, ошибка одной не
    останавливает остальные. Возвращает список нефатальных предупреждений.
    """
    warnings: list[str] = []

    async def _step(label: str, cmd: str) -> None:
        await progress(label)
        res = await ssh.run(cmd)
        if not res.ok and res.stderr.strip():
            warnings.append(f"{label} — {res.stderr.strip()[:200]}")

    await _step(
        "Останавливаю awg0...",
        f"awg-quick down {WG_INTERFACE} 2>/dev/null || true",
    )
    await ssh.run(f"systemctl disable awg-quick@{WG_INTERFACE} 2>/dev/null || true")

    await _step(
        "Удаляю пакеты amneziawg*...",
        "DEBIAN_FRONTEND=noninteractive apt-get purge -y "
        "amneziawg amneziawg-tools amneziawg-dkms 2>&1 || true",
    )

    await _step(
        "Удаляю конфиги...",
        f"rm -rf {WG_CONF_DIR}",
    )

    await _step(
        f"Закрываю порт {wg_port}/udp в UFW...",
        f"ufw status >/dev/null 2>&1 && ufw delete allow {wg_port}/udp || true",
    )
    await ssh.run("netfilter-persistent save 2>/dev/null || true")

    return warnings


# --- Peers ------------------------------------------------------------------

@dataclass(slots=True)
class PeerKeys:
    private_key: str
    public_key: str


async def generate_peer_keys(ssh: SSHClient) -> PeerKeys:
    res = await ssh.run(
        "sh -c 'umask 077 && priv=$(awg genkey) && pub=$(echo \"$priv\" | awg pubkey)"
        " && echo \"$priv\" && echo \"$pub\"'",
        check=True,
    )
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) != 2:
        raise SSHError("Не удалось сгенерировать ключи peer'а")
    return PeerKeys(private_key=lines[0], public_key=lines[1])


async def add_peer_on_server(
    ssh: SSHClient, *, public_key: str, peer_ip: str
) -> None:
    await ssh.run(
        f"awg set {WG_INTERFACE} peer {public_key} allowed-ips {peer_ip}/32",
        check=True,
    )
    await ssh.run(f"awg-quick save {WG_INTERFACE}", check=True)


async def remove_peer_on_server(ssh: SSHClient, *, public_key: str) -> None:
    await ssh.run(
        f"awg set {WG_INTERFACE} peer {public_key} remove",
        check=True,
    )
    await ssh.run(f"awg-quick save {WG_INTERFACE}", check=True)


async def list_used_ips(ssh: SSHClient, subnet_cidr: str) -> set[str]:
    res = await ssh.run(f"awg show {WG_INTERFACE} allowed-ips", check=False)
    used: set[str] = set()
    for line in res.stdout.splitlines():
        ips = re.findall(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
        used.update(ips)
    network, _, _ = subnet_cidr.partition("/")
    octets = network.split(".")
    octets[3] = "1"
    used.add(".".join(octets))
    return used


def next_free_ip(subnet_cidr: str, used: set[str]) -> str:
    network, _, _ = subnet_cidr.partition("/")
    octets = network.split(".")
    base = ".".join(octets[:3])
    for last in range(2, 255):
        candidate = f"{base}.{last}"
        if candidate not in used:
            return candidate
    raise SSHError("Подсеть исчерпана — освободи peer'ов или расширь /24")


def build_peer_conf(
    *,
    peer_private_key: str,
    peer_ip: str,
    server_public_key: str,
    endpoint: str,
    params: AmneziaParams,
    dns: str = "1.1.1.1, 1.0.0.1",
) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {peer_private_key}\n"
        f"Address = {peer_ip}/32\n"
        f"DNS = {dns}\n"
        f"{params.to_interface_block()}\n"
        "[Peer]\n"
        f"PublicKey = {server_public_key}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        f"Endpoint = {endpoint}\n"
        "PersistentKeepalive = 25\n"
    )
# --- Monitoring -------------------------------------------------------------

def fmt_bytes(n: int) -> str:
    """Форматирует байты в читаемый вид (B → TB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0  # type: ignore[assignment]
    return f"{n:.1f} TB"


def fmt_traffic_line(used_bytes: int, limit_bytes: int | None, expired: bool) -> str:
    """Строка трафика для карточек подписки. При истёкшей подписке показываем как
    полностью израсходовано (лимит/лимит), а безлимит — как «исчерпан»,
    чтобы не было странного «безлимит» рядом с «истекла»."""
    if limit_bytes is None:
        return "исчерпан" if expired else "безлимит"
    total = fmt_bytes(limit_bytes)
    if expired:
        return f"{total} из {total} (исчерпан)"
    return f"{fmt_bytes(used_bytes)} из {total}"


@dataclass(slots=True)
class PeerTrafficInfo:
    public_key: str
    rx_bytes: int          # байты, принятые сервером от пира (= upload пира)
    tx_bytes: int          # байты, отправленные сервером пиру  (= download пира)
    last_handshake_ts: int # unix-timestamp; 0 = ни разу не подключался


def accumulate_traffic(prev_used: int, prev_raw: int, cur_raw: int) -> tuple[int, int]:
    """Накопление трафика с защитой от сброса счётчика awg.

    `awg show transfer` считает байты с момента поднятия интерфейса; после ребута
    VPS или `awg-quick down/up` счётчик обнуляется. Если текущее сырое значение
    меньше предыдущего — это сброс, и мы добавляем весь `cur_raw` как новую дельту.

    Возвращает (новый_накопленный, новое_сырое_значение).
    """
    delta = cur_raw - prev_raw
    if delta < 0:            # счётчик обнулился → считаем с нуля
        delta = cur_raw
    return prev_used + delta, cur_raw


async def get_peer_traffic(
    ssh: SSHClient, interface: str = WG_INTERFACE
) -> list[PeerTrafficInfo]:
    """awg show transfer + latest-handshakes → список по всем пирам интерфейса."""
    transfer_res   = await ssh.run(f"awg show {interface} transfer")
    handshake_res  = await ssh.run(f"awg show {interface} latest-handshakes")

    hs_map: dict[str, int] = {}
    for line in handshake_res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                hs_map[parts[0]] = int(parts[1])
            except ValueError:
                pass

    result: list[PeerTrafficInfo] = []
    for line in transfer_res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            pub, rx, tx = parts
            try:
                result.append(
                    PeerTrafficInfo(
                        public_key=pub,
                        rx_bytes=int(rx),
                        tx_bytes=int(tx),
                        last_handshake_ts=hs_map.get(pub, 0),
                    )
                )
            except ValueError:
                pass
    return result


@dataclass(slots=True)
class ServerStats:
    uptime: str
    load_1: float
    load_5: float
    load_15: float
    cpu_count: int
    ram_used_mb: int
    ram_total_mb: int
    disk_used_gb: float
    disk_total_gb: float


async def get_server_stats(ssh: SSHClient) -> ServerStats:
    """CPU/RAM/диск/uptime одной составной командой без sleep."""
    cmd = (
        "echo '---UPTIME---'; uptime -p; "
        "echo '---LOAD---'; cat /proc/loadavg; "
        "echo '---CPUS---'; nproc; "
        "echo '---RAM---'; free -m | awk 'NR==2{print $2, $3}'; "
        "echo '---DISK---'; "
        "df -BG / | awk 'NR==2{gsub(/G/,\"\",$2); gsub(/G/,\"\",$3); print $2, $3}'"
    )
    res = await ssh.run(cmd)

    sections: dict[str, str] = {}
    current = ""
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("---") and stripped.endswith("---"):
            current = stripped.strip("-")
        elif current and stripped:
            sections[current] = stripped

    def _f(key: str, idx: int = 0) -> float:
        try:
            return float(sections.get(key, "").split()[idx].replace(",", "."))
        except (IndexError, ValueError):
            return 0.0

    def _i(key: str, idx: int = 0) -> int:
        try:
            return int(sections.get(key, "").split()[idx])
        except (IndexError, ValueError):
            return 0

    load_raw = sections.get("LOAD", "")
    load_parts = load_raw.split()
    return ServerStats(
        uptime=sections.get("UPTIME", "—"),
        load_1=_f("LOAD", 0),
        load_5=_f("LOAD", 1),
        load_15=_f("LOAD", 2),
        cpu_count=_i("CPUS"),
        ram_total_mb=_i("RAM", 0),
        ram_used_mb=_i("RAM", 1),
        disk_total_gb=_f("DISK", 0),
        disk_used_gb=_f("DISK", 1),
)
