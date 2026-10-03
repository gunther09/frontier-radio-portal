"""Testmenue (`TESTMENUE=ja`): klaert am Radio die offenen Fragen (Umlaute, AAC, https,
Weiterleitung, langer Name). Feste IDs ab 9000001, getrennt von den eigenen Sendern; die
Adressen koennen mit der Zeit veralten."""

from __future__ import annotations

MP3_HTTP = "http://s4-webradio.rockantenne.de/coversongs/stream/mp3"

TEST_SENDER = {
    "9000001": {"name": "Test 1: MP3 ueber http (Grundtest)", "url": MP3_HTTP, "codec": "MP3"},
    "9000002": {"name": "Test 2: AAC ueber http", "url": "http://listen.trancebase.fm/tunein-aac-hd-pls",
                "codec": "AAC"},
    "9000003": {"name": "Test 3: MP3 nur ueber https", "url": "https://stream.zeno.fm/6n6ewddtad0uv",
                "codec": "MP3"},
    # Das Radio bekommt eine Adresse des Portals, die selbst mit 302 weiterleitet (kein Aufloesen)
    "9000004": {"name": "Test 4: Adresse leitet per 302 weiter", "url": "@umleitung", "codec": "MP3",
                "raw": True, "ziel": MP3_HTTP},
    "9000005": {"name": "Test 5: Sender mit einem sehr langen Namen, der weit ueber die Anzeige des "
                        "Radios hinausgeht und nur vorn lesbar sein duerfte", "url": MP3_HTTP, "codec": "MP3"},
}

UMLAUT_ZEILEN = ("Umlaute: ÄÖÜ äöü ß é", "Umgeschrieben: AeOeUe aeoeue ss")


def sender(sid: str) -> dict | None:
    s = TEST_SENDER.get(str(sid))
    return {"id": str(sid), "bitrate": "", "land": "Test", "genre": "Test", **s} if s else None
