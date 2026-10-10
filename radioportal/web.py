"""Weboberflaeche (Heimnetz, ohne Anmeldung). Desktop zuerst: Tabellen, breite Seite;
auf dem Handy bleibt sie benutzbar. HTML kommt vom Server, Formulare per POST mit Redirect.

Zwei Seiten: *Sender* (die Liste am Radio, Suche, Adresse, Ausgeblendete, Anfrage-Protokoll)
und *Podcasts*."""

from __future__ import annotations

import datetime
import html
import json
import logging
import threading
import urllib.parse

from . import __version__
from . import podcasts as pod
from . import radio as radio_mod
from .radiobrowser import RadioBrowserError

log = logging.getLogger(__name__)
esc = html.escape

CSS = """
:root{--bg:#f6f6f4;--fg:#1d1d1b;--mute:#6b6b66;--card:#fff;--line:#dcdcd6;--acc:#1f5fbf;--ok:#1b7a3a;
--warn:#a35d00;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ececE8;--mute:#9a9a94;--card:#202020;--line:#383836;
--acc:#7fb0ff;--ok:#5fcf86;--warn:#f0b050;--bad:#ff8a80}}
*{box-sizing:border-box}
body{font:15px/1.45 system-ui,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
header{background:var(--card);border-bottom:1px solid var(--line)}
nav{max-width:1180px;margin:auto;padding:.6rem 1rem;display:flex;gap:1.2rem;flex-wrap:wrap;align-items:baseline}
nav b{margin-right:1rem}nav a{color:var(--fg);text-decoration:none;padding:.15rem 0}
nav a.on{border-bottom:2px solid var(--acc);color:var(--acc)}
main{max-width:1180px;margin:auto;padding:1rem}main a{color:var(--acc)}
h1{font-size:1.35rem;margin:.4rem 0 1rem}h2{font-size:1.1rem;margin:1.8rem 0 .6rem}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;padding:.45rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);background:var(--bg)}
td.r{white-space:nowrap;text-align:right}
.mute{color:var(--mute)}.klein{font-size:.82rem;word-break:break-all}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.msg{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--acc);padding:.6rem .9rem;margin:0 0 1rem}
.tipp{background:var(--card);border:1px solid var(--line);padding:.6rem .9rem;margin:1rem 0}
form{display:inline;margin:0}
button,input[type=submit]{font:inherit;padding:.25rem .7rem;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:4px;cursor:pointer}
button:hover{border-color:var(--acc)}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
input[type=text],input[type=search],input[type=url],select{font:inherit;padding:.3rem .5rem;border:1px solid var(--line);
border-radius:4px;background:var(--card);color:var(--fg)}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin:.4rem 0}
.row label{min-width:5rem}.grow{flex:1;min-width:14rem}
.leer{padding:1.5rem;text-align:center;color:var(--mute);background:var(--card);border:1px dashed var(--line)}
details{margin:1.8rem 0}summary{cursor:pointer;font-weight:600}
@media(max-width:700px){.hide-m{display:none}}
"""

NAV = (("/", "Sender"), ("/podcasts", "Podcasts"))


class Result:
    """Antwort der Weboberflaeche."""

    def __init__(self, status=200, ctype="text/html; charset=utf-8", body=b"", headers=()):
        self.status, self.ctype, self.body, self.headers = status, ctype, body, tuple(headers)


def page(titel: str, inhalt: str, aktiv: str = "", meldung: str = "") -> Result:
    nav = "".join(f'<a href="{u}"{" class=on" if u == aktiv else ""}>{esc(t)}</a>' for u, t in NAV)
    msg = f'<div class="msg">{esc(meldung)}</div>' if meldung else ""
    doc = (f'<!doctype html><html lang="de"><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{esc(titel)} · Küchenradio</title><style>{CSS}</style>'
           f'<header><nav><b>Küchenradio</b>{nav}</nav></header><main>{msg}<h1>{esc(titel)}</h1>{inhalt}'
           f'<p class="mute klein">radio-portal {esc(__version__)} · nur im Heimnetz · '
           f'<a href="/sicherung.json">Sicherung herunterladen</a></p></main>')
    return Result(body=doc.encode("utf-8"))


def redirect(path: str, meldung: str = "") -> Result:
    if meldung:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"m": meldung})
    return Result(303, "text/plain; charset=utf-8", b"", (("Location", path),))


def knopf(action: str, label: str, cls: str = "", **hidden) -> str:
    felder = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k, v in hidden.items())
    return f'<form method="post" action="{esc(action)}">{felder}<button class="{cls}">{label}</button></form>'


def status_badge(s: dict) -> str:
    """Kann das Radio den Sender spielen? (Anzeige fuer den Nutzer)"""
    codec = (s.get("codec") or "").upper()
    https = (s.get("url") or "").lower().startswith("https://")
    if codec in ("AAC", "HLS", "OGG", "WMA", "PLS", "M3U"):
        return f'<span class="warn" title="{esc(s.get("hinweis", ""))}">{esc(codec)}: spielt am Radio nicht</span>'
    if https:
        return '<span class="ok" title="Der Server holt den Strom">über Server</span>'
    return '<span class="ok">direkt</span>'


def fmt_codec(s: dict) -> str:
    kb = f" {esc(str(s.get('bitrate')))} kbit/s" if s.get("bitrate") else ""
    return f"{esc(s.get('codec') or '?')}{kb}"


def kurzzeit(iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%d.%m. %H:%M")
    except (TypeError, ValueError):
        return ""


def am_radio(s: dict) -> str:
    """Wann hat das Radio den Sender zuletzt nachgeschlagen?"""
    z = s.get("radio_zuletzt")
    if z is None:
        return '<span class="mute" title="nicht erfasst (Sender stammt aus Version 0.2)">–</span>'
    if not z:
        return '<span class="mute">noch nie</span>'
    return esc(kurzzeit(z))


def radio_status(portal) -> str:
    """Statuszeile(n) zu den Radios: meldet es sich bei uns, oder laeuft es still ueber Airable?"""
    radios = portal.radios.snapshot()
    if not radios:
        return ('<div class="msg">Noch kein Radio hat sich am Portal gemeldet. Am Radio müssen DNS 1 und DNS 2 '
                'auf diesen Server zeigen, dazu das Gateway auf den Router. Danach einmal den Netzstecker ziehen: '
                'Das Radio merkt sich DNS-Antworten lange.</div>')
    now = datetime.datetime.now().astimezone()
    zeilen = []
    for ip, e in sorted(radios.items()):
        try:
            t = datetime.datetime.fromisoformat(e["zuletzt"])
            sek = max(0, int((now - t).total_seconds()))
        except (KeyError, ValueError):
            continue
        if sek < 90:
            vor = "gerade eben"
        elif sek < 3600:
            vor = f"vor {sek // 60} Min."
        elif sek < 86400:
            vor = f"vor {sek // 3600} Std."
        else:
            vor = f"vor {sek // 86400} Tagen"
        cls = "ok" if sek < 6 * 3600 else "warn"
        hinweis = "" if sek < 6 * 3600 else (
            " · Nichts gehört. Normal, wenn das Radio aus ist oder seit Stunden denselben Sender spielt. Zeigt es "
            "das alte Airable-Menü: Am Radio müssen DNS 1 und DNS 2 auf den Server zeigen, danach Netzstecker ziehen.")
        zeilen.append(f'<span class="{cls}">●</span> Radio <b>{esc(ip)}</b> hat sich zuletzt {vor} am Portal '
                      f'gemeldet ({esc(kurzzeit(e["zuletzt"]))}), {int(e.get("anfragen", 0))} Anfragen '
                      f'insgesamt.{esc(hinweis)}')
    return '<p>' + '<br>'.join(zeilen) + '</p>'


# ---------------------------------------------------------------------------------------------------
def _liste_tabelle(portal) -> str:
    lib = portal.library
    liste = lib.liste()
    zeilen = []
    for i, sid in enumerate(liste):
        s = lib.sender(sid)
        if not s:
            continue
        info = f"Nr. {esc(sid)}" + (f" · {esc(s['genre'])}" if s.get("genre") else "")
        plaetze = "".join(f'<option value="{n}"{" selected" if n == i + 1 else ""}>{n}</option>'
                          for n in range(1, len(liste) + 1))
        platz = (f'<form method="post" action="/sender/platz"><input type="hidden" name="id" value="{esc(sid)}">'
                 f'<select name="platz" title="Auf Platz setzen" onchange="this.form.submit()">{plaetze}</select>'
                 f'<noscript> <button>Setzen</button></noscript></form>')
        zeilen.append(
            f'<tr><td class="mute">{i + 1}</td><td><b>{esc(s["name"])}</b><div class="klein mute">{info}</div></td>'
            f'<td class="hide-m">{fmt_codec(s)}</td><td>{status_badge(s)}</td><td class="hide-m">{am_radio(s)}</td>'
            f'<td class="r">{knopf("/sender/hoch", "↑", id=sid)} {knopf("/sender/runter", "↓", id=sid)} {platz} '
            f'<a href="/sender/bearbeiten?id={esc(sid)}">Bearbeiten</a> '
            f'{knopf("/sender/ausblenden", "Ausblenden", id=sid)}</td></tr>')
    if not zeilen:
        return '<div class="leer">Noch keine Sender. Unten suchen oder per Adresse hinzufügen.</div>'
    return ("<table><tr><th>#<th>Sender<th class=hide-m>Format<th>Radio<th class=hide-m>Zuletzt am Radio<th></tr>"
            + "".join(zeilen) + "</table>")


def _suche(portal, q: str, alle: bool) -> str:
    form = (f'<form method="get" action="/#hinzufuegen" class="row"><input type="search" name="q" class="grow" '
            f'value="{esc(q)}" placeholder="Sendername, z. B. Rock Antenne">'
            f'<label><input type="checkbox" name="alle" value="1"{" checked" if alle else ""}> '
            f'auch AAC &amp; andere (spielen am Radio nicht)</label><button class="pri">Suchen</button></form>')
    if not q:
        return form
    try:
        treffer = portal.rb.search(q, mp3_only=not alle)
    except RadioBrowserError as e:
        return form + f'<p class="bad">{esc(str(e))}</p>'
    if not treffer:
        return form + '<div class="leer">Keine Treffer.</div>'
    lib = portal.library
    liste = set(lib.liste())
    bekannt = {s.get("rb_uuid"): sid for sid, s in lib.all_senders().items() if s.get("rb_uuid")}
    zeilen = []
    for t in treffer:
        sid = bekannt.get(t["stationuuid"])
        if sid in liste:
            akt = '<span class="mute">steht in der Liste</span>'
        else:
            akt = knopf("/sender/hinzufuegen", "Wieder einblenden" if sid else "Hinzufügen", "pri",
                        uuid=t["stationuuid"])
        https = t["url"].lower().startswith("https://")
        zeilen.append(
            f'<tr><td><b>{esc(t["name"])}</b><div class="klein mute">{esc(t["tags"])}</div></td>'
            f'<td class="hide-m">{esc(t["countrycode"])}</td><td>{esc(t["codec"])} {esc(str(t["bitrate"]))}</td>'
            f'<td class="hide-m">{"https" if https else "http"}</td><td class="r">{akt}</td></tr>')
    return (form + f'<p class="mute">{len(treffer)} Treffer bei radio-browser.info, http zuerst. Beim Hinzufügen '
            f'wird die Adresse geprüft (dauert ein paar Sekunden).</p>'
            "<table><tr><th>Sender<th class=hide-m>Land<th>Format<th class=hide-m>Weg<th></tr>" + "".join(zeilen)
            + "</table>")


def _ausgeblendet(portal) -> str:
    lib = portal.library
    zeilen = []
    for sid in lib.ausgeblendet():
        s = lib.sender(sid)
        if lib.loeschbar(sid):
            weg = knopf("/sender/loeschen", "Löschen", id=sid)
        else:
            weg = '<span class="klein mute" title="Das Radio hat ihn gespielt: er kann auf der FAV-Taste liegen">bleibt</span>'
        zeilen.append(
            f'<tr><td><b>{esc(s["name"])}</b><div class="klein mute">Nr. {esc(sid)}</div></td>'
            f'<td class="hide-m">{am_radio(s)}</td>'
            f'<td class="r">{knopf("/sender/einblenden", "Einblenden", id=sid)} '
            f'<a href="/sender/bearbeiten?id={esc(sid)}">Bearbeiten</a> {weg}</td></tr>')
    if not zeilen:
        return ""
    return ('<h2>Ausgeblendet</h2><p class="mute">Nicht im Menü des Radios, auf der FAV-Taste spielen sie weiter. '
            'Löschen geht nur bei Sendern, die das Radio nie gespielt hat.</p>'
            "<table><tr><th>Sender<th class=hide-m>Zuletzt am Radio<th></tr>" + "".join(zeilen) + "</table>")


def _protokoll(portal) -> str:
    eintraege = portal.anfragen.snapshot()
    if not eintraege:
        return ('<details><summary>Letzte Anfragen des Radios</summary><p class="mute">Seit dem letzten Start des '
                'Dienstes keine.</p></details>')
    lib, pods = portal.library, portal.podcasts
    zeilen = []
    for e in eintraege[:100]:
        name = ""
        if e["id"]:
            s = lib.sender(e["id"]) or pods.get(e["id"])
            if s:
                name = s["name"]
            elif pods.find_episode(e["id"]):
                name = pods.find_episode(e["id"])[1]["titel"]
        cls = "ok" if e["status"] < 400 else "bad"
        was = esc(e["was"]) + (" " + esc(e["id"]) if e["id"] else "")
        if name:
            was += f'<div class="klein mute">{esc(name)}</div>'
        zeilen.append(
            f'<tr><td class="klein">{esc(e["zeit"][11:19])}</td><td class="hide-m klein">{esc(e["ip"])}</td>'
            f'<td>{was}</td><td class="{cls}">{int(e["status"])}</td><td>{esc(e["weg"])}</td></tr>')
    return ('<details><summary>Letzte Anfragen des Radios (Fehlersuche)</summary>'
            '<p class="mute">Nur seit dem letzten Start des Dienstes, neueste zuerst. „Airable“ heißt: Das Portal '
            'kannte die Anfrage nicht und hat sie weitergereicht.</p>'
            "<table><tr><th>Zeit<th class=hide-m>Radio<th>Anfrage<th>Antwort<th>Weg</tr>" + "".join(zeilen)
            + "</table></details>")


def seite_sender(portal, qs) -> Result:
    n_pod = len(portal.podcasts.all())
    darunter = (f'<p class="mute">Darunter am Radio: <b>Podcasts</b> (<a href="/podcasts">{n_pod} abonniert</a>).</p>'
                if n_pod else "")
    tipp = ('<div class="tipp"><b>So steht es am Radio:</b> <i>Internet Radio → Senderliste</i>, in dieser '
            'Reihenfolge.<br><b>FAV-Taste belegen:</b> den Sender am Radio spielen, FAV gedrückt halten, Platz wählen. '
            'Das Radio merkt sich die Nummer und den Namen von diesem Moment. Die Nummer spielt für immer denselben '
            'Sender: Name und Adresse kannst du hier ändern, <i>Ausblenden</i> nimmt ihn nur aus dem Menü, auf der '
            'FAV-Taste spielt er weiter. Lange Namen deshalb vor dem Speichern kürzen (<i>Bearbeiten</i>).</div>')
    q = qs.get("q", [""])[0].strip()
    adresse = ('<h3>Per Adresse</h3><form method="post" action="/sender/adresse" class="row">'
               '<input type="text" name="name" placeholder="Name (optional)">'
               '<input type="url" name="url" class="grow" placeholder="http://… (Stream, .m3u oder .pls)" required>'
               '<button>Prüfen</button></form>')
    inhalt = (radio_status(portal) + _liste_tabelle(portal) + darunter + tipp
              + '<h2 id="hinzufuegen">Sender hinzufügen</h2>' + _suche(portal, q, qs.get("alle", [""])[0] == "1")
              + adresse + _ausgeblendet(portal) + _protokoll(portal))
    return page("Sender", inhalt, "/", qs.get("m", [""])[0])


def seite_adresse(portal, qs, name="", url="", ergebnis=None, fehler="") -> Result:
    form = (f'<form method="post" action="/sender/adresse"><div class="row"><label>Name</label>'
            f'<input type="text" name="name" class="grow" value="{esc(name)}" placeholder="optional"></div>'
            f'<div class="row"><label>Adresse</label><input type="url" name="url" class="grow" '
            f'value="{esc(url)}" placeholder="http://… (Stream, .m3u oder .pls)" required>'
            f'<button class="pri">Prüfen</button></div></form>')
    inhalt = '<p class="mute">Die Adresse wird geprüft: Erreichbarkeit, MP3 oder nicht, http oder https.</p>' + form
    if fehler:
        inhalt += f'<p class="bad">{esc(fehler)}</p>'
    if ergebnis is not None:
        r = ergebnis
        cls = "ok" if r.spielbar else ("warn" if r.ok else "bad")
        inhalt += f'<h2>Ergebnis</h2><p class="{cls}">{esc(r.hinweis)}</p>'
        if r.ok:
            inhalt += (f'<p>Format: <b>{esc(r.codec)}</b>{" · " + esc(r.bitrate) + " kbit/s" if r.bitrate else ""}'
                       f'<br><span class="klein mute">{esc(r.start)}</span></p>'
                       + knopf("/sender/adresse/speichern", "Zur Liste hinzufügen", "pri", name=name or r.name or url,
                               start=r.start, orig=url, codec=r.codec, bitrate=r.bitrate, hinweis=r.hinweis,
                               stext=r.text, sgenre=r.genre))
    inhalt += '<p><a href="/">Zurück zur Liste</a></p>'
    return page("Sender per Adresse", inhalt, "/", qs.get("m", [""])[0])


def seite_bearbeiten(portal, qs, sid: str = "", name=None, url=None, fehler="", text=None) -> Result:
    sid = sid or qs.get("id", [""])[0]
    s = portal.library.sender(sid)
    if not s:
        return redirect("/", "Unbekannter Sender.")
    name = s["name"] if name is None else name
    url = (s.get("url_orig") or s["url"]) if url is None else url
    text = s.get("beschreibung", "") if text is None else text
    auto = radio_mod.beschreibung({**s, "beschreibung": ""})
    form = (f'<form method="post" action="/sender/bearbeiten"><input type="hidden" name="id" value="{esc(sid)}">'
            f'<div class="row"><label>Name</label><input type="text" name="name" class="grow" value="{esc(name)}" required></div>'
            f'<div class="row"><label>Adresse</label><input type="url" name="url" class="grow" value="{esc(url)}" required></div>'
            f'<div class="row"><label>Beschreibung</label><input type="text" name="beschreibung" class="grow" maxlength="120" '
            f'value="{esc(text)}" placeholder="{esc(auto or "leer lassen: keine angegeben")}"></div>'
            f'<div class="row"><label></label><button class="pri">Speichern</button></div></form>')
    fehl = f'<p class="bad">{esc(fehler)}</p>' if fehler else ""
    gespielt = s["url"] if s["url"] != url else ""
    info = (f'<table><tr><td>Nummer</td><td><b>{esc(sid)}</b> (bleibt für immer, die FAV-Taste speichert sie)</td></tr>'
            f'<tr><td>Format</td><td>{fmt_codec(s)} · {status_badge(s)}<div class="klein mute">{esc(s.get("hinweis", ""))}</div></td></tr>'
            + (f'<tr><td>Spielt</td><td class="klein">{esc(gespielt)}</td></tr>' if gespielt else "")
            + f'<tr><td>Zuletzt am Radio</td><td>{am_radio(s)}</td></tr>'
            f'<tr><td>Angelegt</td><td>{esc(kurzzeit(s.get("angelegt", "")))} · {esc(s.get("quelle", ""))}</td></tr></table>')
    tipp = ('<p class="mute">Ein neuer Name erscheint sofort im Menü des Radios. Auf der FAV-Taste steht weiter der '
            'Name vom Speichern, bis du den Platz am Radio neu belegst. Eine neue Adresse wird vorher geprüft.</p>')
    inhalt = (fehl + form + tipp + info + '<p>' + knopf("/sender/pruefen", "Adresse jetzt prüfen", id=sid)
              + ' <a href="/">Zurück zur Liste</a></p>')
    return page(f"Sender bearbeiten: {s['name']}", inhalt, "/", qs.get("m", [""])[0])


def seite_podcasts(portal, qs) -> Result:
    form = ('<form method="post" action="/podcasts/neu" class="row"><input type="url" name="url" class="grow" required '
            'placeholder="RSS-Adresse des Podcasts, z. B. https://…/feed.xml"><button class="pri">Abonnieren</button></form>')
    zeilen = []
    for pid, p in sorted(portal.podcasts.all().items(), key=lambda kv: kv[1]["name"].lower()):
        neu = portal.podcasts.unheard(p)
        zeilen.append(
            f'<tr><td><b><a href="/podcast?id={esc(pid)}">{esc(p["name"])}</a></b>'
            f'<div class="klein mute">{esc(p["feed"])}</div></td>'
            f'<td>{len(p["episoden"])}</td><td>{"<b>%d</b>" % neu if neu else "0"}</td>'
            f'<td class="hide-m klein">{esc(kurzzeit(p.get("aktualisiert", "")))}</td>'
            f'<td class="r">{knopf("/podcasts/aktualisieren", "Aktualisieren", id=pid)} '
            f'{knopf("/podcasts/gehoert", "Alle gehört", id=pid)} {knopf("/podcasts/entfernen", "✕", id=pid)}</td></tr>')
    tab = ("<table><tr><th>Podcast<th>Folgen<th>Neu<th class=hide-m>Aktualisiert<th></tr>" + "".join(zeilen) + "</table>"
           if zeilen else '<div class="leer">Noch kein Podcast abonniert.</div>')
    erkl = ('<p class="mute">Am Radio steht <b>Podcasts</b> unten in der Senderliste; ungehörte Folgen tragen ein '
            '<b>*</b>. Der Server prüft alle paar Stunden auf neue Folgen. Beim Abonnieren gelten nur die neuesten '
            'drei als ungehört. Die RSS-Adresse steht meist auf der Webseite des Podcasts („RSS“).</p>')
    alle = knopf("/podcasts/aktualisieren", "Alle aktualisieren", id="alle") if zeilen else ""
    q = qs.get("q", [""])[0].strip()
    suche = (f'<h2>Podcast suchen</h2><form method="get" action="/podcasts" class="row">'
             f'<input type="search" name="q" class="grow" value="{esc(q)}" placeholder="Name, z. B. Logbuch oder Tagesschau">'
             f'<button class="pri">Suchen</button></form>')
    if q:
        try:
            treffer = pod.search_podcasts(q, portal.podcast_search)
        except pod.PodcastError as e:
            suche += f'<p class="bad">{esc(str(e))}</p>'
        else:
            zl = "".join(
                f'<tr><td><b>{esc(r["name"])}</b><div class="klein mute">{esc(r["autor"])} · {esc(r["genre"])}</div></td>'
                f'<td class="r">{knopf("/podcasts/neu", "Abonnieren", "pri", url=r["feed"])}</td></tr>' for r in treffer)
            suche += ("<table><tr><th>Podcast<th></tr>" + zl + "</table>") if zl else '<div class="leer">Keine Treffer.</div>'
    return page("Podcasts", erkl + "<h2>Abonniert</h2>" + tab + f'<p>{alle}</p>' + suche
                + "<h2>Per RSS-Adresse</h2>" + form, "/podcasts", qs.get("m", [""])[0])


def seite_podcast(portal, qs) -> Result:
    p = portal.podcasts.get(qs.get("id", [""])[0])
    if not p:
        return redirect("/podcasts", "Unbekannter Podcast.")
    zeilen = []
    for e in p["episoden"]:
        mp3 = "mpeg" in (e.get("typ") or "mpeg").lower() or e["url"].lower().split("?")[0].endswith(".mp3")
        typ = "" if mp3 else f'<span class="warn" title="Das Radio spielt nur MP3">{esc(e.get("typ", ""))}</span>'
        zeilen.append(
            f'<tr><td>{"<b>*</b>" if not e["gehoert"] else ""}</td><td><b>{esc(e["titel"])}</b>'
            f'<div class="klein mute">{esc(e.get("beschreibung", "")[:160])}</div></td>'
            f'<td class="hide-m klein">{esc(e.get("datum", "")[:10])}</td><td class="hide-m klein">{esc(e.get("dauer", ""))}</td>'
            f'<td>{typ}</td></tr>')
    tab = ("<table><tr><th><th>Folge<th class=hide-m>Datum<th class=hide-m>Dauer<th></tr>" + "".join(zeilen) + "</table>")
    return page(p["name"], f'<p class="mute">{esc(p.get("beschreibung", ""))}</p>' + tab, "/podcasts")


# ------------------------------------------------------------------------------------ Aktionen (POST)
def _uebernehmen(portal, uuid: str) -> tuple[str | None, str]:
    """radio-browser-Sender pruefen und als eigenen Sender anlegen. -> (id | None, Meldung)"""
    try:
        st = portal.rb.by_uuid(uuid)
    except RadioBrowserError as e:
        return None, str(e)
    if not st or not st["url"]:
        return None, "Sender bei radio-browser.info nicht gefunden."
    r = portal.prober(st["url"])
    if not r.ok:
        return None, f"„{st['name']}“ nicht übernommen: {r.hinweis}"
    genre = (st["tags"].split(",")[0] or "").strip()
    sid = portal.library.add_sender(
        name=st["name"], url=r.start, codec=r.codec, bitrate=st["bitrate"] or r.bitrate, land=st["countrycode"],
        genre=genre, quelle="radio-browser", rb_uuid=uuid, url_orig=st["url"], hinweis=r.hinweis,
        tags=st["tags"], stream_text=r.text, stream_genre=r.genre)
    threading.Thread(target=portal.rb.count_click, args=(uuid,), daemon=True).start()
    return sid, r.hinweis


def _in_die_liste(portal, sid: str, hint: str = "") -> Result:
    lib = portal.library
    name = lib.sender(sid)["name"]
    if not lib.zeigen(sid):
        return redirect("/", f"„{name}“ steht schon in der Liste.")
    return redirect("/", f"„{name}“ steht jetzt in der Liste (Platz {len(lib.liste())}). {hint}".strip())


def aktion(portal, path: str, form: dict) -> Result:
    lib = portal.library
    f = lambda k: (form.get(k) or [""])[0].strip()  # noqa: E731

    if path == "/sender/hinzufuegen":
        sid, hint = _uebernehmen(portal, f("uuid"))
        return _in_die_liste(portal, sid, hint) if sid else redirect("/", hint)
    if path in ("/sender/hoch", "/sender/runter", "/sender/ausblenden", "/sender/einblenden"):
        sid = f("id")
        {"/sender/hoch": lambda: lib.verschieben(sid, -1), "/sender/runter": lambda: lib.verschieben(sid, 1),
         "/sender/ausblenden": lambda: lib.ausblenden(sid), "/sender/einblenden": lambda: lib.zeigen(sid)}[path]()
        return redirect("/")
    if path == "/sender/platz":
        s = lib.sender(f("id"))
        if not s or not f("platz").isdigit():
            return redirect("/", "Unbekannter Sender.")
        if lib.platz_setzen(s["id"], int(f("platz"))):
            return redirect("/", f"„{s['name']}“ steht jetzt auf Platz {lib.liste().index(s['id']) + 1}.")
        return redirect("/")
    if path == "/sender/loeschen":
        s = lib.sender(f("id"))
        if s and lib.remove_sender(s["id"]):
            return redirect("/", f"„{s['name']}“ gelöscht.")
        return redirect("/", "Nicht gelöscht: Das Radio hat den Sender schon gespielt, er kann auf der FAV-Taste liegen.")
    if path == "/sender/adresse":
        url = f("url")
        if not url:
            return seite_adresse(portal, {}, f("name"), url, fehler="Bitte eine Adresse eingeben.")
        return seite_adresse(portal, {}, f("name"), url, ergebnis=portal.prober(url))
    if path == "/sender/adresse/speichern":
        name, start = f("name"), f("start")
        if not name or not start:
            return redirect("/", "Name oder Adresse fehlt.")
        sid = lib.add_sender(name=name, url=start, codec=f("codec"), bitrate=f("bitrate"), quelle="manuell",
                             url_orig=f("orig") or start, hinweis=f("hinweis"), stream_text=f("stext")[:160],
                             stream_genre=f("sgenre")[:40])
        return _in_die_liste(portal, sid)
    if path == "/sender/bearbeiten":
        sid, name, url, text = f("id"), f("name"), f("url"), f("beschreibung")[:120]
        s = lib.sender(sid)
        if not s:
            return redirect("/", "Unbekannter Sender.")
        if not name or not url:
            return seite_bearbeiten(portal, {}, sid, name, url, fehler="Name und Adresse dürfen nicht leer sein.",
                                    text=text)
        felder, zusatz = {"name": name, "beschreibung": text}, ""
        if url != (s.get("url_orig") or s["url"]):
            r = portal.prober(url)
            if not r.ok:
                return seite_bearbeiten(portal, {}, sid, name, url, fehler=f"Nicht gespeichert: {r.hinweis}",
                                        text=text)
            felder.update(url=r.start, url_orig=url, codec=r.codec, bitrate=r.bitrate or s.get("bitrate", ""),
                          hinweis=r.hinweis, stream_text=r.text, stream_genre=r.genre)
            zusatz = f" Neue Adresse: {r.hinweis}"
        lib.update_sender(sid, **felder)
        return redirect("/", f"„{name}“ gespeichert.{zusatz}")
    if path == "/sender/pruefen":
        s = lib.sender(f("id"))
        if not s:
            return redirect("/", "Unbekannter Sender.")
        r = portal.prober(s.get("url_orig") or s["url"])
        if r.ok:
            lib.update_sender(s["id"], url=r.start, codec=r.codec, hinweis=r.hinweis, stream_text=r.text,
                              stream_genre=r.genre)
        return redirect(f"/sender/bearbeiten?id={s['id']}", f"Prüfung: {r.hinweis}")

    if path == "/podcasts/neu":
        url = f("url")
        try:
            parsed = pod.fetch_feed(portal.streamer, url)
        except pod.PodcastError as e:
            return redirect("/podcasts", f"Nicht abonniert: {e}")
        pid, neu = portal.podcasts.add(url, parsed)
        p = portal.podcasts.get(pid)
        return redirect("/podcasts", (f"„{p['name']}“ abonniert, {len(p['episoden'])} Folgen." if neu
                                      else f"„{p['name']}“ war schon abonniert."))
    if path == "/podcasts/aktualisieren":
        if f("id") == "alle":
            res = pod.refresh_all(portal.podcasts, portal.streamer)
            neu = sum(v for v in res.values() if isinstance(v, int))
            fehler = sum(1 for v in res.values() if not isinstance(v, int))
            return redirect("/podcasts", f"{neu} neue Folgen" + (f", {fehler} Feed(s) mit Fehler (Journal)" if fehler else "") + ".")
        p = portal.podcasts.get(f("id"))
        if not p:
            return redirect("/podcasts", "Unbekannter Podcast.")
        try:
            n = portal.podcasts.refresh(p["id"], pod.fetch_feed(portal.streamer, p["feed"]))
        except pod.PodcastError as e:
            return redirect("/podcasts", f"„{p['name']}“: {e}")
        return redirect("/podcasts", f"„{p['name']}“: {n} neue Folgen.")
    if path == "/podcasts/gehoert":
        portal.podcasts.mark_all_heard(f("id"))
        return redirect("/podcasts", "Alle Folgen als gehört markiert.")
    if path == "/podcasts/entfernen":
        p = portal.podcasts.get(f("id"))
        ok = bool(p) and portal.podcasts.remove(f("id"))
        return redirect("/podcasts", f"„{p['name']}“ entfernt." if ok else "Unbekannter Podcast.")
    return Result(404, "text/plain; charset=utf-8", b"Nicht gefunden.\n")


def _origin_ok(headers: dict) -> bool:
    """Einfacher Schutz gegen fremde Webseiten: Origin muss zum Host passen."""
    origin, host = headers.get("origin", ""), headers.get("host", "")
    if not origin or not host:
        return False
    return urllib.parse.urlsplit(origin).netloc.lower() == host.lower()


GET_SEITEN = {"/": seite_sender, "/sender/bearbeiten": seite_bearbeiten, "/sender/adresse": seite_adresse,
              "/podcasts": seite_podcasts, "/podcast": seite_podcast}


def sicherung(portal) -> Result:
    """Alle Daten als eine JSON-Datei zum Herunterladen (Sender, Senderliste, Podcasts)."""
    data = {"erstellt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "version": __version__, "sender": portal.library.all_senders(), "senderliste": portal.library.liste(),
            "podcasts": portal.podcasts.all()}
    body = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    name = "kuechenradio-sicherung-" + datetime.date.today().isoformat() + ".json"
    return Result(200, "application/json; charset=utf-8", body,
                  (("Content-Disposition", f'attachment; filename="{name}"'),))


def handle(portal, command: str, path: str, query: str = "", headers: dict | None = None,
           body: bytes = b"") -> Result:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if path == "/healthz":
        return Result(body=b"ok", ctype="text/plain; charset=utf-8")
    if command in ("GET", "HEAD"):
        if path == "/sicherung.json":
            return sicherung(portal)
        seite = GET_SEITEN.get(path)
        if seite:
            return seite(portal, urllib.parse.parse_qs(query))
        return Result(404, "text/plain; charset=utf-8", b"Nicht gefunden.\n")
    if command == "POST":
        if not _origin_ok(headers):
            return Result(403, "text/plain; charset=utf-8", b"Anfrage von fremder Seite abgelehnt.\n")
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        try:
            return aktion(portal, path, form)
        except Exception:  # noqa: BLE001
            log.exception("Aktion %s fehlgeschlagen", path)
            return redirect("/", "Das hat nicht geklappt (siehe Journal).")
    return Result(405, "text/plain; charset=utf-8", b"Nicht erlaubt.\n")
