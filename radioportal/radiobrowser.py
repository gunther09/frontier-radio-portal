"""Client fuer radio-browser.info (https://api.radio-browser.info/)."""

from __future__ import annotations

import json
import logging
import random
import socket
import urllib.parse
import urllib.request

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"frontier-radio-portal/{__version__}"
FALLBACK_SERVERS = ["de1.api.radio-browser.info", "de2.api.radio-browser.info", "nl1.api.radio-browser.info",
                    "at1.api.radio-browser.info"]
FIELDS = ("stationuuid", "name", "url_resolved", "url", "codec", "bitrate", "countrycode", "country",
          "tags", "hls", "clickcount", "votes")


class RadioBrowserError(Exception):
    pass


def discover_servers() -> list[str]:
    """Server laut API-Doku ueber `all.api.radio-browser.info` ermitteln (Rueckwaertsaufloesung)."""
    names = []
    try:
        for info in socket.getaddrinfo("all.api.radio-browser.info", 443, socket.AF_INET, socket.SOCK_STREAM):
            try:
                names.append(socket.gethostbyaddr(info[4][0])[0])
            except OSError:
                pass
    except OSError:
        pass
    names = [n for n in dict.fromkeys(names) if n.endswith("radio-browser.info")]
    random.shuffle(names)
    return names or random.sample(FALLBACK_SERVERS, len(FALLBACK_SERVERS))


class RadioBrowser:
    def __init__(self, servers: list[str] | None = None, timeout: float = 8.0, fetch=None):
        self.servers = servers
        self.timeout = timeout
        self._fetch = fetch or self._http_get

    def _http_get(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read(4 * 1024 * 1024).decode("utf-8"))

    def _get(self, path: str, params: dict | None = None):
        if self.servers is None:
            self.servers = discover_servers()
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        last = None
        for server in list(self.servers):
            try:
                return self._fetch(f"https://{server}{path}{query}")
            except (OSError, ValueError) as e:
                last = e
                log.warning("radio-browser %s: %s", server, e)
        self.servers = None  # beim naechsten Mal neu ermitteln
        raise RadioBrowserError(f"radio-browser.info nicht erreichbar ({last})")

    @staticmethod
    def _clean(st: dict) -> dict:
        out = {k: st.get(k, "") for k in FIELDS}
        out["url"] = st.get("url_resolved") or st.get("url") or ""
        tags = []
        for t in (t.strip() for t in str(st.get("tags") or "").split(",")):
            if t and len(", ".join(tags + [t])) <= 80:
                tags.append(t)
        out["tags"] = ", ".join(tags)
        return out

    def _search_raw(self, name: str, mp3_only: bool, limit: int) -> list:
        params = {"name": name, "hidebroken": "true", "order": "clickcount", "reverse": "true",
                  "limit": str(limit)}
        if mp3_only:
            params["codec"] = "MP3"
        raw = self._get("/json/stations/search", params)
        return raw if isinstance(raw, list) else []

    def search(self, name: str, mp3_only: bool = True, limit: int = 40) -> list[dict]:
        """Treffer mit http-Adresse zuerst, gleiche Adressen zusammengefasst.

        radio-browser sucht den Text am Stueck im Namen ("alex berlin" findet "ALEX Offener Kanal
        Berlin" nicht). Bei mehreren Woertern und wenigen Treffern suchen wir deshalb nach dem
        laengsten Wort und filtern nach allen Woertern (Name oder Tags)."""
        raw = self._search_raw(name, mp3_only, limit)
        words = [w for w in name.lower().split() if w]
        if len(words) > 1 and len(raw) < 5:
            for st in self._search_raw(max(words, key=len), mp3_only, 300):
                text = f"{st.get('name', '')} {st.get('tags', '')}".lower()
                if all(w in text for w in words):
                    raw.append(st)
        seen, out = set(), []
        for st in raw:
            c = self._clean(st)
            if not c["url"] or c["url"] in seen or st.get("hls") in (1, "1", True):
                continue
            seen.add(c["url"])
            out.append(c)
        out.sort(key=lambda s: not s["url"].lower().startswith("http://"))  # stabil: Klickzahl bleibt
        return out[:limit]

    def by_uuid(self, uuid: str) -> dict | None:
        raw = self._get(f"/json/stations/byuuid/{urllib.parse.quote(uuid, safe='')}")
        return self._clean(raw[0]) if isinstance(raw, list) and raw else None

    def count_click(self, uuid: str) -> None:
        """Aus Hoeflichkeit: zaehlt als Klick bei radio-browser."""
        try:
            self._get(f"/json/url/{urllib.parse.quote(uuid, safe='')}")
        except RadioBrowserError:
            pass
