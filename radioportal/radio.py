"""Eigene Antworten fuer das Radio (Hauptmenue, Favoriten, Nachschlagen, Abspielen)."""

from __future__ import annotations

import http.client
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import NamedTuple

from . import probe as probe_mod
from . import testmenue
from .airable import UpstreamError, klartext
from .stream import StreamError
from .xmlitems import Xml, page

log = logging.getLogger(__name__)

TOKEN = b"<EncryptedToken>3a3f5ac48a1dab4e</EncryptedToken>"
HTML = "text/html; charset=UTF-8"
PLAY_CACHE_SECONDS = 30
PLATZ_ERSTE = 3000001  # "Favorit 1" ... "Favorit N": Platz k spielt immer den k-ten Favoriten der Weboberflaeche
PLAETZE = 10
AIRABLE_PAUSE = 300  # nach einem Ausfall fragen wir Airable 5 Minuten nicht mehr (kein 4-s-Warten je Menue)


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


def platz_nr(sid: str) -> int:
    """1..PLAETZE, wenn `sid` eine Platz-ID ist, sonst 0."""
    if sid.isdigit() and PLATZ_ERSTE <= int(sid) < PLATZ_ERSTE + PLAETZE:
        return int(sid) - PLATZ_ERSTE + 1
    return 0


def platz_sender(portal, nr: int) -> dict | None:
    favs = portal.library.favorites()
    return portal.library.sender(favs[nr - 1]) if 0 < nr <= len(favs) else None


def lookup(portal, sid: str) -> dict | None:
    """Eigener Sender, Platz (k-ter Favorit), Sender-Ersatz einer Airable-ID oder Testsender."""
    s = portal.library.sender(sid)
    if s:
        return s
    if platz_nr(sid):
        return platz_sender(portal, platz_nr(sid))
    ersatz = portal.store.ersatz_of(sid)
    if ersatz:
        s = lookup(portal, ersatz) if platz_nr(ersatz) else portal.library.sender(ersatz)
        if s:
            return s
    if portal.cfg.testmenue:
        return testmenue.sender(sid)
    return None


def _station(portal, host: str, shown_id: str, s: dict, name: str = "", desc: str = "") -> str:
    """Bookmark: Das Radio ruft ihn, wenn man den Sender dort zu den Favoriten hinzufuegt/entfernt
    (nur fuer eigene Sender, nicht fuer Plaetze und Ersatz)."""
    bm = ""
    if portal.library.sender(shown_id):
        aktion = "pop" if shown_id in portal.library.favorites() else "push"
        bm = f"http://{host}/vtuner/collection/{aktion}/station={shown_id}?"
    return portal.xml.station(shown_id, name or s["name"], f"http://{host}/portal/play/{shown_id}", desc=desc,
                              fmt=s.get("genre", ""), location=s.get("land", ""), bandwidth=s.get("bitrate", ""),
                              bookmark=bm)


def plaetze(portal, host: str) -> Reply:
    """Feste Eintraege "Favorit 1..N" zum Speichern in der FAV-Liste des Radios. Die Beschriftung
    bleibt immer gleich, was spielt, bestimmt die Weboberflaeche (Reihenfolge der Favoriten)."""
    x = portal.xml
    items = []
    for nr in range(1, PLAETZE + 1):
        sid = str(PLATZ_ERSTE + nr - 1)
        s = platz_sender(portal, nr)
        desc = ("Spielt jetzt: " + s["name"]) if s else "Noch leer (Favoriten im Browser pflegen)"
        items.append(x.station(sid, f"Favorit {nr}", f"http://{host}/portal/play/{sid}", desc=desc))
    return Reply(200, HTML, x.listing(items, previous=f"http://{host}/portal/favoriten?"))


def collection_aendern(portal, host: str, aktion: str, sid: str) -> Reply:
    """`/vtuner/collection/push|pop/station=ID`: Favorit am Radio hinzufuegen/entfernen."""
    lib = portal.library
    if not lib.sender(sid):
        ers = portal.store.ersatz_of(sid)
        sid = ers if ers and lib.sender(ers) else sid
    if aktion == "push":
        lib.fav_add(sid)
        text = "Zu den Favoriten hinzugefuegt"
    else:
        lib.fav_remove(sid)
        text = "Aus den Favoriten entfernt"
    log.info("Radio: %s station=%s", aktion, sid)
    return Reply(200, HTML, portal.xml.listing([portal.xml.display(text)], previous=f"http://{host}/vtuner?"))


def play_target(portal, host: str, s: dict) -> str:
    """Adresse, die das Radio abspielt. Weiterleitungen und Playlists loesen wir jetzt auf
    (Sitzungskennungen verfallen, das Radio folgt hoechstens einer Weiterleitung). https
    geht ueber /portal/live/<id>."""
    if s.get("raw"):
        return f"http://{host}/portal/umleitung/{s['id']}"
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


def airable_menue(portal, host: str, target: str, headers) -> list[tuple[str, str]]:
    """Die `Dir`-Eintraege aus Airables Hauptmenue (Titel, URL), oder [] wenn Airable nicht antwortet."""
    if time.monotonic() < portal.airable_pause_until:
        return []
    try:
        up = portal.forwarder.forward("GET", host, target, headers, b"", timeout=4.0)
        if up.status != 200:
            raise UpstreamError(f"Antwort {up.status}")
        root = ET.fromstring(klartext(up.headers, up.body))
        out = []
        for it in root.iter("Item"):
            if (it.findtext("ItemType") or "").strip() != "Dir":
                continue
            title, url = (it.findtext("Title") or "").strip(), (it.findtext("UrlDir") or "").strip()
            if title and url:
                out.append((title, url))
        if not out:
            raise UpstreamError("keine Menue-Eintraege")
        portal.airable_failures = 0
        return out
    except (UpstreamError, ET.ParseError) as e:
        # Ein einzelner Aussetzer sperrt Airable nicht; erst der zweite Fehler in Folge.
        portal.airable_failures += 1
        if portal.airable_failures >= 2:
            portal.airable_pause_until = time.monotonic() + AIRABLE_PAUSE
        log.warning("Hauptmenue: Airable nicht nutzbar (%s)%s", e,
                    f", {AIRABLE_PAUSE // 60} Min. nur eigene Eintraege" if portal.airable_failures >= 2 else "")
        return []


def hauptmenue(portal, host: str, target: str, headers) -> Reply:
    """Unsere Eintraege oben, darunter die Airable-Menues (solange es sie gibt)."""
    x = portal.xml
    items = [x.dir("Favoriten", f"http://{host}/portal/favoriten?")]
    if portal.podcasts.all():
        items.append(x.dir("Eigene Podcasts", f"http://{host}/portal/podcasts?"))
    for title, url in airable_menue(portal, host, target, headers):
        items.append(x.dir("Airable-Favoriten" if title == "Meine Favoriten" else title, url))
    if portal.cfg.testmenue:
        items.append(x.dir("Test", f"http://{host}/portal/test?"))
    return Reply(200, HTML, x.listing(items))


def favoriten(portal, host: str, qs: dict) -> Reply:
    x = portal.xml
    entries = []
    for sid in portal.library.favorites():
        s = portal.library.sender(sid)
        if s:
            entries.append(_station(portal, host, sid, s))
    start, end = _range(qs)
    if entries:
        entries.append(x.dir("Favorit-Plaetze (FAV-Taste)", f"http://{host}/portal/plaetze?"))
    if not entries:
        shown = [x.display("Noch keine Favoriten")]
        if portal.cfg.portal_url:
            shown.append(x.display("Im Browser: " + portal.cfg.portal_url))
        return Reply(200, HTML, x.listing(shown, count=len(shown), previous=f"http://{host}/vtuner?"))
    return Reply(200, HTML, x.listing(page(entries, start, end), count=len(entries),
                                      previous=f"http://{host}/vtuner?"))


def testmenue_liste(portal, host: str) -> Reply:
    x = portal.xml
    xu = Xml("utf8")
    items = [xu.display(testmenue.UMLAUT_ZEILEN[0]), x.display(testmenue.UMLAUT_ZEILEN[1])]
    for sid in testmenue.TEST_SENDER:
        items.append(_station(portal, host, sid, testmenue.sender(sid)))
    return Reply(200, HTML, x.listing(items, previous=f"http://{host}/vtuner?"))


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
        new = f"http://{host}/portal/stream/{portal.relay_ids.add(probe.final_url)}.mp3"
    else:
        new = probe.final_url
    if portal.cfg.mitschnitt:
        log.info("Folge: %s -> %s (%s)", "Relay" if probe.needs_relay else "direkt", new.split("?", 1)[0][:100],
                 probe.content_type)
    return new


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


def handle(portal, host: str, command: str, path: str, query: str, headers) -> Reply | None:
    """None: nicht unsere Sache, an Airable durchreichen."""
    low = path.lower()
    qs = urllib.parse.parse_qs(query, keep_blank_values=True)
    full = path + ("?" + query if query else "")

    if low.endswith("/loginxml.asp"):
        if "token" in qs:
            return Reply(200, "text/html;charset=UTF-8", TOKEN)
        if "gofile" in qs:
            return hauptmenue(portal, host, full, headers)
        return None
    if path.rstrip("/") == "/vtuner":
        return hauptmenue(portal, host, full, headers)

    if low.endswith("/search.asp") and qs.get("sSearchtype") == ["5"] and qs.get("Search"):
        return episode_nachschlagen(portal, host, qs["Search"][0])
    if low.endswith("/search.asp") and qs.get("sSearchtype") == ["3"] and qs.get("Search"):
        sid = qs["Search"][0]
        s = lookup(portal, sid)
        if not s:
            return None
        if not portal.library.sender(sid) and portal.store.ersatz_of(sid):
            portal.store.note_seen(sid)  # Taste mit Ersatz: "zuletzt" fuer die Oberflaeche fuehren
        x = portal.xml
        return Reply(200, HTML, x.listing([_station(portal, host, sid, s)], count=1,
                                          previous=f"http://{host}/vtuner?"))

    if path.startswith("/portal/play/"):
        s = lookup(portal, path.rsplit("/", 1)[-1])
        if not s:
            return Reply(404, "text/plain; charset=utf-8", b"Unbekannt.\n")
        # Klartext ohne Zeilenende, wie Airable
        return Reply(200, "audio/x-mpegurl", play_target(portal, host, s).encode("utf-8"))

    m = re.match(r"^/vtuner/collection/(push|pop)/station=(\d+)/?$", path)
    if m:
        return collection_aendern(portal, host, m.group(1), m.group(2))
    if path == "/portal/plaetze":
        return plaetze(portal, host)
    if path == "/portal/favoriten":
        return favoriten(portal, host, qs)
    if path == "/portal/podcasts":
        return podcast_liste(portal, host)
    if path.startswith("/portal/podcast/"):
        return podcast_folgen(portal, host, path.rsplit("/", 1)[-1])
    if path.startswith("/portal/episode/"):
        return episode_abspielen(portal, host, path.rsplit("/", 1)[-1])
    if path == "/portal/test" and portal.cfg.testmenue:
        return testmenue_liste(portal, host)
    if path.startswith("/portal/umleitung/") and portal.cfg.testmenue:
        s = testmenue.TEST_SENDER.get(path.rsplit("/", 1)[-1])
        if s and s.get("ziel"):
            return Reply(302, "text/html; charset=UTF-8", b"", (("Location", s["ziel"]),))
    if path.startswith("/portal/"):
        return Reply(404, "text/plain; charset=utf-8", b"Unbekannt.\n")
    return None
