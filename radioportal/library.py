"""Eigene Sender und die Senderliste am Radio (`sender.json`, `favoriten.json`).

Eigene IDs sind Ganzzahlen ab 1000001, aufsteigend und werden nie neu vergeben: Die FAV-Liste
des Radios speichert sie. Deshalb gilt: Eine ID spielt immer denselben Sender. Name und Adresse
lassen sich aendern, Ausblenden nimmt einen Sender nur aus der Liste (auf der FAV-Taste spielt er
weiter). Loeschen geht nur bei Sendern, die das Radio nie nachgeschlagen hat, denn nur die koennen
nicht auf einem FAV-Platz liegen (Speichern geht nur, waehrend der Sender spielt)."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import threading
from pathlib import Path

from .store import jetzt, write_json_atomic

log = logging.getLogger(__name__)

FIRST_ID = 1000001
RADIO_MERKEN_ALLE = 600  # Sekunden: "zuletzt am Radio" nicht bei jedem Aufruf neu schreiben


def _read(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        kaputt = path.with_name(path.name + ".kaputt")
        log.error("%s unlesbar (%s), liegt jetzt als %s", path, e, kaputt)
        try:
            os.replace(path, kaputt)
        except OSError:
            pass
        return default


def _alter(zeit: str) -> float:
    try:
        return (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(zeit)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


class Library:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        data = _read(self.dir / "sender.json", {})
        self._sender: dict[str, dict] = data.get("sender", {}) if isinstance(data, dict) else {}
        self._next: int = int(data.get("naechste_id", FIRST_ID)) if isinstance(data, dict) else FIRST_ID
        liste = _read(self.dir / "favoriten.json", [])
        self._liste: list[str] = [f for f in liste if isinstance(f, str)] if isinstance(liste, list) else []
        # IDs nie neu vergeben, auch wenn sender.json fehlt oder alt ist
        for sid in self._sender:
            if sid.isdigit() and int(sid) >= self._next:
                self._next = int(sid) + 1

    # --- Speichern (immer unter dem Lock) -------------------------------------
    def _save_sender(self) -> None:
        write_json_atomic(self.dir / "sender.json", {"naechste_id": self._next, "sender": self._sender})

    def _save_liste(self) -> None:
        write_json_atomic(self.dir / "favoriten.json", self._liste)

    # --- Sender ---------------------------------------------------------------
    def add_sender(self, *, name: str, url: str, codec: str = "", bitrate="", land: str = "",
                   genre: str = "", quelle: str = "manuell", rb_uuid: str = "", url_orig: str = "",
                   hinweis: str = "", tags: str = "", stream_text: str = "", stream_genre: str = "") -> str:
        """Legt einen Sender an und gibt seine ID zurueck. Gleicher radio-browser-Sender oder
        gleiche Adresse: der vorhandene wird zurueckgegeben."""
        with self._lock:
            for sid, s in self._sender.items():
                if (rb_uuid and s.get("rb_uuid") == rb_uuid) or s.get("url") == url:
                    return sid
            sid = str(self._next)
            self._next += 1
            # radio_zuletzt "" = vom Radio noch nie nachgeschlagen (Sender aus der Zeit davor haben
            # das Feld nicht und gelten vorsichtshalber als gespielt)
            self._sender[sid] = {"id": sid, "name": name.strip(), "url": url, "url_orig": url_orig or url,
                                 "codec": codec, "bitrate": str(bitrate or ""), "land": land,
                                 "genre": genre, "quelle": quelle, "rb_uuid": rb_uuid,
                                 "hinweis": hinweis, "tags": tags, "stream_text": stream_text,
                                 "stream_genre": stream_genre, "angelegt": jetzt(), "radio_zuletzt": ""}
            self._save_sender()
            return sid

    def update_sender(self, sid: str, **felder) -> bool:
        with self._lock:
            s = self._sender.get(sid)
            if s is None:
                return False
            s.update(felder)
            self._save_sender()
            return True

    def sender(self, sid: str) -> dict | None:
        with self._lock:
            s = self._sender.get(str(sid))
            return copy.deepcopy(s) if s else None

    def all_senders(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._sender)

    def note_radio(self, sid: str) -> None:
        """Das Radio hat den Sender nachgeschlagen oder abgespielt."""
        with self._lock:
            s = self._sender.get(sid)
            if s is None or _alter(s.get("radio_zuletzt") or "") < RADIO_MERKEN_ALLE:
                return
            s["radio_zuletzt"] = jetzt()
            try:
                self._save_sender()
            except OSError:
                log.exception("sender.json nicht geschrieben")

    def loeschbar(self, sid: str) -> bool:
        with self._lock:
            s = self._sender.get(sid)
            return s is not None and sid not in self._liste and s.get("radio_zuletzt") == ""

    def remove_sender(self, sid: str) -> bool:
        """Nur ausgeblendete Sender, die das Radio nie nachgeschlagen hat. Die ID bleibt vergeben."""
        with self._lock:
            s = self._sender.get(sid)
            if s is None or sid in self._liste or s.get("radio_zuletzt") != "":
                return False
            del self._sender[sid]
            self._save_sender()
            return True

    # --- Senderliste am Radio -------------------------------------------------
    def liste(self) -> list[str]:
        with self._lock:
            return [sid for sid in self._liste if sid in self._sender]

    def ausgeblendet(self) -> list[str]:
        with self._lock:
            return sorted((sid for sid in self._sender if sid not in self._liste), key=int)

    def zeigen(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._sender or sid in self._liste:
                return False
            self._liste.append(sid)
            self._save_liste()
            return True

    def ausblenden(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._liste:
                return False
            self._liste.remove(sid)
            self._save_liste()
            return True

    def verschieben(self, sid: str, delta: int) -> bool:
        with self._lock:
            if sid not in self._liste:
                return False
            i = self._liste.index(sid)
            j = i + delta
            if not 0 <= j < len(self._liste):
                return False
            self._liste[i], self._liste[j] = self._liste[j], self._liste[i]
            self._save_liste()
            return True

    def platz_setzen(self, sid: str, platz: int) -> bool:
        """Sender auf Platz `platz` (1 = oben) der Liste setzen, die anderen ruecken nach."""
        with self._lock:
            if sid not in self._liste:
                return False
            andere = [s for s in self._liste if s in self._sender and s != sid]
            platz = max(1, min(platz, len(andere) + 1))
            if [s for s in self._liste if s in self._sender].index(sid) == platz - 1:
                return False
            self._liste.remove(sid)
            if platz <= len(andere):
                self._liste.insert(self._liste.index(andere[platz - 1]), sid)
            else:
                self._liste.append(sid)
            self._save_liste()
            return True
