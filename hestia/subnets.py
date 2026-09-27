"""Addresses for per-tenant Docker networks. Pure functions, no docker calls.

Every image tenant gets its own Internal bridge. Letting Docker pick the subnet
cost two things. First, each tenant took a whole default pool (/16 or /20), and
a default engine has about 30 of them, which is fewer than HESTIA_MAX_TENANTS.
Second, a subnet freed by one tenant's `network rm` went back to the engine-wide
pool, so the next network created by anyone on that engine could get the same
addresses. A ledger row still pointing at the old IP would then make the public
edge proxy to a stranger's container.

So Hestia allocates small subnets itself, out of a range it owns
(HESTIA_TENANT_SUBNET_POOL). It never hands out a subnet that still holds an
address a live ledger row points at.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable

DEFAULT_TENANT_SUBNET_POOL = "10.231.0.0/16"
# /29 = network, gateway (the host), the tenant, and a few spare addresses.
TENANT_SUBNET_PREFIXLEN = 29
# Linux interface names are at most 15 characters. Every tenant bridge is named
# `hst` + 12 hex characters so one host firewall rule (`-i hst+`) covers them all.
TENANT_BRIDGE_PREFIX = "hst"
NETWORK_PREFIX_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}")

_NON_POOL_NETS = tuple(
    ipaddress.ip_network(net)
    for net in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4")
)


def tenant_bridge_name(network_name: str) -> str:
    """Kernel interface name for a tenant bridge: `hst` + 12 hex characters."""
    digest = hashlib.sha256(network_name.encode()).hexdigest()[:12]
    return f"{TENANT_BRIDGE_PREFIX}{digest}"


def parse_subnet_pool(value: str) -> ipaddress.IPv4Network:
    """Validate HESTIA_TENANT_SUBNET_POOL. Only private IPv4 ranges are accepted."""
    raw = (value or "").strip()
    try:
        pool = ipaddress.ip_network(raw, strict=True)
    except ValueError as exc:
        raise RuntimeError(
            f"HESTIA_TENANT_SUBNET_POOL must be an IPv4 network such as "
            f"{DEFAULT_TENANT_SUBNET_POOL}: {exc}"
        ) from exc
    if not isinstance(pool, ipaddress.IPv4Network):
        raise RuntimeError("HESTIA_TENANT_SUBNET_POOL must be IPv4; tenant networks have IPv6 off")
    if pool.prefixlen > TENANT_SUBNET_PREFIXLEN:
        raise RuntimeError(
            f"HESTIA_TENANT_SUBNET_POOL must be /{TENANT_SUBNET_PREFIXLEN} or larger"
        )
    if not pool.is_private or any(pool.overlaps(net) for net in _NON_POOL_NETS):
        raise RuntimeError(
            "HESTIA_TENANT_SUBNET_POOL must be a private range (for example 10.x.0.0/16), "
            "not loopback, link-local, multicast, reserved or public addresses"
        )
    return pool


def pool_capacity(pool: ipaddress.IPv4Network) -> int:
    return 2 ** (TENANT_SUBNET_PREFIXLEN - pool.prefixlen)


def _addresses(hosts: Iterable[str]) -> list[ipaddress.IPv4Address]:
    out: list[ipaddress.IPv4Address] = []
    for host in hosts:
        try:
            address = ipaddress.ip_address((host or "").strip())
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address):
            out.append(address)
    return out


def pick_tenant_subnet(
    pool: ipaddress.IPv4Network,
    *,
    taken: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
    reserved_hosts: Iterable[str] = (),
    skip: Iterable[ipaddress.IPv4Network] = (),
) -> ipaddress.IPv4Network:
    """The lowest free /29 in the pool.

    Not free: overlaps any network the engine already has (`taken`), contains an
    address a live ledger row still points at (`reserved_hosts`), or was already
    refused on this attempt (`skip`).
    """
    taken_v4 = [net for net in taken if isinstance(net, ipaddress.IPv4Network)]
    reserved = _addresses(reserved_hosts)
    skipped = set(skip)
    for candidate in pool.subnets(new_prefix=TENANT_SUBNET_PREFIXLEN):
        if candidate in skipped:
            continue
        if any(candidate.overlaps(net) for net in taken_v4):
            continue
        if any(address in candidate for address in reserved):
            continue
        return candidate
    raise RuntimeError(
        f"tenant subnet pool {pool} is exhausted ({pool_capacity(pool)} /"
        f"{TENANT_SUBNET_PREFIXLEN} networks); widen HESTIA_TENANT_SUBNET_POOL"
    )


def subnet_in_pool(subnet: str, pool: ipaddress.IPv4Network) -> bool:
    try:
        net = ipaddress.ip_network((subnet or "").strip(), strict=True)
    except ValueError:
        return False
    return (
        isinstance(net, ipaddress.IPv4Network)
        and net.prefixlen == TENANT_SUBNET_PREFIXLEN
        and net.subnet_of(pool)
    )
