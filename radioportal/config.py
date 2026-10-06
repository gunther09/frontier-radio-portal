"""Konfiguration aus Umgebungsvariablen (auf dem Pi aus /etc/radio-portal/radio-portal.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8095
    data_dir: Path = Path("daten")
    # Eigene Adresse: Schutz davor, dass die Weiche sich selbst als Airable ansieht.
    pi_ip: str = ""
    # Zeitlimit fuer Anfragen, die an Airable durchgereicht werden
    upstream_timeout: float = 5.0
    # umschreiben (ae/oe/ue/ss, wie Airable) oder utf8 (das Display des IWR 294 kann Umlaute)
    umlaute: str = "umschreiben"
    # IP-Adressen der Radios (nur fuer die Statusanzeige; leer: jeder Absender zaehlt)
    radio_ips: tuple = ()
    # Adresse der Oberflaeche, wie sie am Radio angezeigt wird (z. B. http://server:8095)
    portal_url: str = ""


def load(env=None) -> Config:
    env = os.environ if env is None else env
    return Config(
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8095")),
        data_dir=Path(env.get("DATA_DIR", "daten")),
        pi_ip=env.get("PI_IP", ""),
        upstream_timeout=float(env.get("UPSTREAM_TIMEOUT", "5")),
        umlaute="utf8" if env.get("UMLAUTE", "umschreiben").strip().lower() == "utf8" else "umschreiben",
        radio_ips=tuple(env.get("RADIO_IPS", "").replace(",", " ").split()),
        portal_url=env.get("PORTAL_URL", "").strip(),
    )
