"""Eigene Podcasts: Feed lesen, Ablage, Radio-Menues, Oberflaeche (erfundene Beispiele)."""

import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from radioportal import config, podcasts, radio, web
from radioportal.airable import Upstream, UpstreamError
from radioportal.podcasts import PodcastError, PodcastStore, episode_id, parse_feed
from radioportal.server import Portal
from radioportal.stream import Probe

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>Mein &amp; Dein Podcast</title><description>&lt;p&gt;Ueber <b>alles</b>&lt;/p&gt;</description>
<language>de</language>
<item><title>Folge 1</title><guid>g1</guid><pubDate>Mon, 01 Sep 2025 10:00:00 +0000</pubDate>
<enclosure url="http://cdn.example/1.mp3" type="audio/mpeg" length="1"/><itunes:duration>12:30</itunes:duration>
<description>Erste &lt;i&gt;Folge&lt;/i&gt;</description></item>
<item><title>Folge 2</title><guid>g2</guid><pubDate>Mon, 08 Sep 2025 10:00:00 +0000</pubDate>
<enclosure url="https://cdn.example/2.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Folge 3</title><guid>g3</guid><pubDate>Mon, 15 Sep 2025 10:00:00 +0000</pubDate>
<enclosure url="http://cdn.example/3.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Folge 4</title><guid>g4</guid><pubDate>Mon, 22 Sep 2025 10:00:00 +0000</pubDate>
<enclosure url="http://cdn.example/4.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Folge 5</title><guid>g5</guid><pubDate>Mon, 29 Sep 2025 10:00:00 +0000</pubDate>
<enclosure url="http://cdn.example/5.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Nur Text, kein Audio</title><guid>gx</guid></item>
</channel></rss>"""


def feed_with(*extra):
    """FEED plus weitere Folgen (Liste von (guid, Datum))."""
    items = "".join(
        f'<item><title>Neu {g}</title><guid>{g}</guid><pubDate>{d}</pubDate>'
        f'<enclosure url="http://cdn.example/{g}.mp3" type="audio/mpeg"/></item>' for g, d in extra)
    return FEED.replace("</channel>", items + "</channel>")


class FeedTests(unittest.TestCase):
    def test_feed_lesen(self):
        f = parse_feed(FEED.encode())
        self.assertEqual(f["title"], "Mein & Dein Podcast")
        self.assertEqual(f["description"], "Ueber alles")
        self.assertEqual(len(f["items"]), 5, "Folge ohne Audio-Datei wird uebersprungen")
        self.assertEqual(f["items"][0]["title"], "Folge 5", "neueste zuerst")
        first = f["items"][-1]
        self.assertEqual((first["title"], first["url"], first["duration"]), ("Folge 1", "http://cdn.example/1.mp3", "12:30"))
        self.assertEqual(first["date"][:10], "2025-09-01")
        self.assertEqual(first["desc"], "Erste Folge")

    def test_grosses_archiv_bleibt_klein(self):
        items = "".join(
            f'<item><title>F{i}</title><guid>g{i}</guid><pubDate>Mon, 01 Sep 2025 10:{i // 60:02d}:{i % 60:02d} +0000</pubDate>'
            f'<enclosure url="http://cdn.example/{i}.mp3" type="audio/mpeg"/></item>' for i in range(3000))
        data = (f'<?xml version="1.0"?><rss version="2.0"><channel><title>Archiv</title>{items}</channel></rss>').encode()
        chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)]
        f = podcasts.parse_feed_stream(chunks)
        self.assertEqual(len(f["items"]), 100)
        self.assertEqual(f["title"], "Archiv")
        self.assertEqual(f["items"][0]["title"], "F2999", "neueste zuerst")

    def test_abgeschnittener_feed_liefert_was_da_ist(self):
        f = parse_feed(FEED.encode()[:-60])  # Ende fehlt
        self.assertGreaterEqual(len(f["items"]), 4)

    def test_webseite_statt_feed(self):
        with self.assertRaises(PodcastError) as cm:
            parse_feed(b"<!DOCTYPE html><html><body>Hallo</body></html>")
        self.assertIn("Webseite", str(cm.exception))

    def test_fehlerfaelle(self):
        for bad in (b"<html/>", b"kein xml", b'<!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>',
                    b"<rss><channel><title>x</title></channel></rss>"):
            with self.assertRaises(PodcastError):
                parse_feed(bad)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "podcasts.json"
        self.store = PodcastStore(self.path)

    def test_abonnieren_nur_neueste_ungehoert(self):
        pid, neu = self.store.add("http://feed.example/rss", parse_feed(FEED.encode()))
        self.assertEqual((pid, neu), ("5000001", True))
        p = self.store.get(pid)
        self.assertEqual([e["titel"] for e in p["episoden"]], ["Folge 5", "Folge 4", "Folge 3", "Folge 2", "Folge 1"])
        self.assertEqual(self.store.unheard(p), 3)
        self.assertEqual([e["gehoert"] for e in p["episoden"]], [False, False, False, True, True])
        # zweites Abo desselben Feeds: nichts doppelt
        self.assertEqual(self.store.add("http://feed.example/rss", parse_feed(FEED.encode())), (pid, False))

    def test_aktualisieren_und_gehoert(self):
        pid, _ = self.store.add("http://feed.example/rss", parse_feed(FEED.encode()))
        n = self.store.refresh(pid, parse_feed(feed_with(("g6", "Mon, 06 Oct 2025 10:00:00 +0000")).encode()))
        self.assertEqual(n, 1)
        p = self.store.get(pid)
        self.assertEqual(p["episoden"][0]["titel"], "Neu g6")
        self.assertFalse(p["episoden"][0]["gehoert"])
        self.assertEqual(self.store.refresh(pid, parse_feed(feed_with(("g6", "Mon, 06 Oct 2025 10:00:00 +0000")).encode())), 0)
        newest = p["episoden"][0]
        eid = episode_id(pid, newest["n"])
        found = self.store.find_episode(eid)
        self.assertEqual(found[1]["titel"], "Neu g6")
        self.store.mark_heard(eid)
        self.assertTrue(self.store.find_episode(eid)[1]["gehoert"])
        self.assertIsNone(self.store.find_episode("5000001999999"))
        self.assertIsNone(self.store.find_episode("12345"))

    def test_ids_bleiben_und_werden_nie_neu_vergeben(self):
        pid, _ = self.store.add("http://feed.example/a", parse_feed(FEED.encode()))
        self.assertTrue(self.store.remove(pid))
        pid2, _ = PodcastStore(self.path).add("http://feed.example/b", parse_feed(FEED.encode()))
        self.assertEqual((pid, pid2), ("5000001", "5000002"))

    def test_alte_folgen_fallen_nach_100_weg(self):
        pid, _ = self.store.add("http://feed.example/a", parse_feed(FEED.encode()))
        viele = [(f"n{i}", f"Mon, 06 Oct 2025 10:{i // 60:02d}:{i % 60:02d} +0000") for i in range(120)]
        self.store.refresh(pid, parse_feed(feed_with(*viele).encode()))
        p = self.store.get(pid)
        self.assertEqual(len(p["episoden"]), 100)
        # der Feed liefert nur die neuesten 100 (alle neu): Nummern 6..105, naechste ist 106
        self.assertEqual(p["naechste_n"], 106)
        self.assertTrue(all(e["n"] >= 6 for e in p["episoden"]), "die fuenf alten Folgen sind weggefallen")


class RadioPodcastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.portal = Portal(config.Config(data_dir=Path(self.tmp.name)), mock.Mock())
        self.pid, _ = self.portal.podcasts.add("http://feed.example/rss", parse_feed(FEED.encode()))

    def call(self, path, query=""):
        return radio.handle(self.portal, "aldi.wifiradiofrontier.com", "GET", path, query, [])

    def items(self, reply):
        root = ET.fromstring(reply.body)
        return [{c.tag: (c.text or "") for c in it} for it in root.iter("Item")]

    def test_hauptmenue_zeigt_podcasts_erst_wenn_vorhanden(self):
        self.portal.forwarder.forward.side_effect = UpstreamError("aus")
        titles = [i.get("Title") for i in self.items(self.call("/vtuner", ""))]
        self.assertEqual(titles, ["Favoriten", "Eigene Podcasts"])
        self.portal.podcasts.remove(self.pid)
        titles = [i.get("Title") for i in self.items(self.call("/vtuner", ""))]
        self.assertEqual(titles, ["Favoriten"])

    def test_podcastliste_und_folgen(self):
        it = self.items(self.call("/portal/podcasts"))
        self.assertEqual(it[1]["ItemType"], "ShowOnDemand")
        self.assertEqual(it[1]["ShowOnDemandName"], "Mein & Dein Podcast (3 neu)")
        self.assertEqual(it[1]["ShowOnDemandURL"], f"http://aldi.wifiradiofrontier.com/portal/podcast/{self.pid}")
        # das Radio ruft die URL unveraendert auf (ohne Parameter)
        folgen = self.items(self.call(f"/portal/podcast/{self.pid}"))
        self.assertEqual([i["ShowEpisodeName"] for i in folgen[1:]],
                         ["* Folge 5", "* Folge 4", "* Folge 3", "Folge 2", "Folge 1"])
        self.assertEqual(folgen[1]["ShowEpisodeURL"],
                         f"http://aldi.wifiradiofrontier.com/portal/episode/{folgen[1]['ShowEpisodeID']}")
        self.assertEqual(self.call("/portal/podcast/999").status, 404)

    def test_folge_nachschlagen_typ5(self):
        eid = episode_id(self.pid, 3)
        it = self.items(self.call("/setupapp/aldi/asp/BrowseXML/Search.asp", f"sSearchtype=5&Search={eid}&mac=h"))
        self.assertEqual(it[1]["ItemType"], "ShowEpisode")
        self.assertIsNone(self.call("/setupapp/aldi/asp/BrowseXML/Search.asp", "sSearchtype=5&Search=99999999999999999999999999999999"),
                          "fremde IDs gehen an Airable")

    def test_folge_abspielen_loest_kette_auf_und_markiert_gehoert(self):
        eid = episode_id(self.pid, 5)  # Folge 5 (neueste)
        self.portal.streamer = mock.Mock()
        self.portal.streamer.probe.return_value = Probe("http://cdn.example/end.mp3", False, 206, "audio/mpeg")
        r = self.call(f"/portal/episode/{eid}")
        self.assertEqual((r.status, dict(r.headers)["Location"]), (302, "http://cdn.example/end.mp3"))
        self.portal.streamer.probe.assert_called_with("http://cdn.example/5.mp3")
        self.assertTrue(self.portal.podcasts.find_episode(eid)[1]["gehoert"])

    def test_folge_ueber_https_laeuft_ueber_relay(self):
        eid = episode_id(self.pid, 2)
        self.portal.streamer = mock.Mock()
        self.portal.streamer.probe.return_value = Probe("https://cdn.example/2.mp3", True, 200, "audio/mpeg")
        loc = dict(self.call(f"/portal/episode/{eid}").headers)["Location"]
        self.assertRegex(loc, r"^http://aldi\.wifiradiofrontier\.com/portal/stream/[0-9a-f]{16}\.mp3$")
        self.assertEqual(self.portal.relay_ids.get(loc.rsplit("/", 1)[1].split(".")[0]), "https://cdn.example/2.mp3")

    def test_kette_kaputt_dann_originaladresse(self):
        from radioportal.stream import StreamError
        eid = episode_id(self.pid, 1)
        self.portal.streamer = mock.Mock()
        self.portal.streamer.probe.side_effect = StreamError("weg")
        self.assertEqual(dict(self.call(f"/portal/episode/{eid}").headers)["Location"], "http://cdn.example/1.mp3")


class AirablePauseTests(unittest.TestCase):
    def test_nach_zwei_fehlern_wird_airable_pausiert(self):
        with tempfile.TemporaryDirectory() as d:
            portal = Portal(config.Config(data_dir=Path(d)), mock.Mock())
            portal.forwarder.forward.side_effect = UpstreamError("aus")
            call = lambda: radio.handle(portal, "aldi.wifiradiofrontier.com", "GET", "/vtuner", "", [])  # noqa: E731
            call()
            self.assertEqual(portal.airable_pause_until, 0, "ein Aussetzer sperrt noch nicht")
            call()
            call()
            self.assertEqual(portal.forwarder.forward.call_count, 2, "nach dem zweiten Fehler wird nicht mehr gefragt")
            portal.airable_pause_until = 0  # Pause abgelaufen: wieder versuchen
            call()
            self.assertEqual(portal.forwarder.forward.call_count, 3)

    def test_erfolg_setzt_zaehler_zurueck(self):
        with tempfile.TemporaryDirectory() as d:
            portal = Portal(config.Config(data_dir=Path(d)), mock.Mock())
            ok = Upstream(200, "OK", [], b"<ListOfItems><Item><ItemType>Dir</ItemType><Title>Sender</Title><UrlDir>http://x/?</UrlDir></Item></ListOfItems>")
            portal.forwarder.forward.side_effect = [UpstreamError("aus"), ok, UpstreamError("aus")]
            for _ in range(3):
                radio.handle(portal, "aldi.wifiradiofrontier.com", "GET", "/vtuner", "", [])
            self.assertEqual(portal.airable_pause_until, 0, "Fehler, Erfolg, Fehler: nie zwei in Folge")

    def test_leeres_menue_zaehlt_als_ausfall(self):
        with tempfile.TemporaryDirectory() as d:
            portal = Portal(config.Config(data_dir=Path(d)), mock.Mock())
            portal.forwarder.forward.return_value = Upstream(200, "OK", [], b"<ListOfItems/>")
            titles = [i.get("Title") for i in
                      ET.fromstring(radio.handle(portal, "aldi.wifiradiofrontier.com", "GET", "/vtuner", "", []).body).iter("Item")
                      for i in [{c.tag: c.text for c in i}]]
            self.assertEqual(titles, ["Favoriten"])
            self.assertEqual(portal.airable_failures, 1)


class SucheUndSicherungTests(unittest.TestCase):
    ITUNES = {"resultCount": 3, "results": [
        {"collectionName": "Logbuch <Netzpolitik>", "artistName": "Tim", "feedUrl": "https://feed.example/lnp",
         "primaryGenreName": "News", "country": "DEU"},
        {"collectionName": "Ohne Feed", "artistName": "X"},
        {"collectionName": "Boese", "feedUrl": "javascript:alert(1)"}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.portal = Portal(config.Config(data_dir=Path(self.tmp.name)))
        self.portal.podcast_search = lambda url: self.ITUNES

    def test_suche_nimmt_nur_echte_feeds(self):
        res = podcasts.search_podcasts("logbuch", self.portal.podcast_search)
        self.assertEqual([r["feed"] for r in res], ["https://feed.example/lnp"])

    def test_suche_in_der_oberflaeche_maskiert_und_bietet_abo(self):
        html = web.handle(self.portal, "GET", "/podcasts", "q=logbuch", {"Host": "raspi:8095"}).body.decode()
        self.assertIn("Logbuch &lt;Netzpolitik&gt;", html)
        self.assertIn("/podcasts/neu", html)
        self.assertNotIn("javascript:", html)

    def test_suche_ohne_netz(self):
        def kaputt(url):
            raise OSError("kein Netz")
        with self.assertRaises(PodcastError):
            podcasts.search_podcasts("x", kaputt)

    def test_sicherung(self):
        sid = self.portal.library.add_sender(name="Mein Sender", url="http://x/1")
        self.portal.library.fav_add(sid)
        self.portal.store.note_seen("1234567890123456", name="Alt")
        self.portal.store.set_ersatz("1234567890123456", sid)
        res = web.handle(self.portal, "GET", "/sicherung.json", "", {"Host": "raspi:8095"})
        self.assertEqual(res.status, 200)
        self.assertIn("attachment", dict(res.headers)["Content-Disposition"])
        import json
        data = json.loads(res.body.decode("utf-8"))
        self.assertEqual(data["favoriten"], [sid])
        self.assertEqual(data["airable"]["1234567890123456"]["ersatz_id"], sid)
        self.assertEqual(data["sender"][sid]["name"], "Mein Sender")


class WebPodcastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.portal = Portal(config.Config(data_dir=Path(self.tmp.name)))

    def post(self, path, **form):
        return web.handle(self.portal, "POST", path, "", {"Host": "raspi:8095", "Origin": "http://raspi:8095"},
                          urllib.parse.urlencode(form).encode())

    def get(self, path, query=""):
        return web.handle(self.portal, "GET", path, query, {"Host": "raspi:8095"})

    def test_abonnieren_aktualisieren_entfernen(self):
        with mock.patch("radioportal.podcasts.fetch_feed", return_value=parse_feed(FEED.encode())):
            res = self.post("/podcasts/neu", url="http://feed.example/rss")
            self.assertIn("abonniert", urllib.parse.unquote_plus(dict(res.headers)["Location"]))
            self.post("/podcasts/neu", url="http://feed.example/rss")
            self.assertEqual(len(self.portal.podcasts.all()), 1)
            html = self.get("/podcasts").body.decode()
            self.assertIn("Mein &amp; Dein Podcast", html)
            self.assertIn("Folge 5", self.get("/podcast", "id=5000001").body.decode())
            self.post("/podcasts/aktualisieren", id="alle")
            self.post("/podcasts/gehoert", id="5000001")
            self.assertEqual(self.portal.podcasts.unheard(self.portal.podcasts.get("5000001")), 0)
        self.post("/podcasts/entfernen", id="5000001")
        self.assertEqual(self.portal.podcasts.all(), {})
        self.assertEqual(self.get("/podcast", "id=1").status, 303)

    def test_fehler_beim_abonnieren(self):
        with mock.patch("radioportal.podcasts.fetch_feed", side_effect=PodcastError("kein Feed")):
            res = self.post("/podcasts/neu", url="http://x.example/")
        self.assertIn("Nicht abonniert", urllib.parse.unquote_plus(dict(res.headers)["Location"]))
        self.assertEqual(self.portal.podcasts.all(), {})

    def test_refresh_all_meldet_fehler_pro_feed(self):
        pid, _ = self.portal.podcasts.add("http://feed.example/rss", parse_feed(FEED.encode()))
        with mock.patch("radioportal.podcasts.fetch_feed", side_effect=PodcastError("weg")):
            res = podcasts.refresh_all(self.portal.podcasts, self.portal.streamer)
        self.assertEqual(res, {pid: "weg"})


if __name__ == "__main__":
    unittest.main()
