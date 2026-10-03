"""Start: python3 -m radioportal"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from . import __version__, config, podcasts, server


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


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    cfg = config.load()
    srv = server.make_server(cfg)
    stop = threading.Event()
    threading.Thread(target=_refresher, args=(srv.portal, stop), daemon=True).start()
    # systemd beendet mit SIGTERM: sauber herunterfahren statt mitten im Schreiben sterben.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    logging.info("radio-portal %s lauscht auf %s:%d, Daten in %s, Mitschnitt %s", __version__,
                 cfg.host, cfg.port, cfg.data_dir, "an" if cfg.mitschnitt else "aus")
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
