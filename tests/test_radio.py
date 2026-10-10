"""Radio-Routen, Bibliothek, XML (erfundene Beispiele)."""

import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from radioportal import config, probe, radio, xmlitems
from radioportal.library import Library
from radioportal.podcasts import parse_feed
from radioportal.server import Portal

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Pod</title>
<item><title>F1</title><guid>g1</guid><enclosure url="http://cdn.example/1.mp3" type="audio/mpeg"/></item>
</channel></rss>"""


class KeinAirable:
    """Die Senderliste darf Airable nie fragen."""

    def __init__(self):
        self.calls = []

    def forward(self, *a, **kw):
        self.calls.append(a)
        raise AssertionError("Airable gefragt")


def items(reply):
    root = ET.fromstring(reply.body)
    return int(root.findtext("ItemCount")), [
        {c.tag: (c.text or "") for c in it} for it in root.iter("Item")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mk()

    def mk(self, **kw):
        self.portal = Portal(config.Config(data_dir=Path(self.tmp.name), **kw), KeinAirable())

    def call(self, path, query="", host="aldi.wifiradiofrontier.com"):
        return radio.handle(self.portal, host, "GET", path, query, [])

    def search(self, sid):
        return self.call("/setupapp/aldi/asp/BrowseXML/Search.asp", f"sSearchtype=3&Search={sid}&mac=h")


class LibraryTests(Base):
    def test_ids_werden_nie_neu_vergeben(self):
        lib = self.portal.library
        a = lib.add_sender(name="A", url="http://a/1")
        b = lib.add_sender(name="B", url="http://b/1")
        self.assertEqual((a, b), ("1000001", "1000002"))
        self.assertTrue(lib.remove_sender(b))
        c = Library(Path(self.tmp.name)).add_sender(name="C", url="http://c/1")  # neu geladen
        self.assertEqual(c, "1000003")

    def test_gleicher_sender_nur_einmal(self):
        lib = self.portal.library
        a = lib.add_sender(name="A", url="http://a/1", rb_uuid="u1")
        self.assertEqual(lib.add_sender(name="A2", url="http://a/other", rb_uuid="u1"), a)
        self.assertEqual(lib.add_sender(name="A3", url="http://a/1"), a)

    def test_liste_reihenfolge_ausblenden(self):
        lib = self.portal.library
        a, b, c = (lib.add_sender(name=n, url=f"http://x/{n}") for n in "abc")
        for s in (a, b, c):
            self.assertTrue(lib.zeigen(s))
        self.assertFalse(lib.zeigen(a))
        self.assertTrue(lib.verschieben(c, -1))
        self.assertEqual(lib.liste(), [a, c, b])
        self.assertFalse(lib.verschieben(a, -1))
        self.assertTrue(lib.ausblenden(c))
        self.assertEqual((lib.liste(), lib.ausgeblendet()), ([a, b], [c]))
        self.assertEqual(Library(Path(self.tmp.name)).liste(), [a, b])

    def test_platz_setzen(self):
        lib = self.portal.library
        a, b, c, d = (lib.add_sender(name=n, url=f"http://x/{n}") for n in "abcd")
        for s in (a, b, c, d):
            lib.zeigen(s)
        self.assertTrue(lib.platz_setzen(c, 1))
        self.assertEqual(lib.liste(), [c, a, b, d])
        self.assertTrue(lib.platz_setzen(c, 3))
        self.assertEqual(lib.liste(), [a, b, c, d])
        self.assertTrue(lib.platz_setzen(a, 99))
        self.assertEqual(lib.liste(), [b, c, d, a])
        self.assertFalse(lib.platz_setzen(b, 1), "steht schon da")
        lib.ausblenden(d)
        self.assertFalse(lib.platz_setzen(d, 1))
        self.assertEqual(Library(Path(self.tmp.name)).liste(), [b, c, a])

    def test_loeschen_nur_wenn_ausgeblendet_und_nie_am_radio(self):
        lib = self.portal.library
        a, b = (lib.add_sender(name=n, url=f"http://x/{n}") for n in "ab")
        lib.zeigen(a)
        self.assertFalse(lib.remove_sender(a), "steht in der Liste")
        lib.ausblenden(a)
        lib.note_radio(b)  # das Radio hat b gespielt: kann auf der FAV-Taste liegen
        self.assertFalse(lib.loeschbar(b))
        self.assertFalse(lib.remove_sender(b))
        self.assertTrue(lib.loeschbar(a))
        self.assertTrue(lib.remove_sender(a))

    def test_sender_aus_version_02_gelten_als_gespielt(self):
        lib = self.portal.library
        a = lib.add_sender(name="Alt", url="http://x/alt")
        lib._sender[a].pop("radio_zuletzt")  # so sehen Sender aus der Zeit vor 0.3 aus
        self.assertFalse(lib.loeschbar(a))

    def test_note_radio_schreibt_nicht_jedes_mal(self):
        lib = self.portal.library
        a = lib.add_sender(name="A", url="http://x/a")
        with mock.patch.object(lib, "_save_sender", wraps=lib._save_sender) as save:
            lib.note_radio(a)
            lib.note_radio(a)
        self.assertEqual(save.call_count, 1)
        self.assertTrue(Library(Path(self.tmp.name)).sender(a)["radio_zuletzt"])


class XmlTests(unittest.TestCase):
    def test_umlaute(self):
        self.assertEqual(xmlitems.umlaute("Äpfel Öl Übung ß é"), "Aepfel Oel Uebung ss e")
        self.assertEqual(xmlitems.umlaute("Äpfel", "utf8"), "Äpfel")
        # utf8-Modus: Latin-1 bleibt, Typografie wird ersetzt, Unbekanntes faellt weg oder auf ASCII
        self.assertEqual(xmlitems.umlaute("Süd – „Zitat“ … Straße é", "utf8"), 'Süd - "Zitat" ... Straße é')
        self.assertEqual(xmlitems.umlaute("Łódź 😀 Привет", "utf8"), "ódz  ")

    def test_escaping_und_zaehlung(self):
        x = xmlitems.Xml()
        data = x.listing([x.station("5", "A & B <c>", "http://h/p?a=1&b=2", fmt="Rock", bandwidth=128)],
                         previous="http://h/vtuner?")
        root = ET.fromstring(data)
        self.assertEqual(root.findtext("ItemCount"), "1")  # Previous zaehlt nicht
        self.assertEqual(root.findtext(".//StationName"), "A & B <c>")
        self.assertEqual(root.findtext(".//StationUrl"), "http://h/p?a=1&b=2")
        self.assertIsNone(root.find(".//Bookmark"))

    def test_seiten(self):
        self.assertEqual(xmlitems.page(list(range(250)), 101, 200), list(range(100, 200)))
        self.assertEqual(len(xmlitems.page(list(range(250)), 1, 500)), 100)


class SenderlisteTests(Base):
    MENUE = ("/setupapp/aldi/asp/BrowseXML/loginXML.asp", "gofile=&mac=h&dlang=ger")

    def test_leer(self):
        n, it = items(self.call(*self.MENUE))
        self.assertEqual([i["ItemType"] for i in it], ["Display"])
        self.mk(portal_url="http://raspi:8095")
        n, it = items(self.call(*self.MENUE))
        self.assertIn("raspi:8095", it[1]["Display"])

    def test_sender_direkt_oben_dann_podcasts_ohne_airable(self):
        lib = self.portal.library
        a = lib.add_sender(name="Alpha", url="http://a.example/live", genre="Rock", land="DE", bitrate="128")
        b = lib.add_sender(name="Beta", url="http://b.example/live")
        versteckt = lib.add_sender(name="Versteckt", url="http://c.example/live")
        lib.zeigen(b)
        lib.zeigen(a)
        r = self.call(*self.MENUE)
        self.assertEqual(r.ctype, "text/html; charset=UTF-8")
        n, it = items(r)
        self.assertEqual([i["StationName"] for i in it], ["Beta", "Alpha"])
        self.assertEqual(it[1]["StationUrl"], f"http://aldi.wifiradiofrontier.com/portal/play/{a}")
        self.assertEqual((it[1]["StationFormat"], it[1]["StationLocation"]), ("Rock", "DE"))
        self.assertEqual(it[1]["StationDesc"], "Rock")
        self.assertEqual(it[0]["StationDesc"], "")
        lib.update_sender(a, tags="rock, Rock, classic rock")
        lib.update_sender(b, stream_genre="Jazz")
        n, it = items(self.call(*self.MENUE))
        self.assertEqual((it[1]["StationDesc"], it[0]["StationDesc"]), ("rock, classic rock", "Jazz"))
        lib.update_sender(a, stream_text="Alles von Relevanz.")
        n, it = items(self.call(*self.MENUE))
        self.assertEqual(it[1]["StationDesc"], "Alles von Relevanz.")
        lib.update_sender(a, beschreibung="Mein Lieblingssender")
        n, it = items(self.call(*self.MENUE))
        self.assertEqual(it[1]["StationDesc"], "Mein Lieblingssender")
        self.portal.podcasts.add("http://feed.example/rss", parse_feed(FEED))
        n, it = items(self.call(*self.MENUE))
        self.assertEqual([i.get("StationName") or i.get("Title") for i in it], ["Beta", "Alpha", "Podcasts"])
        self.assertTrue(it[2]["UrlDir"].endswith("/portal/podcasts?"))
        self.assertEqual(n, 3)
        self.assertNotIn(versteckt, [i.get("StationId") for i in it])
        self.assertEqual(self.portal.forwarder.calls, [])

    def test_vtuner_und_altes_menue_zeigen_die_senderliste(self):
        lib = self.portal.library
        lib.zeigen(lib.add_sender(name="Alpha", url="http://a.example/live"))
        for path, q in (("/vtuner", "&mac=h"), ("/portal/favoriten", "&startItems=1&endItems=100")):
            self.assertEqual(items(self.call(path, q))[1][0]["StationName"], "Alpha", path)

    def test_blaettern(self):
        lib = self.portal.library
        for i in range(130):
            lib.zeigen(lib.add_sender(name=f"Sender {i}", url=f"http://x/{i}"))
        self.portal.podcasts.add("http://feed.example/rss", parse_feed(FEED))
        n, it = items(self.call("/vtuner", "startItems=1&endItems=100"))
        self.assertEqual((n, len(it)), (131, 100))
        n, it = items(self.call("/vtuner", "startItems=101&endItems=200"))
        self.assertEqual(len(it), 31)
        self.assertEqual(it[0]["StationName"], "Sender 100")
        self.assertEqual(it[-1]["Title"], "Podcasts")

    def test_token(self):
        r = self.call("/setupapp/aldi/asp/BrowseXML/loginXML.asp", "token=0")
        self.assertEqual(len(r.body), 49)

    def test_alte_routen_gibt_es_nicht_mehr(self):
        for path in ("/portal/plaetze", "/portal/test", "/portal/umleitung/9000004"):
            self.assertEqual(self.call(path).status, 404, path)
        self.assertIsNone(self.call("/vtuner/collection/push/station=1000001"), "geht an Airable")


class LookupPlayTests(Base):
    def setUp(self):
        super().setUp()
        lib = self.portal.library
        self.eigen = lib.add_sender(name="Mein Sender", url="http://stream.example/live", codec="MP3",
                                    bitrate="128", land="DE")
        self.https = lib.add_sender(name="Nur https", url="https://secure.example/live")

    def test_eigene_id_wird_nachgeschlagen_und_gemerkt(self):
        n, it = items(self.search(self.eigen))
        st = it[1]
        self.assertEqual((st["StationId"], st["StationName"]), (self.eigen, "Mein Sender"))
        self.assertEqual(st["StationUrl"], f"http://aldi.wifiradiofrontier.com/portal/play/{self.eigen}")
        self.assertEqual(it[0]["UrlPrevious"], "http://aldi.wifiradiofrontier.com/vtuner?")
        self.assertTrue(self.portal.library.sender(self.eigen)["radio_zuletzt"])

    def test_ausgeblendet_spielt_weiter(self):
        """FAV-Taste: Ein Sender, der nicht mehr in der Liste steht, wird trotzdem gefunden."""
        self.assertNotIn(self.eigen, self.portal.library.liste())
        self.assertEqual(items(self.search(self.eigen))[1][1]["StationName"], "Mein Sender")

    def test_unbekannte_ids_gehen_an_airable(self):
        self.assertIsNone(self.search("1234567890123456"))
        self.assertIsNone(self.search("3000001"), "Plaetze gibt es nicht mehr")

    def test_play_klartext_ohne_zeilenende(self):
        with mock.patch("radioportal.probe.resolve_for_play",
                        return_value="http://cdn.example/final.mp3?sid=1") as m:
            r = self.call(f"/portal/play/{self.eigen}")
            self.call(f"/portal/play/{self.eigen}")  # zweiter Aufruf kommt aus dem Zwischenspeicher
        self.assertEqual((r.status, r.ctype, r.body), (200, "audio/x-mpegurl", b"http://cdn.example/final.mp3?sid=1"))
        self.assertEqual(m.call_count, 1)

    def test_https_geht_ueber_live(self):
        with mock.patch("radioportal.probe.resolve_for_play", side_effect=lambda u, timeout=0: u):
            r = self.call(f"/portal/play/{self.https}", host="aldi2.wifiradiofrontier.com")
        self.assertEqual(r.body, f"http://aldi2.wifiradiofrontier.com/portal/live/{self.https}.mp3".encode())

    def test_unbekannte_play_id(self):
        self.assertEqual(self.call("/portal/play/42").status, 404)

    def test_umlaute_im_namen(self):
        sid = self.portal.library.add_sender(name="Süd-Radio Köln", url="http://y/1")
        self.assertEqual(items(self.search(sid))[1][1]["StationName"], "Sued-Radio Koeln")


class AufloesenTests(unittest.TestCase):
    def test_zeitbudget_wird_eingehalten(self):
        """Das Radio wartet beim Abspielen: nie laenger als das Budget, sonst die gespeicherte Adresse."""
        def langsam(url, timeout, body_bytes=0):
            time.sleep(0.3)
            return 302, {"location": url + "x"}, b"", False
        with mock.patch("radioportal.probe._fetch", side_effect=langsam):
            t = time.monotonic()
            self.assertEqual(probe.resolve_for_play("http://a.example/s", timeout=4.0, budget=1.0),
                             "http://a.example/s")
            self.assertLess(time.monotonic() - t, 1.5)

    def test_weiterleitung_wird_aufgeloest(self):
        antworten = iter([(302, {"location": "http://cdn.example/e.mp3"}, b"", False),
                          (200, {"content-type": "audio/mpeg"}, b"", False)])
        with mock.patch("radioportal.probe._fetch", side_effect=lambda *a, **k: next(antworten)):
            self.assertEqual(probe.resolve_for_play("http://a.example/s"), "http://cdn.example/e.mp3")


class BeschreibeTests(unittest.TestCase):
    def test_ohne_kennung_des_radios(self):
        self.assertEqual(radio.beschreibe("/setupapp/aldi/asp/BrowseXML/loginXML.asp", "token=0"), ("Anmeldung", ""))
        self.assertEqual(radio.beschreibe("/setupapp/aldi/asp/BrowseXML/loginXML.asp", "gofile=&mac=geheim"),
                         ("Senderliste", ""))
        self.assertEqual(radio.beschreibe("/setupapp/aldi/asp/BrowseXML/Search.asp",
                                          "sSearchtype=3&Search=1000003&mac=geheim"), ("Sender nachschlagen", "1000003"))
        self.assertEqual(radio.beschreibe("/portal/live/1000002.mp3", ""), ("Sender über den Server", "1000002"))
        self.assertEqual(radio.beschreibe("/FindUpdate.aspx", "mac=AABBCC"), ("Update-Prüfung", ""))
        self.assertNotIn("geheim", str(radio.beschreibe("/vtuner/country=de", "mac=geheim")))


class StreamAngabenTests(unittest.TestCase):
    def test_platzhalter_und_name_fallen_weg(self):
        sa = probe.stream_angaben
        self.assertEqual(sa({"icy-name": "Nova", "icy-description": "Es ist kompliziert.", "icy-genre": "Talk"}),
                         ("Es ist kompliziert.", "Talk"))
        self.assertEqual(sa({"icy-name": "ROCK ANTENNE Bayern", "icy-description": "ROCK ANTENNE  Bayern"}), ("", ""))
        self.assertEqual(sa({"icy-name": "no name", "icy-description": "Unspecified description",
                             "icy-genre": "various"}), ("", ""))
        self.assertEqual(sa({"icy-description": "GrÃ¼Ãe"}), ("Grüße", ""))


if __name__ == "__main__":
    unittest.main()
