"""Durchreichen an den Airable-Uebergangsdienst, fuer alles, was das Portal nicht selbst kennt.

Im Menue des Radios kommt Airable nicht mehr vor. Durchgereicht werden nur noch Anfragen, die
das Portal nicht beantwortet, etwa alte FAV-Eintraege mit Airable-IDs, solange es Airable gibt."""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
from typing import NamedTuple

DOMAIN = "wifiradiofrontier.com"
MAX_BODY = 4 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
       "upgrade", "proxy-authenticate", "proxy-authorization"}
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def is_radio_host(host: str) -> bool:
    """Nur Namen unter wifiradiofrontier.com werden bedient oder durchgereicht."""
    host = host.lower().rstrip(".")
    return (host == DOMAIN or host.endswith("." + DOMAIN)) and bool(_HOSTNAME.match(host))


class UpstreamError(Exception):
    pass


class Upstream(NamedTuple):
    status: int
    reason: str
    headers: list
    body: bytes


def _system_resolve(host: str) -> str:
    # Auf dem Server fragt der Resolver des Systems die FRITZ!Box, nicht den eigenen dnsmasq.
    infos = socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)
    return infos[0][4][0]


class Forwarder:
    def __init__(self, timeout: float = 5.0, pi_ip: str = "", port: int = 80,
                 resolve=None, refuse_loopback: bool = True):
        self.timeout = timeout
        self.pi_ip = pi_ip
        self.port = port
        self.resolve = resolve or _system_resolve
        self.refuse_loopback = refuse_loopback

    def forward(self, method: str, host: str, target: str, headers, body: bytes = b"",
                timeout: float | None = None) -> Upstream:
        host = host.lower()
        if not is_radio_host(host):
            raise UpstreamError(f"Host {host!r} wird nicht durchgereicht")
        try:
            ip = self.resolve(host)
        except OSError as e:
            raise UpstreamError(f"{host} nicht aufloesbar: {e}") from e
        if ip == self.pi_ip or (self.refuse_loopback and ipaddress.ip_address(ip).is_loopback):
            raise UpstreamError(f"{host} loest auf {ip} auf (eigener Rechner), keine Schleife")
        out = {k: v for k, v in headers
               if k.lower() not in HOP and k.lower() not in ("host", "content-length")}
        out["Host"] = host
        out["Connection"] = "close"
        conn = http.client.HTTPConnection(ip, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, target, body=body or None, headers=out)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise UpstreamError("Antwort zu gross")
            return Upstream(resp.status, resp.reason, resp.getheaders(), data)
        except (OSError, http.client.HTTPException) as e:
            raise UpstreamError(f"{host} ({ip}): {e}") from e
        finally:
            conn.close()
