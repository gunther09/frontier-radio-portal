"""Podcast-Folgen: Weiterleitungsketten aufloesen und https-Quellen als http weitergeben.

Das Radio folgt hoechstens einer Weiterleitung und kann kein https. Die Weiche loest die
Kette deshalb selbst auf: Laeuft sie nur ueber http, bekommt das Radio die Endadresse
direkt; kommt irgendwo https vor, holt der Server die Folge und reicht sie als http weiter
(`/portal/stream/<id>.mp3`)."""

from __future__ import annotations

import http.client
import ipaddress
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import NamedTuple

from . import __version__

REDIRECTS = (301, 302, 303, 307, 308)
MAX_HOPS = 8
USER_AGENT = f"Mozilla/5.0 (compatible; frontier-radio-portal/{__version__})"
_QUOTE_SAFE = "/%:@!$&'()*+,;=-._~?"


class StreamError(Exception):
    pass


class Opened(NamedTuple):
    conn: http.client.HTTPConnection
    resp: http.client.HTTPResponse
    url: str          # Adresse der letzten Antwort
    hops: list        # alle besuchten Adressen, die erste zuerst


class Probe(NamedTuple):
    final_url: str
    needs_relay: bool  # irgendwo in der Kette steht https
    status: int
    content_type: str


def _public_ip(host: str, port: int, allow_private: bool) -> str:
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as e:
        raise StreamError(f"{host} nicht aufloesbar: {e}") from e
    for info in infos:
        ip = info[4][0]
        if allow_private or ipaddress.ip_address(ip).is_global:
            return ip
    raise StreamError(f"{host} zeigt nicht ins oeffentliche Internet")


class Streamer:
    def __init__(self, timeout: float = 15.0, allow_private: bool = False):
        self.timeout = timeout
        # Nur fuer Tests. Sonst darf nichts aus dem eigenen Netz geholt werden.
        self.allow_private = allow_private
        self._lock = threading.Lock()
        self._probes: dict[str, tuple[float, Probe]] = {}

    def _open_once(self, url: str, headers: dict) -> tuple:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise StreamError(f"Adresse nicht abrufbar: {url[:80]!r}")
        https = u.scheme == "https"
        port = u.port or (443 if https else 80)
        ip = _public_ip(u.hostname, port, self.allow_private)
        try:
            sock = socket.create_connection((ip, port), self.timeout)
            if https:
                try:
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
                except Exception:
                    sock.close()
                    raise
            conn = (http.client.HTTPSConnection if https else http.client.HTTPConnection)(
                u.hostname, port, timeout=self.timeout)
            conn.sock = sock  # verbunden wird mit der geprueften IP, Host/SNI bleiben der Name
            target = urllib.parse.quote(u.path or "/", safe=_QUOTE_SAFE)
            if u.query:
                target += "?" + urllib.parse.quote(u.query, safe=_QUOTE_SAFE)
            hdrs = {"User-Agent": USER_AGENT, "Connection": "close", **headers}
            conn.request("GET", target, headers=hdrs)
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            raise StreamError(f"{u.hostname}: {e}") from e

    def open(self, url: str, headers: dict | None = None) -> Opened:
        """GET, Weiterleitungen werden selbst verfolgt (und jede Station geprueft)."""
        hops = [url]
        for _ in range(MAX_HOPS + 1):
            conn, resp = self._open_once(url, headers or {})
            loc = resp.getheader("Location")
            if resp.status in REDIRECTS and loc:
                conn.close()
                url = urllib.parse.urljoin(url, loc)
                hops.append(url)
                continue
            return Opened(conn, resp, url, hops)
        raise StreamError("zu viele Weiterleitungen")

    def probe(self, url: str) -> Probe:
        """Kette bis zum Ende verfolgen, ohne die Folge zu laden (kurz im Zwischenspeicher:
        das Radio fragt jede Folge zweimal)."""
        now = time.monotonic()
        with self._lock:
            hit = self._probes.get(url)
            if hit and now - hit[0] < 120:
                return hit[1]
        o = self.open(url, {"Range": "bytes=0-0"})
        o.conn.close()
        result = Probe(o.url, any(h.lower().startswith("https:") for h in o.hops), o.resp.status,
                       o.resp.getheader("Content-Type") or "")
        with self._lock:
            self._probes = {k: v for k, v in self._probes.items() if now - v[0] < 120}
            self._probes[url] = (now, result)
        return result


class Registry:
    """Kurze IDs fuer Adressen, die der Server weiterreicht (nur was wir selbst eintragen,
    ist abrufbar: kein offener Proxy)."""

    def __init__(self, maximum: int = 500, ttl: float = 6 * 3600):
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.maximum = maximum
        self.ttl = ttl

    def add(self, url: str) -> str:
        sid = secrets.token_hex(8)
        with self._lock:
            self._items[sid] = (time.monotonic(), url)
            while len(self._items) > self.maximum:
                self._items.popitem(last=False)
        return sid

    def get(self, sid: str) -> str | None:
        with self._lock:
            item = self._items.get(sid)
            if item and time.monotonic() - item[0] < self.ttl:
                return item[1]
        return None
