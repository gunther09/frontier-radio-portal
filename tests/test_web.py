"""Weboberflaeche (erfundene Beispiele, radio-browser und Stream-Pruefung sind Attrappen)."""

import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from radioportal import config, radio, web
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
                  spielbar=not https, hinweis="geprueft")


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


class PageTests(WebBase):
    def test_alle_seiten_rendern(self):
        sid = self.portal.library.add_sender(name='Böse <script>alert(1)</script>', url="http://x/1", codec="MP3")
        self.portal.library.fav_add(sid)
        self.portal.store.note_seen("1234567890123456", name="ALEX <i>Berlin</i>")
        for p in ("/", "/suche", "/neu", "/sender", "/tasten"):
            res = self.get(p)
            self.assertEqual(res.status, 200, p)
            body = self.text(res)
            self.assertNotIn("<script>alert", body, f"{p}: Name nicht maskiert")
            self.assertNotIn("<i>Berlin", body)
        self.assertIn("&lt;script&gt;", self.text(self.get("/")))
        self.assertEqual(self.get("/gibts-nicht").status, 404)
        self.assertEqual(self.get("/healthz").body, b"ok")

    def test_suche(self):
        body = self.text(self.get("/suche", "q=rock"))
        self.assertIn("Rock &lt;b&gt;FM&lt;/b&gt;", body)
        self.assertLess(body.index("Rock &lt;b&gt;FM"), body.index("Jazz Radio"), "http zuerst")
        self.assertIn("★ Favorit", body)


class StatusTests(WebBase):
    def test_ohne_radio(self):
        self.assertIn("Noch kein Radio", self.text(self.get("/")))

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

    def test_lange_stille_wird_gewarnt(self):
        self.portal.radios.touch("192.168.1.26")
        self.portal.radios._data["192.168.1.26"]["zuletzt"] = "2026-01-01T10:00:00+01:00"
        body = self.text(self.get("/"))
        self.assertIn("vor ", body)
        self.assertIn("FRITZ!Box", body)


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
            res = web.handle(self.portal, "POST", "/favorit/weg", "", headers, b"id=1")
            self.assertEqual(res.status, 403)

    def test_suchtreffer_als_favorit_uebernehmen(self):
        res = self.post("/favorit/uebernehmen", uuid="u1")
        self.assertTrue(self.location(res).startswith("/?m="))
        lib = self.portal.library
        (sid,) = lib.favorites()
        s = lib.sender(sid)
        self.assertEqual((s["name"], s["url"], s["quelle"], s["rb_uuid"]), ("Rock <b>FM</b>", "http://rock.example/live",
                                                                         "radio-browser", "u1"))
        self.assertEqual(sid, "1000001")
        # zweites Mal: kein Duplikat
        self.post("/favorit/uebernehmen", uuid="u1")
        self.assertEqual(len(lib.all_senders()), 1)
        self.assertIn("Rock", self.text(self.get("/")))

    def test_unerreichbarer_sender_wird_nicht_uebernommen(self):
        STATIONS["u3"] = dict(STATIONS["u1"], stationuuid="u3", url_resolved="http://kaputt.example/x")
        self.addCleanup(STATIONS.pop, "u3")
        res = self.post("/favorit/uebernehmen", uuid="u3")
        self.assertIn("/suche?m=", self.location(res))
        self.assertEqual(self.portal.library.all_senders(), {})

    def test_reihenfolge_aendern_und_entfernen(self):
        for u in ("u1", "u2"):
            self.post("/favorit/uebernehmen", uuid=u)
        a, b = self.portal.library.favorites()
        self.post("/favorit/hoch", id=b)
        self.assertEqual(self.portal.library.favorites(), [b, a])
        self.post("/favorit/runter", id=b)
        self.assertEqual(self.portal.library.favorites(), [a, b])
        self.post("/favorit/weg", id=a)
        self.assertEqual(self.portal.library.favorites(), [b])
        # a ist jetzt kein Favorit mehr und darf entfernt werden, b nicht
        self.post("/sender/entfernen", id=b)
        self.assertIsNotNone(self.portal.library.sender(b))
        self.post("/sender/entfernen", id=a)
        self.assertIsNone(self.portal.library.sender(a))

    def test_sender_per_adresse(self):
        res = self.post("/neu", name="Mein Sender", url="http://eigen.example/stream")
        body = self.text(res)
        self.assertIn("Speichern und als Favorit", body)
        res = self.post("/neu/speichern", name="Mein Sender", start="http://eigen.example/stream",
                        orig="http://eigen.example/stream", codec="MP3", bitrate="128", hinweis="ok")
        self.assertEqual(self.location(res).split("?")[0], "/")
        (sid,) = self.portal.library.favorites()
        self.assertEqual(self.portal.library.sender(sid)["quelle"], "manuell")
        # kaputte Adresse: kein Speichern-Knopf
        self.assertNotIn("Speichern und als Favorit", self.text(self.post("/neu", name="x", url="http://kaputt.example/")))

    def test_taste_mit_ersatz(self):
        aid = "1234567890123456"
        self.portal.store.note_seen(aid, name="Alter Sender")
        self.portal.store.note_play(aid, "http://airable.example/s")
        # Ersatz per Vorschlag
        self.assertIn("Rock", self.text(self.get("/tasten/vorschlag", f"id={aid}")))
        res = self.post("/tasten/uebernehmen", aid=aid, uuid="u1")
        self.assertEqual(self.location(res).split("?")[0], "/tasten")
        ersatz = self.portal.store.ersatz_of(aid)
        self.assertEqual(self.portal.library.sender(ersatz)["name"], "Rock <b>FM</b>")
        self.assertEqual(radio.lookup(self.portal, aid)["id"], ersatz)
        self.assertEqual(self.portal.library.favorites(), [], "Ersatz ist nicht automatisch Favorit")
        # Ersatz geschuetzt, dann entfernen
        self.post("/sender/entfernen", id=ersatz)
        self.assertIsNotNone(self.portal.library.sender(ersatz))
        self.post("/tasten/ersatz", aid=aid, sid="")
        self.assertEqual(self.portal.store.ersatz_of(aid), "")
        # vorhandenen Sender als Ersatz waehlen
        self.post("/tasten/ersatz", aid=aid, sid=ersatz)
        self.assertEqual(self.portal.store.ersatz_of(aid), ersatz)

    def test_ersatz_fuer_unbekannte_ids(self):
        self.post("/tasten/ersatz", aid="999", sid="1000001")
        self.assertEqual(self.portal.store.ersatz_of("999"), "")
        res = self.post("/tasten/uebernehmen", aid="999", uuid="u1")
        self.assertIn("Unbekannte", urllib.parse.unquote(self.location(res)))
        self.assertEqual(self.portal.library.all_senders(), {})


if __name__ == "__main__":
    unittest.main()
