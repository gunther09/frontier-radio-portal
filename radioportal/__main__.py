"""Start: python3 -m radioportal"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from . import __version__, config, podcasts, server
from .radiobrowser import RadioBrowserError


REFRESH_EVERY = 2 * 3600


def _refresher(portal, stop: threading.Event) -> None:
    """Prueft die abonnierten Podcasts regelmaessig auf neue Folgen."""
    stop.wait(120)
    while not stop.is_set():
        try:
            res = podcasts.refresh_all(portal.podcasts, portal.streamer)
            neu = sum(v for v in res.values() if isinstance(v, int))
            if neu:
                logging.info("Podcasts: %d neue Folgen", neu)
        except Exception:  # noqa: BLE001 - die Aktualisierung darf nie den Dienst beenden
            logging.exception("Podcast-Aktualisierung fehlgeschlagen")
        stop.wait(REFRESH_EVERY)


def nachtragen(portal) -> int:
    """Sender aus der Zeit vor 0.3.1 haben weder Stream-Beschreibung noch radio-browser-Tags:
    einmal nachholen (Feld `stream_text` fehlt = noch nie nachgesehen)."""
    n = 0
    for sid, s in portal.library.all_senders().items():
        if "stream_text" in s:
            continue
        felder = {"stream_text": "", "stream_genre": ""}
        r = portal.prober(s.get("url_orig") or s["url"])
        if r.ok:
            felder.update(stream_text=r.text, stream_genre=r.genre)
        if s.get("rb_uuid") and not s.get("tags"):
            try:
                st = portal.rb.by_uuid(s["rb_uuid"])
                felder["tags"] = st["tags"] if st else ""
            except RadioBrowserError:
                continue  # beim naechsten Start wieder
        portal.library.update_sender(sid, **felder)
        n += 1
    return n


def _nachtragen(portal, stop: threading.Event) -> None:
    stop.wait(10)
    try:
        if n := nachtragen(portal):
            logging.info("Sender-Beschreibungen nachgetragen: %d", n)
    except Exception:  # noqa: BLE001
        logging.exception("Nachtragen der Sender-Beschreibungen fehlgeschlagen")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    cfg = config.load()
    srv = server.make_server(cfg)
    stop = threading.Event()
    threading.Thread(target=_refresher, args=(srv.portal, stop), daemon=True).start()
    threading.Thread(target=_nachtragen, args=(srv.portal, stop), daemon=True).start()
    # systemd beendet mit SIGTERM: sauber herunterfahren statt mitten im Schreiben sterben.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    logging.info("radio-portal %s lauscht auf %s:%d, Daten in %s", __version__, cfg.host, cfg.port, cfg.data_dir)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
