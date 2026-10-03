"""XML-Listen im Format des Radios (abgeleitet aus dem beobachteten Verhalten).

`UrlDir` und `UrlPrevious` enden auf `?`: das Radio haengt seine Parameter an. `StationUrl`
ruft es dagegen unveraendert auf."""

from __future__ import annotations

import re
import unicodedata
from xml.sax.saxutils import escape

HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


_TYPOGRAFIE = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2018": "'", "\u2019": "'", "\u201a": "'",
                             "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ", "\u2022": "*"})


def _ascii(ch: str) -> str:
    return unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")


def umlaute(text: str, mode: str = "umschreiben") -> str:
    """`umschreiben`: ae/oe/ue/ss, Rest per NFKD auf ASCII (wie Airable fuer dieses Radio).
    `utf8`: Zeichen bis U+00FF bleiben (Umlaute, ss, e-akut: das Display des IWR 294 zeigt sie),
    typografische Zeichen werden ersetzt, alles andere (Emoji, kyrillisch, ...) faellt auf ASCII
    zurueck oder weg, weil nicht bekannt ist, was das Display damit macht."""
    text = _CTRL.sub("", str(text))
    if mode != "utf8":
        return unicodedata.normalize("NFKD", text.translate(_UMLAUTE)).encode("ascii", "ignore").decode("ascii")
    out = []
    for ch in text.translate(_TYPOGRAFIE):
        out.append(ch if ord(ch) <= 0xFF else _ascii(ch))
    return "".join(out)


class Xml:
    def __init__(self, mode: str = "umschreiben"):
        self.mode = mode

    def t(self, text) -> str:
        return escape(umlaute(text, self.mode))

    def dir(self, title: str, url: str) -> str:
        return (f"<Item><ItemType>Dir</ItemType><Title>{self.t(title)}</Title>"
                f"<UrlDir>{escape(url)}</UrlDir><UrlDirBackUp>{escape(url)}</UrlDirBackUp></Item>")

    def previous(self, url: str) -> str:
        return (f"<Item><ItemType>Previous</ItemType><UrlPrevious>{escape(url)}</UrlPrevious>"
                f"<UrlPreviousBackUp>{escape(url)}</UrlPreviousBackUp></Item>")

    def display(self, text: str) -> str:
        return f"<Item><ItemType>Display</ItemType><Display>{self.t(text)}</Display></Item>"

    def station(self, sid: str, name: str, url: str, desc: str = "", fmt: str = "", location: str = "",
                bandwidth="", mime: str = "MP3") -> str:
        return (f"<Item><ItemType>Station</ItemType><StationId>{escape(str(sid))}</StationId>"
                f"<StationName>{self.t(name)}</StationName><StationUrl>{escape(url)}</StationUrl>"
                f"<StationDesc>{self.t(desc)}</StationDesc><StationFormat>{self.t(fmt)}</StationFormat>"
                f"<StationLocation>{self.t(location)}</StationLocation>"
                f"<StationBandWidth>{escape(str(bandwidth or ''))}</StationBandWidth>"
                f"<StationMime>{escape(mime)}</StationMime><Relia>5</Relia></Item>")

    def show(self, sid: str, name: str, url: str) -> str:
        """Ein Podcast (das Radio ruft `url` unveraendert auf)."""
        u = escape(url)
        return (f"<Item><ItemType>ShowOnDemand</ItemType><ShowOnDemandID>{escape(str(sid))}</ShowOnDemandID>"
                f"<ShowOnDemandName>{self.t(name)}</ShowOnDemandName><ShowOnDemandURL>{u}</ShowOnDemandURL>"
                f"<ShowOnDemandURLBackUp>{u}</ShowOnDemandURLBackUp></Item>")

    def episode(self, eid: str, show: str, name: str, url: str, desc: str = "", fmt: str = "",
                lang: str = "", mime: str = "MP3") -> str:
        return (f"<Item><ItemType>ShowEpisode</ItemType><ShowEpisodeID>{escape(str(eid))}</ShowEpisodeID>"
                f"<ShowName>{self.t(show)}</ShowName><ShowEpisodeName>{self.t(name)}</ShowEpisodeName>"
                f"<ShowEpisodeURL>{escape(url)}</ShowEpisodeURL><ShowDesc>{self.t(desc)}</ShowDesc>"
                f"<ShowFormat>{self.t(fmt)}</ShowFormat><Lang>{self.t(lang)}</Lang><Country></Country>"
                f"<ShowMime>{escape(mime)}</ShowMime></Item>")

    def listing(self, items: list[str], count: int | None = None, previous: str | None = None) -> bytes:
        """`ItemCount` zaehlt ohne `Previous`. Der Aufrufer reicht schon die Seite (<= 100)."""
        n = len(items) if count is None else count
        body = ([self.previous(previous)] if previous else []) + items
        return (HEAD + f"<ListOfItems><ItemCount>{n}</ItemCount>" + "".join(body) + "</ListOfItems>\n").encode("utf-8")


def page(entries: list, start: int, end: int) -> list:
    """Das Radio blaettert mit startItems/endItems (1-basiert, hoechstens 100 je Seite)."""
    start = max(1, start)
    end = min(end, start + 99)
    return entries[start - 1:end]
