"""Eigene Sender und Favoriten (`sender.json`, `favoriten.json`).

Eigene IDs sind Ganzzahlen ab 1000001, aufsteigend und werden nie neu vergeben (die
FAV-Liste und Stationstasten des Radios speichern sie). Airable-IDs haben 16 Stellen, es gibt also keine
Ueberschneidung."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path

from .store import jetzt, write_json_atomic

log = logging.getLogger(__name__)

FIRST_ID = 1000001
MAX_ITEMS_PER_PAGE = 100


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


class Library:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        data = _read(self.dir / "sender.json", {})
        self._sender: dict[str, dict] = data.get("sender", {}) if isinstance(data, dict) else {}
        self._next: int = int(data.get("naechste_id", FIRST_ID)) if isinstance(data, dict) else FIRST_ID
        favs = _read(self.dir / "favoriten.json", [])
        self._favs: list[str] = [f for f in favs if isinstance(f, str)] if isinstance(favs, list) else []
        # IDs nie neu vergeben, auch wenn sender.json fehlt oder alt ist
        for sid in self._sender:
            if sid.isdigit() and int(sid) >= self._next:
                self._next = int(sid) + 1

    # --- Speichern (immer unter dem Lock) -------------------------------------
    def _save_sender(self) -> None:
        write_json_atomic(self.dir / "sender.json", {"naechste_id": self._next, "sender": self._sender})

    def _save_favs(self) -> None:
        write_json_atomic(self.dir / "favoriten.json", self._favs)

    # --- Sender ---------------------------------------------------------------
    def add_sender(self, *, name: str, url: str, codec: str = "", bitrate="", land: str = "",
                   genre: str = "", quelle: str = "manuell", rb_uuid: str = "", url_orig: str = "",
                   hinweis: str = "") -> str:
        """Legt einen Sender an und gibt seine ID zurueck. Gleicher radio-browser-Sender oder
        gleiche Adresse: der vorhandene wird zurueckgegeben."""
        with self._lock:
            for sid, s in self._sender.items():
                if (rb_uuid and s.get("rb_uuid") == rb_uuid) or s.get("url") == url:
                    return sid
            sid = str(self._next)
            self._next += 1
            self._sender[sid] = {"id": sid, "name": name.strip(), "url": url, "url_orig": url_orig or url,
                                 "codec": codec, "bitrate": str(bitrate or ""), "land": land,
                                 "genre": genre, "quelle": quelle, "rb_uuid": rb_uuid,
                                 "hinweis": hinweis, "angelegt": jetzt()}
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

    def remove_sender(self, sid: str, in_use: set[str]) -> bool:
        """Entfernt einen Sender, wenn er in keiner Favoritenliste und bei keiner Taste
        verwendet wird. Die ID bleibt vergeben."""
        with self._lock:
            if sid not in self._sender or sid in self._favs or sid in in_use:
                return False
            del self._sender[sid]
            self._save_sender()
            return True

    # --- Favoriten ------------------------------------------------------------
    def favorites(self) -> list[str]:
        with self._lock:
            return list(self._favs)

    def fav_add(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._sender or sid in self._favs:
                return False
            self._favs.append(sid)
            self._save_favs()
            return True

    def fav_remove(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._favs:
                return False
            self._favs.remove(sid)
            self._save_favs()
            return True

    def fav_move(self, sid: str, delta: int) -> bool:
        with self._lock:
            if sid not in self._favs:
                return False
            i = self._favs.index(sid)
            j = i + delta
            if not 0 <= j < len(self._favs):
                return False
            self._favs[i], self._favs[j] = self._favs[j], self._favs[i]
            self._save_favs()
            return True
