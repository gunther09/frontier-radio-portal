"""Konfiguration aus Umgebungsvariablen (auf dem Pi aus /etc/radio-portal/radio-portal.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _ja(wert: str) -> bool:
    return str(wert).strip().lower() in ("ja", "1", "true", "yes", "an")


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8095
    data_dir: Path = Path("daten")
    # Durchgereichte Antworten als Dateien ablegen und jede Anfrage ins Journal schreiben.
    mitschnitt: bool = False
    # Eigene Adresse: Schutz davor, dass die Weiche sich selbst als Airable ansieht.
    pi_ip: str = ""
    upstream_timeout: float = 10.0
    # umschreiben (ae/oe/ue/ss, wie Airable) oder utf8; das Testmenue klaert, was das Display kann
    umlaute: str = "umschreiben"
    testmenue: bool = False
    # IP-Adressen der Radios (nur fuer die Statusanzeige; leer: jeder Absender zaehlt)
    radio_ips: tuple = ()
    # Adresse der Oberflaeche, wie sie am Radio angezeigt wird (z. B. http://server:8095)
    portal_url: str = ""
    # Airable-Menues, die im Hauptmenue fehlen sollen: Teile ihrer Adresse (z. B. "help country=de")
    airable_ausblenden: tuple = ()


def load(env=None) -> Config:
    env = os.environ if env is None else env
    return Config(
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8095")),
        data_dir=Path(env.get("DATA_DIR", "daten")),
        mitschnitt=_ja(env.get("MITSCHNITT", "nein")),
        pi_ip=env.get("PI_IP", ""),
        upstream_timeout=float(env.get("UPSTREAM_TIMEOUT", "10")),
        umlaute="utf8" if env.get("UMLAUTE", "umschreiben").strip().lower() == "utf8" else "umschreiben",
        testmenue=_ja(env.get("TESTMENUE", "nein")),
        radio_ips=tuple(env.get("RADIO_IPS", "").replace(",", " ").split()),
        portal_url=env.get("PORTAL_URL", "").strip(),
        airable_ausblenden=tuple(env.get("AIRABLE_AUSBLENDEN", "").replace(",", " ").split()),
    )
