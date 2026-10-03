"""Eigene Podcasts: RSS-Feeds abonnieren, Folgen merken, "ungehoert" verfolgen (`podcasts.json`).

Unabhaengig von Airable. Folge-IDs: Podcast-ID + sechsstellige laufende Nummer (nie neu vergeben).
Podcast-IDs ab 5000001, getrennt von Sendern (1000001) und Airable (16/32 Stellen)."""

from __future__ import annotations

import copy
import email.utils
import html
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import __version__
from .store import jetzt, write_json_atomic
from .stream import StreamError

log = logging.getLogger(__name__)

FIRST_ID = 5000001
MAX_EPISODES = 100          # pro Podcast; das Radio zeigt hoechstens 100 je Liste
MAX_FEED_BYTES = 40 * 1024 * 1024  # wird streamend gelesen, nicht im Speicher gehalten
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
NEW_AT_SUBSCRIBE = 3        # beim Abonnieren gelten nur die neuesten Folgen als ungehoert


class PodcastError(Exception):
    pass


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return "".join(child.itertext()).strip() if child is not None else ""


def _plain(text: str, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _date(text: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(text).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        return ""


def _item(it) -> dict | None:
    enc = it.find("enclosure")
    url = (enc.get("url") or "").strip() if enc is not None else ""
    if not url.lower().startswith(("http://", "https://")):
        return None
    return {"guid": _text(it, "guid") or url, "title": _plain(_text(it, "title"), 200) or "(ohne Titel)",
            "url": url, "type": (enc.get("type") or "").strip(), "date": _date(_text(it, "pubDate")),
            "duration": _text(it, ITUNES + "duration"),
            "desc": _plain(_text(it, "description") or _text(it, ITUNES + "summary"))}


def _newest(items: list, keep: int) -> list:
    return sorted(items, key=lambda i: i["date"], reverse=True)[:keep]


def parse_feed_stream(chunks) -> dict:
    """RSS 2.0 aus Datenbloecken lesen, ohne das ganze XML im Speicher zu halten (Feeds mit dem
    kompletten Archiv sind viele MB gross). Behalten werden die neuesten Folgen.

    -> {title, description, language, items: [{guid, title, url, type, date, duration, desc}]}"""
    parser = ET.XMLPullParser(events=("start", "end"))
    info = {"title": "", "description": "", "language": ""}
    items: list = []
    depth, first, ok = 0, True, True

    def drain():
        nonlocal depth
        for event, el in parser.read_events():
            if event == "start":
                depth += 1
                continue
            depth -= 1  # Tiefe des Elternelements: rss=1, channel=2
            if el.tag == "item" and depth == 2:
                it = _item(el)
                if it:
                    items.append(it)
                    if len(items) > 3 * MAX_EPISODES:
                        items[:] = _newest(items, MAX_EPISODES)
                el.clear()
            elif depth == 2 and el.tag in ("title", "description", "language") and not info[el.tag]:
                info[el.tag] = _plain("".join(el.itertext()), 300 if el.tag == "description" else 120)

    for chunk in chunks:
        if first:
            first = False
            head = chunk[:4096]
            if b"<!ENTITY" in head.upper():
                raise PodcastError("Feed mit eigenen Entitaeten wird nicht gelesen")
            if re.search(rb"<html", head, re.I):
                raise PodcastError("Das ist eine Webseite, kein RSS-Feed (Adresse des Feeds suchen)")
        try:
            parser.feed(chunk)
            drain()
        except ET.ParseError as e:
            if items:  # abgeschnitten oder fehlerhaft hinten: was wir haben, reicht
                ok = False
                break
            raise PodcastError(f"Kein lesbarer Feed ({e})") from e
    if ok:
        try:
            parser.close()
            drain()
        except ET.ParseError as e:
            if not items:
                raise PodcastError(f"Kein lesbarer Feed ({e})") from e
    if not items:
        raise PodcastError("Der Feed enthaelt keine Folgen mit Audio-Datei (oder ist kein RSS-Feed)")
    return {"title": info["title"] or "(ohne Titel)", "description": info["description"],
            "language": info["language"], "items": _newest(items, MAX_EPISODES)}


def parse_feed(data: bytes) -> dict:
    return parse_feed_stream([data])


def fetch_feed(streamer, url: str) -> dict:
    """Feed laden (auch https, Weiterleitungen) und streamend lesen."""
    if not url.lower().startswith(("http://", "https://")):
        raise PodcastError("Bitte eine Adresse mit http:// oder https:// angeben")
    try:
        o = streamer.open(url, {"Accept": "application/rss+xml, application/xml, text/xml, */*"})
    except StreamError as e:
        raise PodcastError(f"Feed nicht erreichbar: {e}") from e
    try:
        if o.resp.status != 200:
            raise PodcastError(f"Feed antwortet mit {o.resp.status}")

        def chunks():
            total = 0
            while total < MAX_FEED_BYTES:
                data = o.resp.read(65536)
                if not data:
                    return
                total += len(data)
                yield data

        return parse_feed_stream(chunks())
    except OSError as e:
        raise PodcastError(f"Feed nicht lesbar: {e}") from e
    finally:
        o.conn.close()


def episode_id(pid: str, n: int) -> str:
    return f"{pid}{n:06d}"


class PodcastStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._next = FIRST_ID
        self._data: dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self._data = raw.get("podcasts", {})
            self._next = int(raw.get("naechste_id", FIRST_ID))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            kaputt = self.path.with_name(self.path.name + ".kaputt")
            log.error("%s unlesbar (%s), liegt jetzt als %s", self.path, e, kaputt)
            try:
                os.replace(self.path, kaputt)
            except OSError:
                pass
        for pid in self._data:
            if pid.isdigit() and int(pid) >= self._next:
                self._next = int(pid) + 1

    def _save(self) -> None:
        write_json_atomic(self.path, {"naechste_id": self._next, "podcasts": self._data})

    # --- Abonnieren / Aktualisieren ------------------------------------------------
    def add(self, feed_url: str, parsed: dict) -> tuple[str, bool]:
        """-> (Podcast-ID, neu angelegt?)"""
        with self._lock:
            for pid, p in self._data.items():
                if p["feed"] == feed_url:
                    return pid, False
            pid = str(self._next)
            self._next += 1
            self._data[pid] = {"id": pid, "feed": feed_url, "name": parsed["title"],
                               "beschreibung": parsed["description"], "sprache": parsed["language"],
                               "angelegt": jetzt(), "aktualisiert": "", "naechste_n": 1, "episoden": []}
            self._merge(pid, parsed, erste=True)
            self._save()
            return pid, True

    def refresh(self, pid: str, parsed: dict) -> int:
        """Neue Folgen uebernehmen. -> Anzahl neuer Folgen"""
        with self._lock:
            if pid not in self._data:
                return 0
            neu = self._merge(pid, parsed, erste=False)
            self._save()
            return neu

    def _merge(self, pid: str, parsed: dict, erste: bool) -> int:
        p = self._data[pid]
        p["name"] = parsed["title"] or p["name"]
        p["beschreibung"] = parsed["description"] or p.get("beschreibung", "")
        p["aktualisiert"] = jetzt()
        bekannt = {e["guid"] for e in p["episoden"]}
        fresh = [it for it in parsed["items"] if it["guid"] not in bekannt]
        fresh.sort(key=lambda it: it["date"])  # aelteste zuerst: kleinere Nummern
        for it in fresh:
            n = p["naechste_n"]
            p["naechste_n"] += 1
            p["episoden"].append({"n": n, "guid": it["guid"], "titel": it["title"], "url": it["url"],
                                  "typ": it["type"], "datum": it["date"], "dauer": it["duration"],
                                  "beschreibung": it["desc"], "gehoert": False})
        if erste:  # nur die neuesten gelten als ungehoert
            for e in sorted(p["episoden"], key=lambda e: (e["datum"], e["n"]))[:-NEW_AT_SUBSCRIBE]:
                e["gehoert"] = True
        p["episoden"].sort(key=lambda e: (e["datum"], e["n"]), reverse=True)
        del p["episoden"][MAX_EPISODES:]
        return len(fresh)

    # --- Lesen / Aendern ----------------------------------------------------------------
    def all(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, pid: str) -> dict | None:
        with self._lock:
            p = self._data.get(str(pid))
            return copy.deepcopy(p) if p else None

    def find_episode(self, eid: str) -> tuple[dict, dict] | None:
        """(Podcast, Folge) zur Folge-ID."""
        eid = str(eid)
        with self._lock:
            for pid, p in self._data.items():
                if eid.startswith(pid) and eid[len(pid):].isdigit():
                    n = int(eid[len(pid):])
                    for e in p["episoden"]:
                        if e["n"] == n:
                            return copy.deepcopy(p), copy.deepcopy(e)
        return None

    def mark_heard(self, eid: str) -> None:
        eid = str(eid)
        with self._lock:
            for pid, p in self._data.items():
                if eid.startswith(pid) and eid[len(pid):].isdigit():
                    for e in p["episoden"]:
                        if e["n"] == int(eid[len(pid):]) and not e["gehoert"]:
                            e["gehoert"] = True
                            self._save()
                    return

    def mark_all_heard(self, pid: str) -> None:
        with self._lock:
            for e in self._data.get(pid, {}).get("episoden", []):
                e["gehoert"] = True
            self._save()

    def remove(self, pid: str) -> bool:
        with self._lock:
            if pid not in self._data:
                return False
            del self._data[pid]
            self._save()
            return True

    @staticmethod
    def unheard(p: dict) -> int:
        return sum(1 for e in p["episoden"] if not e["gehoert"])


def refresh_all(store: "PodcastStore", streamer) -> dict:
    """Alle Podcasts aktualisieren. -> {Podcast-ID: Anzahl neuer Folgen oder Fehlertext}"""
    result = {}
    for pid, p in store.all().items():
        try:
            result[pid] = store.refresh(pid, fetch_feed(streamer, p["feed"]))
        except PodcastError as e:
            log.warning("Podcast %s (%s): %s", pid, p["name"], e)
            result[pid] = str(e)
    return result


def search_podcasts(term: str, fetch=None, limit: int = 25) -> list[dict]:
    """Podcast-Suche ueber die kostenlose iTunes-Suche (liefert die RSS-Adresse mit).
    -> [{name, autor, feed, genre, land}]"""
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "media": "podcast", "entity": "podcast", "limit": str(limit), "country": "DE"})
    try:
        if fetch:
            data = fetch(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": f"frontier-radio-portal/{__version__}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError) as e:
        raise PodcastError(f"Podcast-Suche nicht erreichbar ({e})") from e
    out = []
    for r in data.get("results", []) if isinstance(data, dict) else []:
        feed = r.get("feedUrl") or ""
        if feed.lower().startswith(("http://", "https://")):
            out.append({"name": r.get("collectionName") or r.get("trackName") or "(ohne Titel)",
                        "autor": r.get("artistName") or "", "feed": feed,
                        "genre": r.get("primaryGenreName") or "", "land": r.get("country") or ""})
    return out
