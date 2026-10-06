"""Weboberflaeche (erfundene Beispiele, radio-browser und Stream-Pruefung sind Attrappen)."""

import tempfile
import unittest
import urllib.parse
from pathlib import Path

from radioportal import config, web
from radioportal import radio as radio_mod
from radioportal.__main__ import nachtragen
from radioportal.probe import Result
from radioportal.radiobrowser import RadioBrowser
from radioportal.server import Portal

STATIONS = {
    "u1": {"stationuuid": "u1", "name": "Rock <b>FM</b>", "url_resolved": "http://rock.example/live", "url": "",
           "codec": "MP3", "bitrate": 128, "countrycode": "DE", "tags": "rock,pop", "hls": 0},
    "u2": {"stationuuid": "u2", "name": "Jazz Radio", "url_resolved": "https://jazz.example/live", "url": "",
           "codec": "MP3", "bitrate": 96, "countrycode": "FR", "tags": "jazz", "hls": 0},
}


def fake_fetch(url):
    if "/json/stations/search" in url:
        return list(STATIONS.values())
    if "/json/stations/byuuid/" in url:
        return [STATIONS[url.rsplit("/", 1)[-1]]] if url.rsplit("/", 1)[-1] in STATIONS else []
    return []


def fake_probe(url):
    if "kaputt" in url:
        return Result(ok=False, hinweis="Nicht erreichbar oder kein Audiostrom (Antwort 404).")
    https = url.startswith("https://")
    return Result(ok=True, url=url, start=url, codec="MP3", bitrate="128", https_nur=https,
                  spielbar=not https, hinweis="geprueft", text="Guter Rock." if "rock" in url else "")


class WebBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.portal = Portal(config.Config(data_dir=Path(self.tmp.name)))
        self.portal.rb = RadioBrowser(servers=["rb.example"], fetch=fake_fetch)
        self.portal.prober = fake_probe

    def get(self, path, query=""):
        return web.handle(self.portal, "GET", path, query, {"Host": "raspi:8095"})

    def post(self, path, **form):
        return web.handle(self.portal, "POST", path, "", {"Host": "raspi:8095", "Origin": "http://raspi:8095"},
                          urllib.parse.urlencode(form).encode())

    def text(self, res):
        return res.body.decode("utf-8")

    def location(self, res):
        self.assertEqual(res.status, 303)
        return dict(res.headers)["Location"]

    def meldung(self, res):
        return urllib.parse.unquote_plus(self.location(res))


class PageTests(WebBase):
    def test_alle_seiten_rendern_und_maskieren(self):
        lib = self.portal.library
        sid = lib.add_sender(name='Böse <script>alert(1)</script>', url="http://x/1", codec="MP3")
        lib.zeigen(sid)
        weg = lib.add_sender(name="Weg <i>x</i>", url="http://x/2")
        self.portal.anfragen.add("192.168.1.26", "Sender nachschlagen", 200, "Portal", sid)
        for p, q in (("/", ""), ("/", "q=rock"), ("/sender/bearbeiten", f"id={weg}"), ("/sender/adresse", ""),
                     ("/podcasts", "")):
            res = self.get(p, q)
            self.assertEqual(res.status, 200, p)
            body = self.text(res)
            self.assertNotIn("<script>alert", body, f"{p}: Name nicht maskiert")
            self.assertNotIn("<i>x</i>", body)
        body = self.text(self.get("/"))
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("Ausgeblendet", body)
        self.assertIn("Letzte Anfragen", body)
        for alt in ("/suche", "/neu", "/tasten", "/sender", "/gibts-nicht"):
            self.assertEqual(self.get(alt).status, 404, alt)
        self.assertEqual(self.get("/healthz").body, b"ok")
        self.assertEqual(self.get("/sender/bearbeiten", "id=42").status, 303)

    def test_suche_zeigt_was_schon_da_ist(self):
        body = self.text(self.get("/", "q=rock"))
        self.assertIn("Rock &lt;b&gt;FM&lt;/b&gt;", body)
        self.assertLess(body.index("Rock &lt;b&gt;FM"), body.index("Jazz Radio"), "http zuerst")
        self.assertEqual(body.count("Hinzufügen</button>"), 2)
        self.post("/sender/hinzufuegen", uuid="u1")
        body = self.text(self.get("/", "q=rock"))
        self.assertIn("steht in der Liste", body)
        self.post("/sender/ausblenden", id="1000001")
        self.assertIn("Wieder einblenden", self.text(self.get("/", "q=rock")))


class StatusTests(WebBase):
    def test_ohne_radio(self):
        body = self.text(self.get("/"))
        self.assertIn("Noch kein Radio", body)
        self.assertIn("DNS 1 und DNS 2", body)

    def test_radio_gemeldet_und_persistent(self):
        self.portal.radios.touch("192.168.1.26")
        self.portal.radios.touch("192.168.1.26")
        body = self.text(self.get("/"))
        self.assertIn("192.168.1.26", body)
        self.assertIn("gerade eben", body)
        self.assertIn("2 Anfragen", body)
        from radioportal.store import RadioStatus
        neu = RadioStatus(Path(self.tmp.name) / "radio.json")  # beim ersten Mal sofort gespeichert
        self.assertEqual(neu.snapshot()["192.168.1.26"]["anfragen"], 1)

    def test_lange_stille_wird_erklaert(self):
        self.portal.radios.touch("192.168.1.26")
        self.portal.radios._data["192.168.1.26"]["zuletzt"] = "2026-01-01T10:00:00+01:00"
        body = self.text(self.get("/"))
        self.assertIn("vor ", body)
        self.assertIn("DNS 1 und DNS 2", body)


class SucheTests(unittest.TestCase):
    def test_mehrwort_suche_filtert_nach_allen_woertern(self):
        calls = []

        def fetch(url):
            calls.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
            name = calls[-1]["name"][0]
            if name == "alex berlin":
                return []
            return [{"stationuuid": "a", "name": "ALEX Offener Kanal Berlin", "url_resolved": "http://a/1",
                     "codec": "MP3", "tags": ""},
                    {"stationuuid": "b", "name": "Radio Alex Hamburg", "url_resolved": "http://b/1",
                     "codec": "MP3", "tags": ""}]
        res = RadioBrowser(servers=["x"], fetch=fetch).search("alex berlin")
        self.assertEqual([r["name"] for r in res], ["ALEX Offener Kanal Berlin"])
        self.assertEqual([c["name"][0] for c in calls], ["alex berlin", "berlin"])


class ActionTests(WebBase):
    def test_post_braucht_passenden_origin(self):
        for headers in ({"Host": "raspi:8095"}, {"Host": "raspi:8095", "Origin": "http://boese.example"}):
            res = web.handle(self.portal, "POST", "/sender/ausblenden", "", headers, b"id=1")
            self.assertEqual(res.status, 403)

    def test_suchtreffer_hinzufuegen(self):
        res = self.post("/sender/hinzufuegen", uuid="u1")
        self.assertIn("Platz 1", self.meldung(res))
        lib = self.portal.library
        (sid,) = lib.liste()
        s = lib.sender(sid)
        self.assertEqual((s["name"], s["url"], s["quelle"], s["rb_uuid"]), ("Rock <b>FM</b>", "http://rock.example/live",
                                                                         "radio-browser", "u1"))
        self.assertEqual(sid, "1000001")
        self.assertIn("schon in der Liste", self.meldung(self.post("/sender/hinzufuegen", uuid="u1")))
        self.assertEqual(len(lib.all_senders()), 1)
        self.assertIn("Rock", self.text(self.get("/")))

    def test_unerreichbarer_sender_wird_nicht_uebernommen(self):
        STATIONS["u3"] = dict(STATIONS["u1"], stationuuid="u3", url_resolved="http://kaputt.example/x")
        self.addCleanup(STATIONS.pop, "u3")
        self.assertIn("nicht übernommen", self.meldung(self.post("/sender/hinzufuegen", uuid="u3")))
        self.assertEqual(self.portal.library.all_senders(), {})

    def test_reihenfolge_ausblenden_einblenden_loeschen(self):
        for u in ("u1", "u2"):
            self.post("/sender/hinzufuegen", uuid=u)
        lib = self.portal.library
        a, b = lib.liste()
        self.post("/sender/hoch", id=b)
        self.assertEqual(lib.liste(), [b, a])
        self.post("/sender/runter", id=b)
        self.assertEqual(lib.liste(), [a, b])
        self.post("/sender/ausblenden", id=a)
        self.assertEqual(lib.liste(), [b])
        lib.note_radio(b)
        self.post("/sender/ausblenden", id=b)
        self.assertIn("Nicht gelöscht", self.meldung(self.post("/sender/loeschen", id=b)))
        self.assertIsNotNone(lib.sender(b))
        self.assertIn("gelöscht", self.meldung(self.post("/sender/loeschen", id=a)))
        self.assertIsNone(lib.sender(a))
        self.post("/sender/einblenden", id=b)
        self.assertEqual(lib.liste(), [b])

    def test_sender_per_adresse(self):
        body = self.text(self.post("/sender/adresse", name="Mein Sender", url="http://eigen.example/stream"))
        self.assertIn("Zur Liste hinzufügen", body)
        res = self.post("/sender/adresse/speichern", name="Mein Sender", start="http://eigen.example/stream",
                        orig="http://eigen.example/stream", codec="MP3", bitrate="128", hinweis="ok")
        self.assertEqual(self.location(res).split("?")[0], "/")
        (sid,) = self.portal.library.liste()
        self.assertEqual(self.portal.library.sender(sid)["quelle"], "manuell")
        self.assertNotIn("Zur Liste hinzufügen", self.text(self.post("/sender/adresse", name="x",
                                                                      url="http://kaputt.example/")))

    def test_bearbeiten_name_und_adresse_id_bleibt(self):
        self.post("/sender/hinzufuegen", uuid="u1")
        lib = self.portal.library
        (sid,) = lib.liste()
        self.post("/sender/bearbeiten", id=sid, name="Rock FM", url="http://rock.example/live")
        self.assertEqual(lib.sender(sid)["name"], "Rock FM")
        # kaputte neue Adresse: nichts gespeichert, Fehler auf der Seite
        res = self.post("/sender/bearbeiten", id=sid, name="Anders", url="http://kaputt.example/x")
        self.assertEqual(res.status, 200)
        self.assertIn("Nicht gespeichert", self.text(res))
        self.assertEqual((lib.sender(sid)["name"], lib.sender(sid)["url"]), ("Rock FM", "http://rock.example/live"))
        # neue Adresse: geprueft, gleiche Nummer
        self.post("/sender/bearbeiten", id=sid, name="Rock FM", url="http://neu.example/rock")
        self.assertEqual(lib.sender(sid)["url"], "http://neu.example/rock")
        self.assertEqual(lib.liste(), [sid])
        res = self.post("/sender/bearbeiten", id=sid, name="", url="http://neu.example/rock")
        self.assertIn("nicht leer", self.text(res))

    def test_beschreibung_aus_stream_und_tags(self):
        lib = self.portal.library
        self.post("/sender/hinzufuegen", uuid="u1")
        self.post("/sender/hinzufuegen", uuid="u2")
        rock, jazz = lib.liste()
        self.assertEqual((lib.sender(rock)["stream_text"], lib.sender(rock)["tags"]), ("Guter Rock.", "rock, pop"))
        self.assertEqual(radio_mod.beschreibung(lib.sender(jazz)), "jazz")
        self.post("/sender/bearbeiten", id=jazz, name="Jazz Radio", url="https://jazz.example/live",
                  beschreibung="  Nur Jazz  ")
        self.assertEqual(radio_mod.beschreibung(lib.sender(jazz)), "Nur Jazz")
        self.assertIn('value="Nur Jazz"', self.text(self.get("/sender/bearbeiten", f"id={jazz}")))

    def test_nachtragen_fuer_alte_sender(self):
        lib = self.portal.library
        alt = lib.add_sender(name="Alt", url="http://rock.example/live", rb_uuid="u1")
        weg = lib.add_sender(name="Weg", url="http://kaputt.example/")
        for sid in (alt, weg):  # wie aus Version 0.3.0: Felder fehlen ganz
            with lib._lock:
                for k in ("stream_text", "stream_genre", "tags"):
                    lib._sender[sid].pop(k)
        self.assertEqual(nachtragen(self.portal), 2)
        self.assertEqual((lib.sender(alt)["stream_text"], lib.sender(alt)["tags"]), ("Guter Rock.", "rock, pop"))
        self.assertEqual(lib.sender(weg)["stream_text"], "")
        self.assertEqual(nachtragen(self.portal), 0)

    def test_pruefen(self):
        self.post("/sender/hinzufuegen", uuid="u1")
        res = self.post("/sender/pruefen", id="1000001")
        self.assertTrue(self.location(res).startswith("/sender/bearbeiten?id=1000001&m="))

    def test_sicherung(self):
        self.post("/sender/hinzufuegen", uuid="u1")
        import json
        res = self.get("/sicherung.json")
        self.assertIn("attachment", dict(res.headers)["Content-Disposition"])
        data = json.loads(res.body.decode("utf-8"))
        self.assertEqual(data["senderliste"], ["1000001"])
        self.assertEqual(data["sender"]["1000001"]["rb_uuid"], "u1")
        self.assertNotIn("airable", data)


if __name__ == "__main__":
    unittest.main()
