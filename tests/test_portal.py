"""Tests mit erfundenen Beispielen (keine echten Mitschnitte)."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from radioportal import airable, config, server
from radioportal.airable import Forwarder, Upstream, UpstreamError, parse_stations
from radioportal.store import AirableStore
from radioportal.stream import Probe, Registry, StreamError, Streamer

STATION_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ListOfItems><ItemCount>1</ItemCount>
<Item><ItemType>Previous</ItemType><UrlPrevious>http://aldi.wifiradiofrontier.com/vtuner?</UrlPrevious></Item>
<Item><ItemType>Station</ItemType><StationId>1234567890123456</StationId>
<StationName>Test &amp; Radio</StationName><StationUrl>http://x/play</StationUrl>
<StationFormat>Rock</StationFormat><StationLocation>Germany &gt; Bavaria</StationLocation>
<StationBandWidth>128</StationBandWidth><StationMime>MP3</StationMime></Item>
</ListOfItems>""".encode()


class FakeAirable(BaseHTTPRequestHandler):
    seen = []
    audio_base = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeAirable.seen.append((self.path, self.headers.get("Host")))
        if "/vtuner/play/episode=" in self.path:
            self.send_response(302)
            self.send_header("Location", FakeAirable.audio_base + "/a")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "/Search.asp" in self.path:
            body, ctype = STATION_XML, "text/html; charset=UTF-8"
        elif "/vtuner/play/" in self.path:
            body, ctype = b"http://stream.example.org/live.mp3", "audio/x-mpegurl"
        elif "/vtuner/podcast=" in self.path:
            # wie Airable: ohne mac gibt es nur einen 500er
            ok = "mac=" in self.path
            body, ctype = (b"<ListOfItems/>" if ok else b""), "text/html"
            self.send_response(200 if ok else 500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif "loginXML.asp?token=0" in self.path:
            body, ctype = b"<EncryptedToken>0123456789abcdef</EncryptedToken>", "text/html"
        else:
            body, ctype = b"anderes", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


AUDIO = b"0123456789"


class FakeAudio(BaseHTTPRequestHandler):
    """Podcast-Server: /a leitet auf /b weiter (zweite Weiterleitung), /loop leitet auf sich selbst."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/a":
            self.send_response(302)
            self.send_header("Location", "/b")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/b":
            rng = self.headers.get("Range", "")
            body, status = AUDIO, 200
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                body, status = AUDIO[int(a):int(b) + 1 if b else None], 206
            self.send_response(status)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio = ThreadingHTTPServer(("127.0.0.1", 0), FakeAudio)
        threading.Thread(target=cls.audio.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.audio.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.audio.shutdown()
        cls.audio.server_close()

    def test_kette_wird_aufgeloest(self):
        p = Streamer(allow_private=True).probe(self.base + "/a")
        self.assertEqual((p.final_url, p.needs_relay), (self.base + "/b", False))
        self.assertIn(p.status, (200, 206))
        self.assertEqual(p.content_type, "audio/mpeg")

    def test_schleife(self):
        with self.assertRaises(StreamError):
            Streamer(allow_private=True).probe(self.base + "/loop")

    def test_nichts_aus_dem_eigenen_netz(self):
        for url in ("http://127.0.0.1:1/", "http://192.168.1.1/", "http://10.0.0.5/x",
                    "http://169.254.169.254/", "ftp://example.org/x", "file:///etc/passwd"):
            with self.assertRaises(StreamError, msg=url):
                Streamer().probe(url)

    def test_https_in_der_kette_braucht_relay(self):
        class Fake(Streamer):
            def _open_once(self, url, headers):
                if url.startswith("http://start"):
                    return _Conn(), _Resp(301, {"Location": "https://cdn.example/x.mp3"})
                return _Conn(), _Resp(200, {"Content-Type": "audio/mpeg"})
        p = Fake().probe("http://start.example/x")
        self.assertTrue(p.needs_relay)
        self.assertEqual(p.final_url, "https://cdn.example/x.mp3")

    def test_registry(self):
        r = Registry(maximum=2)
        a, b, c = r.add("http://a"), r.add("http://b"), r.add("http://c")
        self.assertIsNone(r.get(a))
        self.assertEqual((r.get(b), r.get(c)), ("http://b", "http://c"))
        self.assertIsNone(r.get("gibtsnicht"))


class _Conn:
    def close(self):
        pass


class _Resp:
    def __init__(self, status, headers):
        self.status, self._h = status, headers

    def getheader(self, name, default=None):
        return self._h.get(name, default)


class ParseTests(unittest.TestCase):
    def test_stationen(self):
        st = parse_stations(STATION_XML)
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["id"], "1234567890123456")
        self.assertEqual(st[0]["name"], "Test & Radio")
        self.assertEqual(st[0]["ort"], "Germany > Bavaria")
        self.assertEqual(st[0]["bitrate"], "128")

    def test_kaputtes_und_gefaehrliches_xml(self):
        self.assertEqual(parse_stations(b"<ListOfItems><Item>"), [])
        self.assertEqual(parse_stations(b'<!DOCTYPE x [<!ENTITY a "b">]><ListOfItems/>'), [])

    def test_radio_host(self):
        self.assertTrue(airable.is_radio_host("aldi.wifiradiofrontier.com"))
        self.assertTrue(airable.is_radio_host("ALDI2.wifiradiofrontier.com."))
        self.assertFalse(airable.is_radio_host("wifiradiofrontier.com.evil.org"))
        self.assertFalse(airable.is_radio_host("evilwifiradiofrontier.com"))
        self.assertFalse(airable.is_radio_host("192.168.1.50"))
        self.assertFalse(airable.is_radio_host(""))


class RadioParamsTests(unittest.TestCase):
    def setUp(self):
        self.p = airable.RadioParams()
        self.p.learn("10.0.0.5", "/vtuner/podcasts?&startItems=1&endItems=100&mac=abc123&dlang=ger&fver=1&ven=aldi1")

    def test_podcast_ohne_parameter_wird_ergaenzt(self):
        self.assertEqual(
            self.p.complete("10.0.0.5", "/vtuner/podcast=77?"),
            "/vtuner/podcast=77?&startItems=1&endItems=100&mac=abc123&dlang=ger&fver=1&ven=aldi1")

    def test_ohne_fragezeichen_und_mit_start(self):
        self.assertEqual(
            self.p.complete("10.0.0.5", "/vtuner/podcast=77"),
            "/vtuner/podcast=77?&startItems=1&endItems=100&mac=abc123&dlang=ger&fver=1&ven=aldi1")
        self.assertEqual(
            self.p.complete("10.0.0.5", "/vtuner/podcast=77?&startItems=101&endItems=200"),
            "/vtuner/podcast=77?&startItems=101&endItems=200&mac=abc123&dlang=ger&fver=1&ven=aldi1")

    def test_unveraendert(self):
        mit_mac = "/vtuner/podcasts?&mac=zzz&startItems=1"
        self.assertEqual(self.p.complete("10.0.0.5", mit_mac), mit_mac)
        self.assertEqual(self.p.complete("10.0.0.5", "/vtuner/play/episode=1?"), "/vtuner/play/episode=1?")
        self.assertEqual(self.p.complete("10.0.0.5", "/setupapp/aldi/x.asp?token=0"),
                         "/setupapp/aldi/x.asp?token=0")
        # anderes Radio, das wir nicht kennen
        self.assertEqual(self.p.complete("10.0.0.6", "/vtuner/podcast=77?"), "/vtuner/podcast=77?")

    def test_play_url_lehrt_nichts(self):
        self.p.learn("10.0.0.7", "/vtuner/play/station=1?mac=xyz&ven=aldi1&fver=1&dlang=en")
        self.assertEqual(self.p.complete("10.0.0.7", "/vtuner/podcast=77?"), "/vtuner/podcast=77?")


class StoreTests(unittest.TestCase):
    def test_atomar_und_neu_laden(self):
        with tempfile.TemporaryDirectory() as d:
            pfad = Path(d) / "airable.json"
            s = AirableStore(pfad)
            s.note_seen("42", name="Eins", bitrate="64")
            s.note_play("42", "http://a/b")
            s.note_play("42", "http://a/b")
            neu = AirableStore(pfad).snapshot()
            self.assertEqual(neu["42"]["name"], "Eins")
            self.assertEqual(neu["42"]["gespielt"], 2)
            self.assertEqual(neu["42"]["ersatz_id"], "")
            self.assertFalse(list(Path(d).glob("*.tmp")))

    def test_kaputte_datei_wird_beiseite_gelegt(self):
        with tempfile.TemporaryDirectory() as d:
            pfad = Path(d) / "airable.json"
            pfad.write_text("{kaputt", encoding="utf-8")
            self.assertEqual(AirableStore(pfad).snapshot(), {})
            self.assertTrue((Path(d) / "airable.json.kaputt").exists())


class ForwarderTests(unittest.TestCase):
    def test_schleife_und_fremde_hosts_abgelehnt(self):
        f = Forwarder(pi_ip="192.168.1.50", resolve=lambda h: "192.168.1.50")
        with self.assertRaises(UpstreamError):
            f.forward("GET", "aldi.wifiradiofrontier.com", "/", [])
        with self.assertRaises(UpstreamError):
            Forwarder(resolve=lambda h: "127.0.0.1").forward("GET", "aldi.wifiradiofrontier.com", "/", [])
        with self.assertRaises(UpstreamError):
            Forwarder(resolve=lambda h: "1.2.3.4").forward("GET", "example.org", "/", [])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.audio = ThreadingHTTPServer(("127.0.0.1", 0), FakeAudio)
        threading.Thread(target=cls.audio.serve_forever, daemon=True).start()
        FakeAirable.audio_base = f"http://127.0.0.1:{cls.audio.server_address[1]}"
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeAirable)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fwd = Forwarder(port=cls.fake.server_address[1], resolve=lambda h: "127.0.0.1",
                        refuse_loopback=False)
        cfg = config.Config(port=0, data_dir=Path(cls.tmp.name), mitschnitt=True)
        cls.srv = server.make_server(cfg, fwd)
        cls.srv.portal.streamer = Streamer(allow_private=True)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.fake.shutdown()
        cls.audio.shutdown()
        cls.audio.server_close()
        cls.srv.server_close()
        cls.fake.server_close()
        cls.tmp.cleanup()

    def get(self, path, host, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": host, **(headers or {})})
        r = c.getresponse()
        return r, r.read()

    def setUp(self):
        FakeAirable.seen.clear()

    def test_token_wird_lokal_beantwortet(self):
        r, body = self.get("/setupapp/aldi/asp/BrowseXML/loginXML.asp?token=0", "aldi.wifiradiofrontier.com")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"<EncryptedToken>3a3f5ac48a1dab4e</EncryptedToken>")
        self.assertEqual(len(body), 49)
        self.assertEqual(r.getheader("Content-Length"), "49")
        self.assertEqual(FakeAirable.seen, [])

    def test_unbekanntes_wird_durchgereicht(self):
        r, body = self.get("/vtuner/country=de?&startItems=1&mac=h", "aldi2.wifiradiofrontier.com")
        self.assertEqual((r.status, body), (200, b"anderes"))
        self.assertEqual(FakeAirable.seen[0][1], "aldi2.wifiradiofrontier.com")

    def test_nur_bekannte_radios_werden_gezaehlt(self):
        self.srv.portal.cfg = config.Config(port=0, data_dir=Path(self.tmp.name), radio_ips=("10.9.9.9",))
        try:
            vorher = self.srv.portal.radios.snapshot().get("127.0.0.1", {}).get("anfragen", 0)
            self.get("/vtuner/country=de?&mac=h", "aldi.wifiradiofrontier.com")
            nachher = self.srv.portal.radios.snapshot().get("127.0.0.1", {}).get("anfragen", 0)
            self.assertEqual(vorher, nachher)
        finally:
            self.srv.portal.cfg = config.Config(port=0, data_dir=Path(self.tmp.name), mitschnitt=True)

    def test_update_wird_lokal_mit_404_beantwortet(self):
        r, _ = self.get("/FindUpdate.aspx?mac=AABBCCDDEEFF&version=1", "update.wifiradiofrontier.com")
        self.assertEqual(r.status, 404)
        self.assertEqual(FakeAirable.seen, [])

    def test_sender_id_wird_erfasst(self):
        self.get("/setupapp/aldi/asp/BrowseXML/Search.asp?sSearchtype=3&Search=1234567890123456&mac=h",
                 "aldi.wifiradiofrontier.com")
        r, body = self.get("/vtuner/play/station=1234567890123456?mac=h&ven=aldi1",
                           "aldi2.wifiradiofrontier.com")
        self.assertEqual(body, b"http://stream.example.org/live.mp3")
        self.assertEqual(r.getheader("Content-Type"), "audio/x-mpegurl")
        e = self.srv.portal.store.snapshot()["1234567890123456"]
        self.assertEqual(e["name"], "Test & Radio")
        self.assertEqual(e["airable_url"], "http://stream.example.org/live.mp3")
        self.assertGreaterEqual(e["gespielt"], 1)

    def test_podcast_bekommt_parameter_des_radios(self):
        self.get("/vtuner/podcasts?&startItems=1&endItems=100&mac=h42&dlang=ger&fver=1&ven=aldi1",
                 "aldi.wifiradiofrontier.com")
        r, body = self.get("/vtuner/podcast=7591126125771553?", "aldi.wifiradiofrontier.com")
        self.assertEqual((r.status, body), (200, b"<ListOfItems/>"))
        self.assertIn("mac=h42", FakeAirable.seen[-1][0])

    def test_folge_direkt_auf_endadresse(self):
        r, _ = self.get("/vtuner/play/episode=111?mac=h&ven=aldi1", "aldi.wifiradiofrontier.com")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), FakeAirable.audio_base + "/b")

    def test_folge_ueber_relay(self):
        class ImmerRelay(Streamer):
            def probe(self, url):
                p = super().probe(url)
                return Probe(p.final_url, True, p.status, p.content_type)
        self.srv.portal.streamer = ImmerRelay(allow_private=True)
        try:
            r, _ = self.get("/vtuner/play/episode=111?mac=h&ven=aldi1", "aldi.wifiradiofrontier.com")
            loc = r.getheader("Location")
            self.assertRegex(loc, r"^http://aldi\.wifiradiofrontier\.com/portal/stream/[0-9a-f]{16}\.mp3$")
            path = loc.replace("http://aldi.wifiradiofrontier.com", "")
            r, body = self.get(path, "aldi.wifiradiofrontier.com")
            self.assertEqual((r.status, body), (200, AUDIO))
            self.assertEqual(r.getheader("Content-Type"), "audio/mpeg")
            r, body = self.get(path, "aldi.wifiradiofrontier.com", {"Range": "bytes=2-5"})
            self.assertEqual((r.status, body), (206, b"2345"))
        finally:
            self.srv.portal.streamer = Streamer(allow_private=True)

    def test_relay_unbekannte_id(self):
        r, _ = self.get("/portal/stream/0123456789abcdef.mp3", "aldi.wifiradiofrontier.com")
        self.assertEqual(r.status, 404)

    def test_mitschnitt(self):
        self.get("/vtuner/country=de?&startItems=1&mac=h", "aldi.wifiradiofrontier.com")
        index = Path(self.tmp.name, "mitschnitt", "index.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(json.loads(z)["status"] == 200 for z in index))

    def test_oberflaeche_und_fremder_host(self):
        r, body = self.get("/healthz", "raspi:8095")
        self.assertEqual((r.status, body), (200, b"ok"))
        r, body = self.get("/", "raspi:8095")
        self.assertEqual(r.status, 200)
        self.assertIn("Küchenradio".encode(), body)
        # Pfad des Radios, aber falscher Host: nie weiterreichen
        r, _ = self.get("/setupapp/aldi/asp/BrowseXML/loginXML.asp?token=0", "example.org")
        self.assertEqual(r.status, 404)
        self.assertEqual(FakeAirable.seen, [])

    def test_airable_nicht_erreichbar_gibt_502(self):
        srv = server.make_server(
            config.Config(port=0, data_dir=Path(self.tmp.name) / "x"),
            Forwarder(port=1, resolve=lambda h: "127.0.0.1", refuse_loopback=False, timeout=1))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("GET", "/x", headers={"Host": "aldi.wifiradiofrontier.com"})
            self.assertEqual(c.getresponse().status, 502)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
