"""Radio-Routen, Bibliothek, XML (erfundene Beispiele)."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from radioportal import config, radio, xmlitems
from radioportal.airable import Upstream, UpstreamError
from radioportal.library import Library
from radioportal.server import Portal

AIRABLE_MENUE = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ListOfItems><ItemCount>2</ItemCount>
<Item><ItemType>Dir</ItemType><Title>Meine Favoriten</Title><UrlDir>http://aldi.wifiradiofrontier.com/vtuner/collection=stations?</UrlDir><UrlDirBackUp>x</UrlDirBackUp></Item>
<Item><ItemType>Dir</ItemType><Title>Podcasts</Title><UrlDir>http://aldi.wifiradiofrontier.com/vtuner/podcasts?</UrlDir><UrlDirBackUp>x</UrlDirBackUp></Item>
</ListOfItems>"""


class FakeForwarder:
    def __init__(self, body=AIRABLE_MENUE, error=None):
        self.body, self.error, self.calls = body, error, []

    def forward(self, method, host, target, headers, body=b"", timeout=None):
        self.calls.append(target)
        if self.error:
            raise self.error
        return Upstream(200, "OK", [("Content-Type", "text/html")], self.body)


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
        self.portal = Portal(config.Config(data_dir=Path(self.tmp.name), **kw), FakeForwarder())

    def call(self, path, query="", host="aldi.wifiradiofrontier.com"):
        return radio.handle(self.portal, host, "GET", path, query, [])


class LibraryTests(Base):
    def test_ids_werden_nie_neu_vergeben(self):
        lib = self.portal.library
        a = lib.add_sender(name="A", url="http://a/1")
        b = lib.add_sender(name="B", url="http://b/1")
        self.assertEqual((a, b), ("1000001", "1000002"))
        self.assertTrue(lib.remove_sender(b, set()))
        c = Library(Path(self.tmp.name)).add_sender(name="C", url="http://c/1")  # neu geladen
        self.assertEqual(c, "1000003")

    def test_gleicher_sender_nur_einmal(self):
        lib = self.portal.library
        a = lib.add_sender(name="A", url="http://a/1", rb_uuid="u1")
        self.assertEqual(lib.add_sender(name="A2", url="http://a/other", rb_uuid="u1"), a)
        self.assertEqual(lib.add_sender(name="A3", url="http://a/1"), a)

    def test_favoriten_reihenfolge_und_schutz(self):
        lib = self.portal.library
        a, b, c = (lib.add_sender(name=n, url=f"http://x/{n}") for n in "abc")
        for s in (a, b, c):
            self.assertTrue(lib.fav_add(s))
        self.assertFalse(lib.fav_add(a))
        self.assertTrue(lib.fav_move(c, -1))
        self.assertEqual(lib.favorites(), [a, c, b])
        self.assertFalse(lib.fav_move(a, -1))
        self.assertFalse(lib.remove_sender(a, set()), "Favorit darf nicht geloescht werden")
        lib.fav_remove(a)
        self.assertFalse(lib.remove_sender(a, {a}), "Taste mit Ersatz darf nicht geloescht werden")
        self.assertTrue(lib.remove_sender(a, set()))
        self.assertEqual(Library(Path(self.tmp.name)).favorites(), [c, b])


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

    def test_seiten(self):
        self.assertEqual(xmlitems.page(list(range(250)), 101, 200), list(range(100, 200)))
        self.assertEqual(len(xmlitems.page(list(range(250)), 1, 500)), 100)


class MenueTests(Base):
    def test_hauptmenue(self):
        r = self.call("/setupapp/aldi/asp/BrowseXML/loginXML.asp", "gofile=&mac=h&dlang=ger")
        n, it = items(r)
        self.assertEqual([i["Title"] for i in it], ["Favoriten", "Airable-Favoriten", "Podcasts"])
        self.assertEqual(n, 3)
        self.assertTrue(it[0]["UrlDir"].endswith("/portal/favoriten?"))
        self.assertEqual(r.ctype, "text/html; charset=UTF-8")

    def test_hauptmenue_auch_ueber_vtuner(self):
        n, it = items(self.call("/vtuner", "&mac=h"))
        self.assertEqual(it[0]["Title"], "Favoriten")

    def test_hauptmenue_ohne_airable(self):
        self.portal.forwarder = FakeForwarder(error=UpstreamError("aus"))
        n, it = items(self.call("/vtuner", "&mac=h"))
        self.assertEqual([i["Title"] for i in it], ["Favoriten"])

    def test_testmenue_nur_wenn_eingeschaltet(self):
        _, it = items(self.call("/vtuner", ""))
        self.assertNotIn("Test", [i.get("Title") for i in it])
        self.assertEqual(self.call("/portal/test").status, 404)
        self.mk(testmenue=True)
        _, it = items(self.call("/vtuner", ""))
        self.assertEqual(it[-1]["Title"], "Test")
        n, it = items(self.call("/portal/test"))
        self.assertEqual([i["ItemType"] for i in it][:3], ["Previous", "Display", "Display"])
        self.assertEqual(sum(1 for i in it if i["ItemType"] == "Station"), 5)

    def test_token(self):
        r = self.call("/setupapp/aldi/asp/BrowseXML/loginXML.asp", "token=0")
        self.assertEqual(len(r.body), 49)
        self.assertEqual(self.portal.forwarder.calls, [])

    def test_favoriten_leer_und_gefuellt(self):
        n, it = items(self.call("/portal/favoriten", "&startItems=1&endItems=100&mac=h"))
        self.assertEqual([i["ItemType"] for i in it], ["Previous", "Display"])
        self.mk(portal_url="http://raspi:8095")
        n, it = items(self.call("/portal/favoriten", "&startItems=1&endItems=100&mac=h"))
        self.assertEqual([i["ItemType"] for i in it], ["Previous", "Display", "Display"])
        self.assertIn("raspi:8095", it[2]["Display"])
        lib = self.portal.library
        for i in range(130):
            lib.fav_add(lib.add_sender(name=f"Sender {i}", url=f"http://x/{i}"))
        n, it = items(self.call("/portal/favoriten", "startItems=1&endItems=100"))
        self.assertEqual(n, 131)  # 130 Sender + Ordner "Favorit-Plaetze"
        self.assertEqual(len(it), 101)  # 100 Sender + Previous
        n, it = items(self.call("/portal/favoriten", "startItems=101&endItems=200"))
        self.assertEqual(len(it), 32)
        self.assertEqual(it[1]["StationName"], "Sender 100")
        self.assertEqual(it[-1]["ItemType"], "Dir")


class LookupPlayTests(Base):
    def setUp(self):
        super().setUp()
        lib = self.portal.library
        self.eigen = lib.add_sender(name="Mein Sender", url="http://stream.example/live", codec="MP3",
                                    bitrate="128", land="DE")
        self.https = lib.add_sender(name="Nur https", url="https://secure.example/live")

    def search(self, sid):
        return self.call("/setupapp/aldi/asp/BrowseXML/Search.asp", f"sSearchtype=3&Search={sid}&mac=h")

    def test_eigene_id_wird_nachgeschlagen(self):
        n, it = items(self.search(self.eigen))
        st = it[1]
        self.assertEqual((st["StationId"], st["StationName"]), (self.eigen, "Mein Sender"))
        self.assertEqual(st["StationUrl"], f"http://aldi.wifiradiofrontier.com/portal/play/{self.eigen}")
        self.assertEqual(it[0]["UrlPrevious"], "http://aldi.wifiradiofrontier.com/vtuner?")

    def test_unbekannte_airable_id_wird_durchgereicht(self):
        self.assertIsNone(self.search("1234567890123456"))

    def test_airable_id_mit_ersatz(self):
        aid = "1234567890123456"
        self.portal.store.note_seen(aid, name="Alt")
        self.assertIsNone(self.search(aid))
        self.portal.store.set_ersatz(aid, self.eigen)
        st = items(self.search(aid))[1][1]
        self.assertEqual((st["StationId"], st["StationName"]), (aid, "Mein Sender"))
        with mock.patch("radioportal.probe.resolve_for_play", side_effect=lambda u, timeout=0: u):
            r = self.call(f"/portal/play/{aid}")
        self.assertEqual(r.body, b"http://stream.example/live")

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

    def test_testsender_und_umleitung(self):
        self.mk(testmenue=True)
        r = self.call("/portal/play/9000004")
        self.assertEqual(r.body, b"http://aldi.wifiradiofrontier.com/portal/umleitung/9000004")
        r = self.call("/portal/umleitung/9000004")
        self.assertEqual(r.status, 302)
        self.assertTrue(dict(r.headers)["Location"].startswith("http://"))
        self.assertEqual(self.call("/portal/play/9000001").status, 200)

    def test_umlaute_im_namen(self):
        sid = self.portal.library.add_sender(name="Süd-Radio Köln", url="http://y/1")
        self.assertEqual(items(self.search(sid))[1][1]["StationName"], "Sued-Radio Koeln")


if __name__ == "__main__":
    unittest.main()


class PlatzTests(Base):
    """FAV-Taste: Plaetze "Favorit k" spielen immer den k-ten Favoriten der Weboberflaeche."""

    def setUp(self):
        super().setUp()
        lib = self.portal.library
        self.a = lib.add_sender(name="Alpha", url="http://a.example/live")
        self.b = lib.add_sender(name="Beta", url="http://b.example/live")
        lib.fav_add(self.a)
        lib.fav_add(self.b)

    def search(self, sid):
        return self.call("/setupapp/aldi/asp/BrowseXML/Search.asp", f"sSearchtype=3&Search={sid}&mac=h")

    def test_menue_hat_feste_namen_und_zeigt_inhalt_in_beschreibung(self):
        n, it = items(self.call("/portal/plaetze"))
        st = [i for i in it if i["ItemType"] == "Station"]
        self.assertEqual([s["StationName"] for s in st][:3], ["Favorit 1", "Favorit 2", "Favorit 3"])
        self.assertEqual(st[0]["StationId"], "3000001")
        self.assertIn("Alpha", st[0]["StationDesc"])
        self.assertIn("leer", st[2]["StationDesc"])

    def test_platz_spielt_kten_favoriten_und_folgt_der_reihenfolge(self):
        self.assertEqual(items(self.search("3000001"))[1][1]["StationName"], "Alpha")
        self.portal.library.fav_move(self.b, -1)
        self.assertEqual(items(self.search("3000001"))[1][1]["StationName"], "Beta")
        self.assertEqual(items(self.search("3000002"))[1][1]["StationName"], "Alpha")
        self.assertIsNone(self.search("3000003"))  # leer: an Airable durchgereicht, die kennt es nicht

    def test_alte_id_mit_platz_als_ersatz(self):
        aid = "1234567890123456"
        self.portal.store.note_seen(aid, name="Alt")
        self.portal.store.set_ersatz(aid, "3000002")
        self.assertEqual(items(self.search(aid))[1][1]["StationName"], "Beta")
        self.portal.library.fav_remove(self.b)
        self.assertIsNone(self.search(aid))

    def test_bookmark_und_push_pop(self):
        c = self.portal.library.add_sender(name="Gamma", url="http://c.example/live")
        st = items(self.search(c))[1][1]
        self.assertTrue(st["Bookmark"].endswith(f"/vtuner/collection/push/station={c}?"))
        r = self.call(f"/vtuner/collection/push/station={c}")
        self.assertEqual(r.status, 200)
        self.assertIn(c, self.portal.library.favorites())
        st = items(self.search(c))[1][1]
        self.assertIn("/collection/pop/", st["Bookmark"])
        self.call(f"/vtuner/collection/pop/station={c}")
        self.assertNotIn(c, self.portal.library.favorites())


class AusblendenTests(Base):
    def test_airable_menues_ausblenden(self):
        self.mk(airable_ausblenden=("help", "country="))
        menue = (b'<?xml version="1.0"?><ListOfItems><ItemCount>3</ItemCount>'
                 b'<Item><ItemType>Dir</ItemType><Title>Sender</Title><UrlDir>http://a/vtuner/stations?</UrlDir></Item>'
                 b'<Item><ItemType>Dir</ItemType><Title>Oertlich</Title><UrlDir>http://a/vtuner/country=de?</UrlDir></Item>'
                 b'<Item><ItemType>Dir</ItemType><Title>Hilfe</Title><UrlDir>http://a/vtuner/help?</UrlDir></Item>'
                 b'</ListOfItems>')
        self.portal.forwarder.body = menue
        _, it = items(self.call("/vtuner", ""))
        self.assertEqual([i.get("Title") for i in it], ["Favoriten", "Sender"])
