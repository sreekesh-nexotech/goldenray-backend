"""On-site discovery of the office's terminals on its LAN (``python -m essl_agent discover``).

An address is not an identity: a terminal is registered on the platform from the serial (and MAC) printed on its
label, and the address is found here by the agent that can see the LAN — every host answering on the ZK port is asked
to state its own serial and MAC (from the terminal's answer, not an ARP table). The platform decides which registered
device, if any, that is. Read-only in every sense; one port, and only a private network this machine is part of
(at most 4096 addresses) — it is not a port scanner and must not become one.
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from essl_agent.identity import display_mac, normalize_serial
from essl_agent.zk_reader import DeviceError, read_info

DEFAULT_WORKERS = 64
DEFAULT_CONNECT_TIMEOUT = 0.4
MAX_ADDRESSES = 4096


def local_ipv4() -> str | None:
    """This machine's address on the network it routes through (a UDP connect sends nothing)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


def default_gateway() -> str | None:
    """Best-effort default gateway, recorded as evidence about the LAN."""
    commands = (
        ["powershell", "-NoProfile", "-Command", "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
        ["ip", "route", "show", "default"],
    )
    for command in commands:
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        for token in (out.stdout or "").replace("\n", " ").split():
            try:
                address = ipaddress.ip_address(token)
            except ValueError:
                continue
            if address.version == 4 and not address.is_unspecified:
                return str(address)
    return None


def network_for(ip: str, prefix: int = 24) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(f"{ip}/{prefix}", strict=False)


def _port_open(ip: str, port: int, timeout: float) -> str | None:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return ip
    except OSError:
        return None


def scan_port(hosts: Iterable[str], port: int = 4370, timeout: float = DEFAULT_CONNECT_TIMEOUT, workers: int = DEFAULT_WORKERS) -> list[str]:
    hosts = list(hosts)
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
        results = pool.map(lambda host: _port_open(host, port, timeout), hosts)
    return [ip for ip in results if ip]


def identify(ip: str, port: int = 4370, password: int = 0, timeout: int = 10) -> dict[str, Any]:
    """Ask one terminal who it is; a host that is not a ZK terminal is reported with its error, never guessed at."""
    host: dict[str, Any] = {"ip_address": ip, "port": port}
    try:
        info = read_info(ip, port, password, timeout)
    except DeviceError as exc:
        host["error"] = str(exc)[:500]
        return host
    host.update(
        serial_number=normalize_serial(info.get("serial_number")),
        mac_address=display_mac(info.get("mac_address")) or info.get("mac_address"),
        device_name=info.get("device_name"),
        firmware_version=info.get("firmware_version"),
        platform=info.get("platform"),
    )
    return host


def targets_for(subnet: str | None, agent_ip: str | None, prefix: int = 24) -> tuple[ipaddress.IPv4Network, list[str]]:
    if subnet:
        network = ipaddress.ip_network(subnet, strict=False)
    elif agent_ip:
        network = network_for(agent_ip, prefix)
    else:
        raise RuntimeError("No local IPv4 address could be determined. Pass --subnet with the office network, e.g. --subnet 192.168.5.0/24.")
    if not network.is_private:
        raise RuntimeError(f"{network} is not a private network: discovery only scans the office LAN.")
    if network.num_addresses > MAX_ADDRESSES:
        raise RuntimeError(f"{network} holds {network.num_addresses} addresses. Narrow it with --subnet: an office LAN is normally a /24.")
    return network, [str(host) for host in network.hosts()]


def discover(
    subnet: str | None = None,
    port: int = 4370,
    password: int = 0,
    prefix: int = 24,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: int = 10,
    workers: int = DEFAULT_WORKERS,
    hosts: Iterable[str] | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Scan one LAN and ask each terminal what it is. Returns the evidence, not a conclusion."""
    agent_ip = local_ipv4()
    if hosts is not None:
        network, targets = None, [str(host) for host in hosts]
    else:
        network, targets = targets_for(subnet, agent_ip, prefix)
    progress = on_progress or (lambda message: None)
    progress(f"scanning {network or f'{len(targets)} host(s)'} on TCP {port}")
    open_hosts = scan_port(targets, port, timeout, workers)
    progress(f"{len(open_hosts)} host(s) answering on {port}: " + (", ".join(open_hosts) or "none"))
    found = []
    for ip in open_hosts:
        progress(f"identifying {ip}")
        found.append(identify(ip, port, password, read_timeout))
    return {"subnet": str(network) if network else None, "agent_ip": agent_ip, "gateway": default_gateway(), "hosts_scanned": len(targets), "hosts_open": len(open_hosts), "found": found}
