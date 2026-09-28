"""The one client-IP resolver. Throttles, lockouts, audit and lead capture all use :func:`get_client_ip`.

Rules (standard §3.1, §3.3):

* ``X-Forwarded-For`` is honoured only when the direct peer (``REMOTE_ADDR``) is a trusted proxy listed in
  ``settings.TRUSTED_PROXIES`` (CIDR list). A client talking to us directly cannot spoof its address.
* The header is walked right-to-left, skipping trusted proxies; the first untrusted hop is the client. Anything to
  the left of that hop was supplied by the client and is ignored.
* A malformed hop stops the walk; we then fall back to the last address a trusted proxy vouched for.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from django.conf import settings

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_CACHE_ATTR = "_flarize_client_ip"


@lru_cache(maxsize=32)
def parse_networks(cidrs: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    """Parse a CIDR list. Raises ``ValueError`` on an invalid entry (prod settings validation relies on it)."""
    return tuple(ipaddress.ip_network(cidr.strip(), strict=False) for cidr in cidrs if cidr and cidr.strip())


def trusted_networks() -> tuple[IPNetwork, ...]:
    return parse_networks(tuple(getattr(settings, "TRUSTED_PROXIES", ()) or ()))


def parse_ip(value: str | None) -> IPAddress | None:
    """Parse one address, tolerating ``ip:port`` and ``[ipv6]:port`` forms some proxies emit."""
    if not value:
        return None
    candidate = value.strip().strip('"')
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def is_trusted(address: IPAddress | None, networks: tuple[IPNetwork, ...] | None = None) -> bool:
    if address is None:
        return False
    networks = trusted_networks() if networks is None else networks
    return any(address.version == network.version and address in network for network in networks)


def resolve_client_ip(remote_addr: str | None, forwarded_for: str | None, networks: tuple[IPNetwork, ...] | None = None) -> str:
    """Pure resolver used by :func:`get_client_ip` (and directly by tests)."""
    networks = trusted_networks() if networks is None else networks
    peer = parse_ip(remote_addr)
    if peer is None:
        return ""
    if not is_trusted(peer, networks) or not forwarded_for:
        return str(peer)
    vouched = peer
    for hop in reversed([part for part in forwarded_for.split(",") if part.strip()]):
        address = parse_ip(hop)
        if address is None:
            return str(vouched)
        if not is_trusted(address, networks):
            return str(address)
        vouched = address
    return str(vouched)


def get_client_ip(request) -> str:
    """Client IP for a Django ``HttpRequest`` or DRF ``Request`` (memoised on the request)."""
    django_request = getattr(request, "_request", request)
    cached = getattr(django_request, _CACHE_ATTR, None)
    if cached is not None:
        return cached
    meta = django_request.META
    ip = resolve_client_ip(meta.get("REMOTE_ADDR"), meta.get("HTTP_X_FORWARDED_FOR"))
    setattr(django_request, _CACHE_ATTR, ip)
    return ip
