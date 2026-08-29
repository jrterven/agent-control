from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class UnsafeEndpointError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    allow_loopback: bool = False
    allow_private: bool = False
    allowed_schemes: frozenset[str] = frozenset({"http", "https", "ws", "wss"})
    allowed_ports: frozenset[int] | None = None


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "100.100.100.200/32",
        "192.0.0.192/32",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "fe80::/10",
        "fd00:ec2::254/128",
        "ff00::/8",
    )
)


def _validate_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address, policy: EndpointPolicy) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # IPv4-mapped IPv6 inherits every IPv4 policy decision; otherwise
        # metadata and Tailscale-CGNAT ranges can be represented around guards.
        _validate_ip(address.ipv4_mapped, policy)
        return
    tailscale_cgnat = ipaddress.ip_network("100.64.0.0/10")
    if address in tailscale_cgnat and policy.allow_private and str(address) != "100.100.100.200":
        return
    if any(address in network for network in _BLOCKED_NETWORKS):
        raise UnsafeEndpointError("Endpoint resolves to a prohibited network")
    if address.is_loopback and not policy.allow_loopback:
        raise UnsafeEndpointError("Loopback endpoints require explicit private mode")
    if address.is_private and not address.is_loopback and not policy.allow_private:
        raise UnsafeEndpointError("Private endpoints require explicit private mode")
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        raise UnsafeEndpointError("Endpoint address is not routable")


async def validate_endpoint(
    url: str,
    policy: EndpointPolicy,
    *,
    resolver=None,
) -> str:
    """Validate URL syntax and every DNS result before making a request.

    Callers must repeat this validation for redirects. The function returns the
    normalized original URL only after all answers satisfy the policy.
    """

    return (await resolve_endpoint(url, policy, resolver=resolver)).url


async def resolve_endpoint(
    url: str,
    policy: EndpointPolicy,
    *,
    resolver=None,
) -> ResolvedEndpoint:
    """Validate and return the exact IP set that transports must pin."""

    parts = urlsplit(url)
    if parts.scheme.lower() not in policy.allowed_schemes:
        raise UnsafeEndpointError("Unsupported endpoint scheme")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeEndpointError("Endpoint must have a hostname and no userinfo")
    try:
        port = parts.port or (443 if parts.scheme in {"https", "wss"} else 80)
    except ValueError as exc:
        raise UnsafeEndpointError("Invalid endpoint port") from exc
    if policy.allowed_ports is not None and port not in policy.allowed_ports:
        raise UnsafeEndpointError("Endpoint port is not allowed")

    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(parts.hostname)))
    except ValueError:
        if resolver is None:
            loop = __import__("asyncio").get_running_loop()
            answers = await loop.getaddrinfo(
                parts.hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
            addresses.update(answer[4][0] for answer in answers)
        else:
            resolved = await resolver(parts.hostname, port)
            addresses.update(str(item) for item in resolved)
    if not addresses:
        raise UnsafeEndpointError("Endpoint hostname did not resolve")
    for raw in addresses:
        _validate_ip(ipaddress.ip_address(raw), policy)
    return ResolvedEndpoint(
        url=url,
        hostname=parts.hostname,
        port=port,
        addresses=tuple(sorted(addresses)),
    )
