"""Kleine Ablagen: JSON atomar schreiben, Statusanzeige der Radios, Anfrage-Protokoll."""

from __future__ import annotations

import collections
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


class RadioLog:
    """Die letzten Anfragen der Radios, nur im Speicher (nach einem Neustart leer).

    Fuer die Fehlersuche auf der Startseite: Laeuft das Radio ueber uns, was fragt es, was
    antworten wir? Ohne die Kennung des Radios (`mac`) und ohne Query-Parameter."""

    def __init__(self, maximum: int = 200):
        self._lock = threading.Lock()
        self._items: collections.deque = collections.deque(maxlen=maximum)

    def add(self, ip: str, was: str, status: int, weg: str, sid: str = "") -> None:
        with self._lock:
            self._items.append({"zeit": jetzt(), "ip": ip, "was": was, "status": status, "weg": weg, "id": sid})

    def snapshot(self) -> list[dict]:
        """Neueste zuerst."""
        with self._lock:
            return [dict(e) for e in reversed(self._items)]
