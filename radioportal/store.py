"""Ablage der erfassten Airable-Sender als JSON (atomar geschrieben, hinter einem Lock)."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


def jetzt() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class AirableStore:
    """`airable.json`: {airable_id: {name, format, ort, bitrate, airable_url, ersatz_id,
    zuerst, zuletzt, gespielt}}. `ersatz_id` bleibt leer, bis ein Ersatz-Sender gewaehlt ist."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("kein Objekt")
            self._data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            kaputt = self.path.with_name(self.path.name + ".kaputt")
            log.error("%s unlesbar (%s), liegt jetzt als %s", self.path, e, kaputt)
            try:
                os.replace(self.path, kaputt)
            except OSError:
                pass

    def _eintrag(self, station_id: str) -> dict:
        eintrag = self._data.get(station_id)
        if eintrag is None:
            eintrag = {"name": "", "format": "", "ort": "", "bitrate": "", "airable_url": "",
                       "ersatz_id": "", "zuerst": jetzt(), "zuletzt": "", "gespielt": 0}
            self._data[station_id] = eintrag
        return eintrag

    def note_seen(self, station_id: str, name: str = "", format: str = "", ort: str = "",
                  bitrate: str = "") -> None:
        """Das Radio hat die ID nachgeschlagen (Stationstaste, FAV, letzter Sender)."""
        with self._lock:
            e = self._eintrag(station_id)
            for key, wert in (("name", name), ("format", format), ("ort", ort), ("bitrate", bitrate)):
                if wert:
                    e[key] = wert
            e["zuletzt"] = jetzt()
            write_json_atomic(self.path, self._data)

    def note_play(self, station_id: str, url: str) -> None:
        """Das Radio hat die Stream-Adresse zur ID geholt."""
        with self._lock:
            e = self._eintrag(station_id)
            e["airable_url"] = url
            e["gespielt"] = int(e.get("gespielt", 0)) + 1
            e["zuletzt"] = jetzt()
            write_json_atomic(self.path, self._data)

    def set_ersatz(self, airable_id: str, sender_id: str | None) -> bool:
        """Legt fest, welcher eigene Sender die Airable-ID ersetzt (None: Ersatz entfernen)."""
        with self._lock:
            e = self._data.get(airable_id)
            if e is None:
                return False
            e["ersatz_id"] = sender_id or ""
            write_json_atomic(self.path, self._data)
            return True

    def ersatz_of(self, airable_id: str) -> str:
        with self._lock:
            return (self._data.get(airable_id) or {}).get("ersatz_id", "")

    def ersatz_ids(self) -> set[str]:
        with self._lock:
            return {e["ersatz_id"] for e in self._data.values() if e.get("ersatz_id")}

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._data)


class RadioStatus:
    """Wann hat sich welches Radio zuletzt am Portal gemeldet? (`radio.json`)

    Fragt ein Radio statt unseres DNS die FRITZ!Box, laeuft es still ueber Airable und meldet
    sich hier nicht mehr: Die Statusseite zeigt das, ohne dass man am Radio nachsehen muss."""

    SAVE_EVERY = 30  # Sekunden

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._saved = 0.0
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = data
        except (OSError, ValueError):
            pass

    def touch(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            neu = ip not in self._data
            e = self._data.setdefault(ip, {"zuerst": jetzt(), "anfragen": 0})
            e["zuletzt"] = jetzt()
            e["anfragen"] = int(e.get("anfragen", 0)) + 1
            if neu or now - self._saved >= self.SAVE_EVERY:
                self._saved = now
                try:
                    write_json_atomic(self.path, self._data)
                except OSError:
                    log.exception("radio.json nicht geschrieben")

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._data)
