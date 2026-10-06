"""Eigene Antworten fuer das Radio: Senderliste, Podcasts, Nachschlagen, Abspielen.

Das Menue *Internet Radio* zeigt die Sender der Weboberflaeche direkt, darunter *Podcasts*.
Airable kommt im Menue nicht vor; nur was das Portal nicht kennt, wird noch durchgereicht."""

from __future__ import annotations

import http.client
import logging
import re
import time
import urllib.parse
from typing import NamedTuple

from . import probe as probe_mod
from .stream import StreamError
from .xmlitems import page

log = logging.getLogger(__name__)

TOKEN = b"<EncryptedToken>3a3f5ac48a1dab4e</EncryptedToken>"
HTML = "text/html; charset=UTF-8"
PLAY_CACHE_SECONDS = 30


class Reply(NamedTuple):
    status: int
    ctype: str
    body: bytes
    headers: tuple = ()


def _range(qs: dict) -> tuple[int, int]:
    def num(key, default):
        try:
            return int(qs.get(key, [default])[0])
        except ValueError:
            return default
    return num("startItems", 1), num("endItems", 100)


def _station(portal, host: str, sid: str, s: dict) -> str:
    return portal.xml.station(sid, s["name"], f"http://{host}/portal/play/{sid}", fmt=s.get("genre", ""),
                              location=s.get("land", ""), bandwidth=s.get("bitrate", ""))


def play_target(portal, host: str, s: dict) -> str:
    """Adresse, die das Radio abspielt. Weiterleitungen und Playlists loesen wir jetzt auf
    (Sitzungskennungen verfallen, das Radio folgt hoechstens einer Weiterleitung). https
    geht ueber /portal/live/<id>."""
    now = time.monotonic()
    hit = portal.play_cache.get(s["url"])
    if hit and now - hit[0] < PLAY_CACHE_SECONDS:
        final = hit[1]
    else:
        final = probe_mod.resolve_for_play(s["url"], timeout=4.0)
        portal.play_cache[s["url"]] = (now, final)
        for k in [k for k, v in portal.play_cache.items() if now - v[0] > 300]:
            portal.play_cache.pop(k, None)
    if final.lower().startswith("https://"):
        return f"http://{host}/portal/live/{s['id']}.mp3"
    return final


def senderliste(portal, host: str, qs: dict) -> Reply:
    """Hauptmenue: die Sender in der Reihenfolge der Weboberflaeche, darunter *Podcasts*."""
    x = portal.xml
    entries = []
    for sid in portal.library.liste():
        s = portal.library.sender(sid)
        if s:
            entries.append(_station(portal, host, sid, s))
    if portal.podcasts.all():
        entries.append(x.dir("Podcasts", f"http://{host}/portal/podcasts?"))
    if not entries:
        shown = [x.display("Noch keine Sender")]
        if portal.cfg.portal_url:
            shown.append(x.display("Im Browser: " + portal.cfg.portal_url))
        return Reply(200, HTML, x.listing(shown))
    start, end = _range(qs)
    return Reply(200, HTML, x.listing(page(entries, start, end), count=len(entries)))


def episode_location(portal, host: str, loc: str) -> str | None:
    """Das Radio folgt hoechstens einer Weiterleitung und kann kein https: die Kette selbst
    aufloesen. Nur http: die Endadresse. https irgendwo: der Server holt die Folge
    (/portal/stream/<id>.mp3). None: Kette nicht nutzbar."""
    try:
        probe = portal.streamer.probe(loc)
    except (StreamError, OSError, http.client.HTTPException) as e:
        log.warning("Folge: Kette nicht aufloesbar (%s)", e)
        return None
    if probe.status not in (200, 206):
        log.warning("Folge: Ziel antwortet %d", probe.status)
        return None
    if probe.needs_relay:
        return f"http://{host}/portal/stream/{portal.relay_ids.add(probe.final_url)}.mp3"
    return probe.final_url


def podcast_liste(portal, host: str) -> Reply:
    x = portal.xml
    items = []
    for pid, p in sorted(portal.podcasts.all().items(), key=lambda kv: kv[1]["name"].lower()):
        neu = portal.podcasts.unheard(p)
        items.append(x.show(pid, p["name"] + (f" ({neu} neu)" if neu else ""), f"http://{host}/portal/podcast/{pid}"))
    if not items:
        items = [x.display("Noch keine Podcasts")]
        if portal.cfg.portal_url:
            items.append(x.display("Im Browser: " + portal.cfg.portal_url))
    return Reply(200, HTML, x.listing(items, previous=f"http://{host}/vtuner?"))


def _episode_item(portal, host: str, p: dict, e: dict) -> str:
    from .podcasts import episode_id
    eid = episode_id(p["id"], e["n"])
    name = ("* " if not e["gehoert"] else "") + e["titel"]
    return portal.xml.episode(eid, p["name"], name, f"http://{host}/portal/episode/{eid}",
                              desc=e.get("beschreibung", ""), lang=p.get("sprache", ""))


def podcast_folgen(portal, host: str, pid: str) -> Reply:
    p = portal.podcasts.get(pid)
    if not p:
        return Reply(404, "text/plain; charset=utf-8", b"Unbekannt.\n")
    items = [_episode_item(portal, host, p, e) for e in p["episoden"]]
    return Reply(200, HTML, portal.xml.listing(items, previous=f"http://{host}/portal/podcasts?"))


def episode_nachschlagen(portal, host: str, eid: str) -> Reply | None:
    """`Search.asp?sSearchtype=5&Search=<ID>`: das Radio sucht die zuletzt gehoerte Folge wieder."""
    found = portal.podcasts.find_episode(eid)
    if not found:
        return None
    p, e = found
    return Reply(200, HTML, portal.xml.listing([_episode_item(portal, host, p, e)], count=1,
                                               previous=f"http://{host}/vtuner?"))


def episode_abspielen(portal, host: str, eid: str) -> Reply:
    found = portal.podcasts.find_episode(eid)
    if not found:
        return Reply(404, "text/plain; charset=utf-8", b"Unbekannt.\n")
    _p, e = found
    new = episode_location(portal, host, e["url"]) or e["url"]
    portal.podcasts.mark_heard(eid)
    return Reply(302, HTML, b"", (("Location", new),))


def beschreibe(path: str, query: str) -> tuple[str, str]:
    """Kurzbeschreibung einer Radio-Anfrage fuer das Anfrage-Protokoll: (Text, ID oder "")."""
    low = path.lower()
    qs = urllib.parse.parse_qs(query)
    if low.endswith("/loginxml.asp"):
        return ("Anmeldung", "") if "token" in qs else ("Senderliste", "")
    if path.rstrip("/") in ("/vtuner", "/portal/favoriten"):
        return "Senderliste", ""
    if low.endswith("/search.asp"):
        sid = (qs.get("Search") or [""])[0][:40]
        return ("Folge nachschlagen" if qs.get("sSearchtype") == ["5"] else "Sender nachschlagen"), sid
    if low.endswith("/findupdate.aspx"):
        return "Update-Prüfung", ""
    for prefix, text in (("/portal/play/", "Sender abspielen"), ("/portal/live/", "Sender über den Server"),
                         ("/portal/podcast/", "Podcast öffnen"), ("/portal/episode/", "Folge abspielen")):
        if path.startswith(prefix):
            return text, path[len(prefix):].split(".")[0][:40]
    if path.startswith("/portal/stream/"):
        return "Folge über den Server", ""
    if path == "/portal/podcasts":
        return "Podcasts", ""
    return path[:80], ""


def handle(portal, host: str, command: str, path: str, query: str, headers) -> Reply | None:
    """None: nicht unsere Sache, an Airable durchreichen."""
    low = path.lower()
    qs = urllib.parse.parse_qs(query, keep_blank_values=True)

    if low.endswith("/loginxml.asp"):
        if "token" in qs:
            return Reply(200, "text/html;charset=UTF-8", TOKEN)
        if "gofile" in qs:
            return senderliste(portal, host, qs)
        return None
    # /vtuner ist Airables "zurueck nach oben", /portal/favoriten das Menue bis Version 0.2
    if path.rstrip("/") in ("/vtuner", "/portal/favoriten"):
        return senderliste(portal, host, qs)

    if low.endswith("/search.asp") and qs.get("sSearchtype") == ["5"] and qs.get("Search"):
        return episode_nachschlagen(portal, host, qs["Search"][0])
    if low.endswith("/search.asp") and qs.get("sSearchtype") == ["3"] and qs.get("Search"):
        sid = qs["Search"][0]
        s = portal.library.sender(sid)
        if not s:
            return None
        portal.library.note_radio(sid)
        return Reply(200, HTML, portal.xml.listing([_station(portal, host, sid, s)], count=1,
                                                   previous=f"http://{host}/vtuner?"))

    if path.startswith("/portal/play/"):
        sid = path.rsplit("/", 1)[-1]
        s = portal.library.sender(sid)
        if not s:
            return Reply(404, "text/plain; charset=utf-8", b"Unbekannt.\n")
        portal.library.note_radio(sid)
        # Klartext ohne Zeilenende, wie Airable
        return Reply(200, "audio/x-mpegurl", play_target(portal, host, s).encode("utf-8"))

    if path == "/portal/podcasts":
        return podcast_liste(portal, host)
    if re.match(r"^/portal/podcast/\d+$", path):
        return podcast_folgen(portal, host, path.rsplit("/", 1)[-1])
    if path.startswith("/portal/episode/"):
        return episode_abspielen(portal, host, path.rsplit("/", 1)[-1])
    if path.startswith("/portal/"):
        return Reply(404, "text/plain; charset=utf-8", b"Unbekannt.\n")
    return None
