"""Durchreichen an den Airable-Uebergangsdienst, ID-Sammler und Mitschnitt."""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import json
import logging
import re
import socket
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

from .store import AirableStore, jetzt

log = logging.getLogger(__name__)

DOMAIN = "wifiradiofrontier.com"
MAX_BODY = 4 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
       "upgrade", "proxy-authenticate", "proxy-authorization"}
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_PLAY = re.compile(r"/vtuner/play/station=(\d+)")


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
    def __init__(self, timeout: float = 10.0, pi_ip: str = "", port: int = 80,
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


class RadioParams:
    """Merkt sich je Radio-IP die Parameter (`mac`, `dlang`, `fver`, `ven`) aus dessen
    Menue-Anfragen und ergaenzt damit Aufrufe, die das Radio ohne sie macht.

    Hintergrund: Die `ShowOnDemandURL` eines Podcasts (`/vtuner/podcast=<ID>?`) ruft das
    Radio unveraendert auf, also ohne `mac`. Airable antwortet darauf mit 500, mit den
    Parametern des Radios aber mit der Folgenliste."""

    KEYS = ("mac", "dlang", "fver", "ven")

    def __init__(self):
        self._lock = threading.Lock()
        self._by_ip: dict[str, dict[str, str]] = {}

    def learn(self, ip: str, target: str) -> None:
        path, _, query = target.partition("?")
        if "/play/" in path:
            return  # Abspiel-Adressen tragen eine andere dlang, nicht daraus lernen
        qs = urllib.parse.parse_qs(query)
        if not qs.get("mac"):
            return
        with self._lock:
            self._by_ip[ip] = {k: qs[k][0] for k in self.KEYS if qs.get(k)}

    def complete(self, ip: str, target: str) -> str:
        """Gibt `target` mit ergaenzten Parametern zurueck (oder unveraendert)."""
        path, _, query = target.partition("?")
        if not path.startswith("/vtuner/") or "/play/" in path:
            return target
        qs = urllib.parse.parse_qs(query)
        if qs.get("mac"):
            return target
        with self._lock:
            known = dict(self._by_ip.get(ip, {}))
        if not known.get("mac"):
            return target
        extra = []
        if "startItems" not in qs:
            extra += ["startItems=1", "endItems=100"]
        extra += [f"{k}={urllib.parse.quote(v, safe='')}" for k, v in known.items()]
        return path + "?" + (query + "&" if query else "&") + "&".join(extra)


def klartext(up_headers, body: bytes) -> bytes:
    for k, v in up_headers:
        if k.lower() == "content-encoding" and v.lower() == "gzip":
            try:
                return gzip.decompress(body)
            except OSError:
                break
    return body


def parse_stations(xml_bytes: bytes) -> list[dict]:
    """`Station`-Eintraege einer ListOfItems-Antwort."""
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("Item"):
        if (item.findtext("ItemType") or "").strip() != "Station":
            continue
        sid = (item.findtext("StationId") or "").strip()
        if not sid.isdigit():
            continue
        out.append({
            "id": sid,
            "name": (item.findtext("StationName") or "").strip(),
            "format": (item.findtext("StationFormat") or "").strip(),
            "ort": (item.findtext("StationLocation") or "").strip(),
            "bitrate": (item.findtext("StationBandWidth") or "").strip(),
        })
    return out


class Collector:
    """Merkt sich die IDs, die das Radio nachschlaegt (`Search.asp`) und abspielt (`play`).

    Sender, die nur in Listen auftauchen, werden nicht erfasst: Nur was das Radio selbst
    nachschlaegt, kann auf einer Stationstaste liegen."""

    def __init__(self, store: AirableStore):
        self.store = store

    def observe(self, target: str, up: Upstream) -> None:
        if up.status != 200:
            return
        path, _, query = target.partition("?")
        body = klartext(up.headers, up.body)
        m = _PLAY.search(path)
        if m:
            url = body.decode("utf-8", "replace").strip()
            if url.lower().startswith("http"):
                self.store.note_play(m.group(1), url)
            return
        if path.lower().endswith("/search.asp"):
            qs = urllib.parse.parse_qs(query)
            if qs.get("sSearchtype") == ["3"]:
                for st in parse_stations(body):
                    self.store.note_seen(st["id"], st["name"], st["format"], st["ort"], st["bitrate"])


class Recorder:
    """Mitschnitt: durchgereichte Antworten als Dateien plus `index.jsonl`. Enthaelt den
    Radio-Hash, bleibt also privat."""

    MAX_DATEIEN = 5000

    def __init__(self, directory: Path, enabled: bool):
        self.dir = Path(directory)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._n = 0
        self._voll = False
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._n = sum(1 for _ in self.dir.glob("*.txt"))

    def record(self, host: str, target: str, up: Upstream) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._n >= self.MAX_DATEIEN:
                if not self._voll:
                    self._voll = True
                    log.warning("Mitschnitt voll (%d Dateien), nichts mehr gespeichert", self.MAX_DATEIEN)
                return
            self._n += 1
            stamp = jetzt().replace(":", "").replace("-", "")[:15]
            name = f"{stamp}_{self._n:04d}_" + re.sub(r"[^A-Za-z0-9._-]+", "_", host + target)[:90] + ".txt"
            (self.dir / name).write_bytes(up.body)
            ctype = next((v for k, v in up.headers if k.lower() == "content-type"), "")
            ort = next((v for k, v in up.headers if k.lower() == "location"), "")
            zeile = {"t": jetzt(), "host": host, "target": target, "status": up.status,
                     "content_type": ctype, "location": ort, "bytes": len(up.body), "datei": name}
            with open(self.dir / "index.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
