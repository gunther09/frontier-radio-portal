"""HTTP-Server: Anfragen mit `Host: *.wifiradiofrontier.com` kommen vom Radio (Weiche),
alle anderen sind die Weboberflaeche."""

from __future__ import annotations

import http.client
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import probe as probe_mod
from . import radio, web
from .airable import HOP, Forwarder, Upstream, UpstreamError, is_radio_host
from .config import Config
from .library import Library
from .podcasts import PodcastStore
from .radiobrowser import RadioBrowser
from .store import RadioLog, RadioStatus
from .stream import Registry, StreamError, Streamer
from .xmlitems import Xml

log = logging.getLogger(__name__)

MAX_REQUEST_BODY = 1024 * 1024
NO_CACHE = (("Cache-Control", "no-store, no-cache, must-revalidate"), ("Pragma", "no-cache"))


class Portal:
    def __init__(self, cfg: Config, forwarder: Forwarder | None = None):
        self.cfg = cfg
        self.library = Library(cfg.data_dir)
        self.podcasts = PodcastStore(cfg.data_dir / "podcasts.json")
        self.podcast_search = None  # None: echte iTunes-Suche; Tests setzen eine Attrappe
        self.radios = RadioStatus(cfg.data_dir / "radio.json")
        self.anfragen = RadioLog()
        self.xml = Xml(cfg.umlaute)
        self.play_cache: dict = {}
        self.rb = RadioBrowser()
        self.prober = probe_mod.probe
        self.forwarder = forwarder or Forwarder(timeout=cfg.upstream_timeout, pi_ip=cfg.pi_ip)
        self.streamer = Streamer()
        self.relay_ids = Registry()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # eine Antwort je Verbindung, immer mit Content-Length
    server_version = "radio-portal"
    timeout = 30

    def log_message(self, *args):
        pass

    @property
    def portal(self) -> Portal:
        return self.server.portal

    def do_GET(self):
        try:
            self._dispatch()
        except Exception:  # noqa: BLE001 - nie ohne Antwort abbrechen
            log.exception("Fehler bei %s %s", self.command, self.path)
            self._send(500, b"Fehler.\n", "text/plain; charset=utf-8")

    do_HEAD = do_POST = do_GET

    def _dispatch(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BODY:
            self._send(413, b"Zu gross.\n", "text/plain; charset=utf-8")
            return
        body = self.rfile.read(length) if length else b""
        host = (self.headers.get("Host") or "").split(":")[0].lower().rstrip(".")
        if is_radio_host(host):
            self._radio(host, body)
        else:
            path, _, query = self.path.partition("?")
            res = web.handle(self.portal, self.command, path, query, dict(self.headers.items()), body)
            self._send(res.status, res.body, res.ctype, res.headers)

    def _merke(self, status: int, weg: str):
        """Eine Zeile im Anfrage-Protokoll (Startseite der Oberflaeche)."""
        path, _, query = self.path.partition("?")
        was, sid = radio.beschreibe(path, query)
        self.portal.anfragen.add(self.client_address[0], was, status, weg, sid)

    def _radio(self, host: str, body: bytes):
        path, _, query = self.path.partition("?")
        ip = self.client_address[0]
        if not self.portal.cfg.radio_ips or ip in self.portal.cfg.radio_ips:
            self.portal.radios.touch(ip)
        if path.lower().endswith("/findupdate.aspx"):
            # Die Anfrage enthaelt die echte MAC: nie weiterreichen, nie protokollieren.
            self._merke(404, "Portal")
            self._send(404, b"Not Found\n", "text/plain; charset=utf-8")
            return
        if path.startswith("/portal/stream/"):
            self._merke(200, "Server holt")
            self._relay(path)
            return
        if path.startswith("/portal/live/"):
            self._merke(200, "Server holt")
            self._live(path)
            return
        try:
            own = radio.handle(self.portal, host, self.command, path, query, list(self.headers.items()))
        except Exception:  # noqa: BLE001 - bei einem Fehler lieber Airable fragen als einen 500er liefern
            log.exception("Eigene Antwort fehlgeschlagen fuer %s", path)
            own = None
        if own is not None:
            self._merke(own.status, "Portal")
            self._send(own.status, own.body, own.ctype, own.headers)
            return
        try:
            up = self.portal.forwarder.forward(self.command, host, self.path, self.headers.items(), body)
        except UpstreamError as e:
            log.warning("Airable: %s %s%s -> %s", self.command, host, path, e)
            self._merke(502, "Airable")
            self._send(502, b"Airable nicht erreichbar.\n", "text/plain; charset=utf-8")
            return
        self._merke(up.status, "Airable")
        self._send_upstream(up)

    def _relay(self, path: str):
        """Holt eine zuvor eingetragene Adresse (auch https) und gibt sie als http weiter."""
        sid = path.rsplit("/", 1)[-1].split(".")[0]
        url = self.portal.relay_ids.get(sid)
        if not url:
            self._send(404, b"Unbekannt.\n", "text/plain; charset=utf-8")
            return
        self._relay_url(url)

    def _live(self, path: str):
        """Sender, die nur per https erreichbar sind: der Server holt den Strom."""
        s = self.portal.library.sender(path.rsplit("/", 1)[-1].split(".")[0])
        if not s:
            self._send(404, b"Unbekannt.\n", "text/plain; charset=utf-8")
            return
        self._relay_url(s["url"])

    def _relay_url(self, url: str):
        headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        try:
            o = self.portal.streamer.open(url, headers)
        except StreamError as e:
            log.warning("Relay: %s", e)
            self._send(502, b"Quelle nicht erreichbar.\n", "text/plain; charset=utf-8")
            return
        resp = o.resp
        try:
            self.send_response(resp.status, resp.reason)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "audio/mpeg")
            for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if resp.getheader(name):
                    self.send_header(name, resp.getheader(name))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            pass  # Radio hat aufgehoert oder die Quelle ist abgerissen
        finally:
            o.conn.close()

    def _send_upstream(self, up: Upstream):
        self.send_response(up.status, up.reason)
        for k, v in up.headers:
            if k.lower() not in HOP and k.lower() not in ("content-length", "server", "date"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(up.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(up.body)

    def _send(self, status: int, body: bytes, ctype: str, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        for k, v in NO_CACHE:
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(cfg: Config, forwarder: Forwarder | None = None) -> Server:
    server = Server((cfg.host, cfg.port), Handler)
    server.portal = Portal(cfg, forwarder)
    return server
