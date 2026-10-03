"""Stream-Pruefung: Kann das Radio diese Adresse spielen?

Eigener kleiner Socket-Client, denn Shoutcast-1-Server antworten mit `ICY 200 OK`, daran
scheitert `http.client`. Das Radio kann MP3 (und WMA) ueber http; kein https, kein AAC, kein HLS."""

from __future__ import annotations

import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field

from . import __version__

USER_AGENT = f"frontier-radio-portal/{__version__}"
MAX_HEADER = 16 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 6.0
REDIRECTS = (301, 302, 303, 307, 308)


@dataclass
class Result:
    ok: bool = False            # Adresse antwortet mit einem Audiostrom
    url: str = ""               # Endadresse nach Weiterleitungen/Playlist (nur zur Anzeige)
    start: str = ""             # Adresse, die gespeichert wird: wird beim Abspielen frisch aufgeloest
                                # (Weiterleitungen tragen oft Sitzungskennungen, die verfallen)
    codec: str = ""             # MP3, AAC, HLS, OGG, ...
    bitrate: str = ""
    name: str = ""
    https_nur: bool = False     # nur ueber https erreichbar
    spielbar: bool = False      # das Radio kann es direkt spielen (MP3 ueber http)
    hinweis: str = ""           # Text fuer die Oberflaeche
    log: list = field(default_factory=list)


def codec_aus_typ(content_type: str, url: str = "") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    u = url.lower().split("?")[0]
    if ct in ("audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg"):
        return "MP3"
    if ct in ("audio/aac", "audio/aacp", "audio/x-aac", "audio/mp4", "audio/x-m4a", "audio/mp4a-latm"):
        return "AAC"
    if ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl") or u.endswith(".m3u8"):
        return "HLS"
    if ct in ("audio/ogg", "application/ogg", "audio/opus"):
        return "OGG"
    if ct in ("audio/x-ms-wma", "audio/x-ms-asf", "video/x-ms-asf"):
        return "WMA"
    if ct in ("audio/x-scpls", "application/pls+xml") or u.endswith(".pls"):
        return "PLS"
    if ct in ("audio/x-mpegurl",) or u.endswith(".m3u"):
        return "M3U"
    return ""


def _mp3_sync(data: bytes) -> bool:
    """ID3-Kopf oder MP3-Frame-Sync (11 gesetzte Bits) in den ersten Bytes."""
    if data[:3] == b"ID3":
        return True
    for i in range(min(len(data) - 1, 4096)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0 and (data[i + 1] & 0x06) != 0:
            return True
    return False


def _fetch(url: str, timeout: float, body_bytes: int = 0):
    """Eine Anfrage. Gibt (status, header-dict (klein), erste Bytes des Bodys) zurueck."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise OSError(f"keine http(s)-Adresse: {url[:60]!r}")
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    sock = socket.create_connection((u.hostname, port), timeout)
    try:
        sock.settimeout(timeout)
        if https:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        target = urllib.parse.quote(u.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if u.query:
            target += "?" + urllib.parse.quote(u.query, safe="/%:@!$&'()*+,;=-._~?")
        host = u.hostname + (f":{u.port}" if u.port else "")
        sock.sendall((f"GET {target} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n"
                      f"Accept: */*\r\nIcy-MetaData: 0\r\nConnection: close\r\n\r\n").encode("latin-1", "replace"))
        buf = b""
        while b"\r\n\r\n" not in buf and b"\n\n" not in buf and len(buf) < MAX_HEADER:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
        head, _, rest = buf.partition(sep)
        lines = head.decode("latin-1").replace("\r", "").split("\n")
        parts = lines[0].split(None, 2)
        if len(parts) < 2 or not (parts[0].startswith("HTTP/") or parts[0].upper().startswith("ICY")):
            raise OSError(f"keine HTTP/ICY-Antwort: {lines[0][:60]!r}")
        try:
            status = int(parts[1])
        except ValueError:
            raise OSError(f"Statuszeile unlesbar: {lines[0][:60]!r}") from None
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k:
                headers[k.strip().lower()] = v.strip()
        while body_bytes and len(rest) < body_bytes:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        return status, headers, rest[:body_bytes], lines[0].startswith("ICY")
    finally:
        sock.close()


def _first_playlist_url(text: str, kind: str) -> str:
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if kind == "PLS":
            if line.lower().startswith("file") and "=" in line:
                cand = line.split("=", 1)[1].strip()
                if cand.lower().startswith("http"):
                    return cand
        elif line and not line.startswith("#") and line.lower().startswith("http"):
            return line
    return ""


def _one(url: str, timeout: float, log: list):
    """Folgt Weiterleitungen und Playlists. Gibt (endgueltige Adresse, Status, Header, Body, icy)."""
    for _ in range(MAX_REDIRECTS + 2):
        status, headers, body, icy = _fetch(url, timeout, body_bytes=4096)
        log.append(f"{status} {url[:100]}")
        if status in REDIRECTS and headers.get("location"):
            url = urllib.parse.urljoin(url, headers["location"])
            continue
        kind = codec_aus_typ(headers.get("content-type", ""), url)
        if status == 200 and kind in ("PLS", "M3U"):
            nxt = _first_playlist_url(body.decode("utf-8", "replace"), kind)
            if not nxt:
                raise OSError("Playlist ohne Adresse")
            url = nxt
            continue
        return url, status, headers, body, icy
    raise OSError("zu viele Weiterleitungen")


def probe(url: str, timeout: float = TIMEOUT) -> Result:
    """Prueft `url`. Bei https wird zuerst die http-Variante versucht (nur sie zaehlt, wenn
    sie nicht wieder auf https umleitet)."""
    r = Result(url=url)
    candidates = [url]
    if url.lower().startswith("https://"):
        candidates.insert(0, "http://" + url[8:])
    last_err = ""
    for cand in candidates:
        try:
            final, status, headers, body, icy = _one(cand, timeout, r.log)
        except (OSError, ssl.SSLError) as e:
            last_err = str(e)
            r.log.append(f"Fehler {cand[:80]}: {e}")
            continue
        if status != 200:
            last_err = f"Antwort {status}"
            continue
        https = final.lower().startswith("https://")
        if cand.startswith("http://") and https and url.lower().startswith("https://") and cand != url:
            # die http-Variante leitet wieder auf https um: gilt nicht
            last_err = "http-Variante leitet auf https um"
            continue
        ct = headers.get("content-type", "")
        codec = codec_aus_typ(ct, final)
        if not codec and (icy or "icy-name" in headers or "icy-br" in headers):
            codec = "MP3" if _mp3_sync(body) or not body else ""
        if not codec and _mp3_sync(body):
            codec = "MP3"
        if not codec:
            last_err = f"kein Audiostrom (Content-Type {ct or 'fehlt'})"
            continue
        r.ok, r.url, r.start, r.codec = True, final, cand, codec
        r.bitrate = headers.get("icy-br", "").split(",")[0].strip()
        r.name = headers.get("icy-name", "")
        r.https_nur = https
        r.spielbar = codec == "MP3" and not https
        if r.spielbar:
            r.hinweis = "Das Radio kann diesen Sender direkt spielen."
        elif https and codec == "MP3":
            r.hinweis = "Nur über https erreichbar: läuft über den Server (Weiterleitung)."
        elif codec in ("AAC", "HLS", "OGG"):
            r.hinweis = f"{codec}: das Radio spielt nur MP3. Läuft erst mit einer Umwandlung (noch nicht gebaut)."
        else:
            r.hinweis = f"{codec}: ob das Radio das spielt, ist offen."
        return r
    r.hinweis = f"Nicht erreichbar oder kein Audiostrom ({last_err})."
    return r


def resolve_for_play(start: str, timeout: float = TIMEOUT) -> str:
    """Loest Weiterleitungen und Playlists der gespeicherten Adresse jetzt auf, damit das Radio
    sie direkt ohne weiteren Sprung abrufen kann. Bei einem Fehler bleibt die Adresse unveraendert."""
    try:
        final, status, _headers, _body, _icy = _one(start, timeout, [])
    except (OSError, ssl.SSLError):
        return start
    return final if status == 200 else start
