"""Weboberflaeche (Heimnetz, ohne Anmeldung). Desktop zuerst: Tabellen, breite Seite;
auf dem Handy bleibt sie benutzbar. HTML kommt vom Server, Formulare per POST mit Redirect."""

from __future__ import annotations

import datetime
import html
import logging
import threading
import urllib.parse

from . import __version__
from . import probe as probe_mod
from . import radio
from . import podcasts as pod
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
h1{font-size:1.35rem;margin:.4rem 0 1rem}h2{font-size:1.1rem;margin:1.6rem 0 .6rem}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;padding:.45rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);background:var(--bg)}
td.r{white-space:nowrap;text-align:right}
.mute{color:var(--mute)}.klein{font-size:.82rem;word-break:break-all}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.msg{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--acc);padding:.6rem .9rem;margin:0 0 1rem}
form{display:inline;margin:0}
button,input[type=submit]{font:inherit;padding:.25rem .7rem;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:4px;cursor:pointer}
button:hover{border-color:var(--acc)}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
input[type=text],input[type=search],input[type=url],select{font:inherit;padding:.3rem .5rem;border:1px solid var(--line);
border-radius:4px;background:var(--card);color:var(--fg)}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin:.4rem 0}
.row label{min-width:5rem}.grow{flex:1;min-width:14rem}
.leer{padding:1.5rem;text-align:center;color:var(--mute);background:var(--card);border:1px dashed var(--line)}
@media(max-width:700px){.hide-m{display:none}}
"""

NAV = (("/", "Favoriten"), ("/suche", "Sender suchen"), ("/neu", "Sender per Adresse"),
       ("/podcasts", "Podcasts"),
       ("/sender", "Alle Sender"), ("/tasten", "FAV-Liste"))


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
    if codec in ("AAC", "HLS", "OGG", "WMA", "PLS", "M3U") and codec != "MP3":
        return f'<span class="warn" title="{esc(s.get("hinweis", ""))}">{esc(codec)}: spielt am Radio nicht</span>'
    if https:
        return '<span class="ok" title="Der Server holt den Strom">über Server</span>'
    return '<span class="ok">direkt</span>'


def fmt_codec(s: dict) -> str:
    kb = f" {esc(str(s.get('bitrate')))} kbit/s" if s.get("bitrate") else ""
    return f"{esc(s.get('codec') or '?')}{kb}"


def radio_status(portal) -> str:
    """Statuszeile(n) zu den Radios: meldet es sich bei uns, oder laeuft es still ueber Airable?"""
    radios = portal.radios.snapshot()
    if not radios:
        return ('<div class="msg">Noch kein Radio hat sich am Portal gemeldet. Stimmen DNS 1 (der Server) und '
                'Gateway am Radio? Das Radio merkt sich DNS-Antworten minutenlang: einmal Strom trennen.</div>')
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
        hinweis = "" if sek < 6 * 3600 else (" · Nichts gehört. Normal, wenn das Radio aus ist; sonst fragt es "
                                             "vielleicht die FRITZ!Box statt des Servers (Netzwerk-Einstellungen am Radio prüfen).")
        zeilen.append(f'<span class="{cls}">●</span> Radio <b>{esc(ip)}</b> hat sich zuletzt {vor} am Portal '
                      f'gemeldet ({esc(e["zuletzt"][:16].replace("T", " "))}), {int(e.get("anfragen", 0))} Anfragen '
                      f'insgesamt.{esc(hinweis)}')
    return '<p>' + '<br>'.join(zeilen) + '</p>'


# ---------------------------------------------------------------------------------------------------
def seite_favoriten(portal, qs) -> Result:
    lib = portal.library
    favs = lib.favorites()
    zeilen = []
    for i, sid in enumerate(favs):
        s = lib.sender(sid)
        if not s:
            continue
        zeilen.append(
            f'<tr><td class="mute">{i + 1}</td><td><b>{esc(s["name"])}</b>'
            f'<div class="klein mute">{esc(s.get("genre", ""))}</div></td>'
            f'<td class="hide-m">{esc(s.get("land", ""))}</td><td>{fmt_codec(s)}</td><td>{status_badge(s)}</td>'
            f'<td class="r">{knopf("/favorit/hoch", "↑", id=sid)} {knopf("/favorit/runter", "↓", id=sid)} '
            f'{knopf("/favorit/weg", "✕", id=sid)}</td></tr>')
    if zeilen:
        tab = ("<table><tr><th>Platz<th>Sender<th class=hide-m>Land<th>Format<th>Radio<th></tr>"
               + "".join(zeilen) + "</table>")
    else:
        tab = ('<div class="leer">Noch keine Favoriten. <a href="/suche">Sender suchen</a> oder '
               '<a href="/neu">per Adresse hinzufügen</a>.</div>')
    n_air = len(portal.store.snapshot())
    info = (f'<p class="mute">Am Radio unter <b>Internet Radio → Favoriten</b> (ganz oben im Menü). '
            f'Reihenfolge wie hier. Der FAV-Eintrag „Favorit 3“ spielt immer Platz 3 '
            f'(Plätze 1–{radio.PLAETZE}, einmal am Radio unter <i>Favoriten → Favorit-Plaetze</i> mit FAV halten speichern). {n_air} Airable-Sender vom Radio erfasst: '
            f'<a href="/tasten">FAV-Liste</a>.</p>')
    return page("Favoriten", radio_status(portal) + info + tab, "/", qs.get("m", [""])[0])


def seite_suche(portal, qs) -> Result:
    q = qs.get("q", [""])[0].strip()
    alle = qs.get("alle", [""])[0] == "1"
    form = (f'<form method="get" action="/suche" class="row"><input type="search" name="q" class="grow" '
            f'value="{esc(q)}" placeholder="Sendername, z. B. Rock Antenne" autofocus>'
            f'<label><input type="checkbox" name="alle" value="1"{" checked" if alle else ""}> '
            f'auch AAC &amp; andere (spielen evtl. nicht)</label><button class="pri">Suchen</button></form>')
    inhalt = form
    if q:
        try:
            treffer = portal.rb.search(q, mp3_only=not alle)
        except RadioBrowserError as e:
            return page("Sender suchen", form + f'<p class="bad">{esc(str(e))}</p>', "/suche")
        zeilen = []
        for t in treffer:
            https = t["url"].lower().startswith("https://")
            zeilen.append(
                f'<tr><td><b>{esc(t["name"])}</b><div class="klein mute">{esc(t["tags"])}</div></td>'
                f'<td class="hide-m">{esc(t["countrycode"])}</td>'
                f'<td>{esc(t["codec"])} {esc(str(t["bitrate"]))}</td>'
                f'<td class="hide-m">{"https" if https else "http"}</td>'
                f'<td class="r">{knopf("/favorit/uebernehmen", "★ Favorit", "pri", uuid=t["stationuuid"])}</td></tr>')
        inhalt += (f'<p class="mute">{len(treffer)} Treffer, http zuerst. Beim Übernehmen wird die Adresse geprüft '
                   f'(dauert ein paar Sekunden).</p>' if treffer else '<div class="leer">Keine Treffer.</div>')
        if zeilen:
            inhalt += ("<table><tr><th>Sender<th class=hide-m>Land<th>Format<th class=hide-m>Weg<th></tr>"
                       + "".join(zeilen) + "</table>")
    return page("Sender suchen", inhalt, "/suche", qs.get("m", [""])[0])


def seite_neu(portal, qs, name="", url="", ergebnis=None, fehler="") -> Result:
    form = (f'<form method="post" action="/neu"><div class="row"><label>Name</label>'
            f'<input type="text" name="name" class="grow" value="{esc(name)}" placeholder="optional"></div>'
            f'<div class="row"><label>Adresse</label><input type="url" name="url" class="grow" '
            f'value="{esc(url)}" placeholder="http://… (Stream, .m3u oder .pls)" required>'
            f'<button class="pri">Prüfen</button></div></form>')
    inhalt = ('<p class="mute">Die Adresse wird vorher geprüft: Erreichbarkeit, MP3 oder nicht, http oder https.</p>'
              + form)
    if fehler:
        inhalt += f'<p class="bad">{esc(fehler)}</p>'
    if ergebnis is not None:
        r = ergebnis
        cls = "ok" if r.spielbar else ("warn" if r.ok else "bad")
        inhalt += f'<h2>Ergebnis</h2><p class="{cls}">{esc(r.hinweis)}</p>'
        if r.ok:
            inhalt += (f'<p>Format: <b>{esc(r.codec)}</b>{" · " + esc(r.bitrate) + " kbit/s" if r.bitrate else ""}'
                       f'<br><span class="klein mute">{esc(r.start)}</span></p>'
                       + knopf("/neu/speichern", "Speichern und als Favorit", "pri", name=name or r.name or url,
                               start=r.start, orig=url, codec=r.codec, bitrate=r.bitrate, hinweis=r.hinweis))
    return page("Sender per Adresse", inhalt, "/neu", qs.get("m", [""])[0])


def seite_sender(portal, qs) -> Result:
    lib = portal.library
    favs = set(lib.favorites())
    ersatz = portal.store.ersatz_ids()
    zeilen = []
    for sid, s in sorted(lib.all_senders().items(), key=lambda kv: int(kv[0])):
        akt = []
        if sid not in favs:
            akt.append(knopf("/favorit/add", "★", id=sid))
        akt.append(knopf("/sender/pruefen", "Prüfen", id=sid))
        if sid not in favs and sid not in ersatz:
            akt.append(knopf("/sender/entfernen", "Entfernen", id=sid))
        marke = (" ★" if sid in favs else "") + (" ⌨" if sid in ersatz else "")
        zeilen.append(
            f'<tr><td class="mute">{esc(sid)}</td><td><b>{esc(s["name"])}</b>{marke}'
            f'<div class="klein mute">{esc(s.get("start", s["url"]))}</div></td>'
            f'<td>{fmt_codec(s)}</td><td>{status_badge(s)}</td><td class="r">{" ".join(akt)}</td></tr>')
    tab = ("<table><tr><th>ID<th>Sender<th>Format<th>Radio<th></tr>" + "".join(zeilen) + "</table>"
           if zeilen else '<div class="leer">Noch keine eigenen Sender.</div>')
    leg = '<p class="mute">★ = Favorit, ⌨ = ersetzt einen Eintrag der FAV-Liste. Diese lassen sich nicht entfernen.</p>'
    return page("Alle Sender", leg + tab, "/sender", qs.get("m", [""])[0])


def seite_tasten(portal, qs) -> Result:
    lib = portal.library
    eigene = sorted(lib.all_senders().values(), key=lambda s: s["name"].lower())
    opts = "".join(f'<option value="{radio.PLATZ_ERSTE + n - 1}">Favorit {n} (jeweils der {n}. Favorit)</option>'
                   for n in range(1, radio.PLAETZE + 1))
    opts += "".join(f'<option value="{esc(s["id"])}">{esc(s["name"])}</option>' for s in eigene)
    zeilen = []
    air = portal.store.snapshot()
    for aid, e in sorted(air.items(), key=lambda kv: kv[1].get("zuletzt", ""), reverse=True):
        eid = e.get("ersatz_id", "")
        nr = radio.platz_nr(eid) if eid else 0
        es = radio.lookup(portal, aid) if eid else None
        if nr or es:
            ziel = (f"Favorit {nr}" + (f" (jetzt: {es['name']})" if es else " (noch leer)")) if nr else es["name"]
            ers = (f'<span class="ok">→ {esc(ziel)}</span> '
                   + knopf("/tasten/ersatz", "Entfernen", aid=aid, sid=""))
        else:
            ers = (f'<form method="post" action="/tasten/ersatz"><input type="hidden" name="aid" value="{esc(aid)}">'
                   f'<select name="sid">{opts}</select> <button>Setzen</button></form> '
                   f'<a href="/tasten/vorschlag?id={esc(aid)}">Vorschlag suchen</a>')
        zeilen.append(
            f'<tr><td><b>{esc(e.get("name") or "(unbekannt)")}</b>'
            f'<div class="klein mute">ID {esc(aid)} · {esc(e.get("format", ""))} · {esc(e.get("ort", ""))} · '
            f'{esc(str(e.get("bitrate", "")))} kbit/s</div><div class="klein mute">{esc(e.get("airable_url", ""))}</div></td>'
            f'<td class="hide-m klein">{int(e.get("gespielt", 0))}× · {esc(e.get("zuletzt", "")[:16].replace("T", " "))}</td>'
            f'<td>{ers}</td></tr>')
    erkl = ('<p class="mute">Die FAV-Taste des Radios (und bei anderen Modellen die Stationstasten) speichert nur die Airable-ID. Wählst du hier '
            'einen Ersatz-Sender, antwortet dieses Portal selbst auf die Taste, auch wenn Airable abschaltet. '
            'Erfasst wird, was das Radio nachschlägt oder abspielt: den Eintrag in der FAV-Liste einmal aufrufen, dann erscheint er hier. '
            'Neue Einträge in der FAV-Liste, die du am Radio selbst anlegst, tragen unsere IDs und brauchen keinen Ersatz.</p>')
    tab = ("<table><tr><th>Airable-Sender am Radio<th class=hide-m>Gespielt · zuletzt<th>Ersatz-Sender</tr>"
           + "".join(zeilen) + "</table>") if zeilen else '<div class="leer">Noch nichts erfasst.</div>'
    plaetze = "".join(
        f"<tr><td>Favorit {n}<td>" + (esc(radio.platz_sender(portal, n)["name"]) if radio.platz_sender(portal, n) else
                                      '<span class="mute">noch leer</span>') + "</tr>"
        for n in range(1, radio.PLAETZE + 1))
    ohne = [a for a, e in air.items() if not e.get("ersatz_id")]
    reihe = (knopf("/tasten/reihe", "Alle Einträge ohne Ersatz der Reihe nach auf Favorit 1, 2, 3 … legen", "pri")
             if ohne else "")
    unten = ("<h2>Favorit-Plätze</h2><p class=\"mute\">Am Radio unter <i>Internetradio → Favoriten → Favorit-Plätze</i>: "
             "den Platz anspielen und FAV halten. Die FAV-Liste zeigt dann immer „Favorit k“, gespielt wird der "
             "k-te Favorit von hier.</p><table><tr><th>Platz<th>spielt jetzt</tr>" + plaetze + "</table>")
    return page("FAV-Liste des Radios", erkl + reihe + tab + unten, "/tasten", qs.get("m", [""])[0])


def seite_vorschlag(portal, qs) -> Result:
    aid = qs.get("id", [""])[0]
    e = portal.store.snapshot().get(aid)
    if not e:
        return redirect("/tasten", "Unbekannte Airable-ID.")
    q = qs.get("q", [""])[0].strip() or e.get("name", "")
    form = (f'<form method="get" action="/tasten/vorschlag" class="row"><input type="hidden" name="id" value="{esc(aid)}">'
            f'<input type="search" name="q" class="grow" value="{esc(q)}"><button class="pri">Suchen</button></form>')
    try:
        treffer = portal.rb.search(q, mp3_only=True) if q else []
    except RadioBrowserError as ex:
        return page("Ersatz suchen", form + f'<p class="bad">{esc(str(ex))}</p>', "/tasten")
    zeilen = "".join(
        f'<tr><td><b>{esc(t["name"])}</b><div class="klein mute">{esc(t["tags"])}</div></td>'
        f'<td>{esc(t["countrycode"])}</td><td>{esc(t["codec"])} {esc(str(t["bitrate"]))}</td>'
        f'<td class="r">{knopf("/tasten/uebernehmen", "Als Ersatz", "pri", aid=aid, uuid=t["stationuuid"])}</td></tr>'
        for t in treffer)
    tab = ("<table><tr><th>Sender<th>Land<th>Format<th></tr>" + zeilen + "</table>"
           if zeilen else '<div class="leer">Keine Treffer.</div>')
    return page(f"Ersatz für „{e.get('name') or aid}“", form + tab, "/tasten")


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
            f'<td class="hide-m klein">{esc(p.get("aktualisiert", "")[:16].replace("T", " "))}</td>'
            f'<td class="r">{knopf("/podcasts/aktualisieren", "Aktualisieren", id=pid)} '
            f'{knopf("/podcasts/gehoert", "Alle gehört", id=pid)} {knopf("/podcasts/entfernen", "✕", id=pid)}</td></tr>')
    tab = ("<table><tr><th>Podcast<th>Folgen<th>Neu<th class=hide-m>Aktualisiert<th></tr>" + "".join(zeilen) + "</table>"
           if zeilen else '<div class="leer">Noch kein Podcast abonniert.</div>')
    erkl = ('<p class="mute">Eigene Podcasts laufen ohne Airable. Die RSS-Adresse steht meist auf der Webseite des '
            'Podcasts („RSS“) oder bei Apple Podcasts/Spotify nicht, aber beim Anbieter. Am Radio erscheint '
            '<b>Eigene Podcasts</b> im Hauptmenü; ungehörte Folgen tragen ein <b>*</b>. Der Server prüft alle '
            'paar Stunden auf neue Folgen. Beim Abonnieren gelten nur die neuesten drei als ungehört.</p>')
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
    return page("Podcasts", erkl + suche + "<h2>Abonniert</h2>" + tab + f'<p>{alle}</p>'
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
        genre=genre, quelle="radio-browser", rb_uuid=uuid, url_orig=st["url"], hinweis=r.hinweis)
    threading.Thread(target=portal.rb.count_click, args=(uuid,), daemon=True).start()
    return sid, r.hinweis


def aktion(portal, path: str, form: dict) -> Result:
    lib = portal.library
    f = lambda k: (form.get(k) or [""])[0].strip()  # noqa: E731

    if path == "/favorit/uebernehmen":
        sid, hint = _uebernehmen(portal, f("uuid"))
        if not sid:
            return redirect("/suche", hint)
        lib.fav_add(sid)
        return redirect("/", f"„{lib.sender(sid)['name']}“ ist jetzt Favorit. {hint}")
    if path in ("/favorit/add", "/favorit/hoch", "/favorit/runter", "/favorit/weg"):
        sid = f("id")
        {"/favorit/add": lambda: lib.fav_add(sid), "/favorit/hoch": lambda: lib.fav_move(sid, -1),
         "/favorit/runter": lambda: lib.fav_move(sid, 1), "/favorit/weg": lambda: lib.fav_remove(sid)}[path]()
        return redirect("/sender" if path == "/favorit/add" else "/")
    if path == "/neu":
        url = f("url")
        r = portal.prober(url) if url else None
        if r is None:
            return seite_neu(portal, {}, f("name"), url, fehler="Bitte eine Adresse eingeben.")
        return seite_neu(portal, {}, f("name"), url, ergebnis=r)
    if path == "/neu/speichern":
        name, start = f("name"), f("start")
        if not name or not start:
            return redirect("/neu", "Name oder Adresse fehlt.")
        sid = lib.add_sender(name=name, url=start, codec=f("codec"), bitrate=f("bitrate"), quelle="manuell",
                             url_orig=f("orig") or start, hinweis=f("hinweis"))
        lib.fav_add(sid)
        return redirect("/", f"„{name}“ ist jetzt Favorit.")
    if path == "/sender/pruefen":
        s = lib.sender(f("id"))
        if not s:
            return redirect("/sender", "Unbekannter Sender.")
        r = portal.prober(s.get("url_orig") or s["url"])
        if r.ok:
            lib.update_sender(s["id"], url=r.start, codec=r.codec, hinweis=r.hinweis)
        return redirect("/sender", f"„{s['name']}“: {r.hinweis}")
    if path == "/sender/entfernen":
        s = lib.sender(f("id"))
        ok = bool(s) and lib.remove_sender(f("id"), portal.store.ersatz_ids())
        return redirect("/sender", f"„{s['name']}“ entfernt." if ok else "Nicht entfernt (Favorit oder Taste).")
    if path == "/tasten/ersatz":
        aid, sid = f("aid"), f("sid")
        if sid and not lib.sender(sid) and not radio.platz_nr(sid):
            return redirect("/tasten", "Unbekannter Sender.")
        ok = portal.store.set_ersatz(aid, sid or None)
        return redirect("/tasten", ("Ersatz gesetzt." if sid else "Ersatz entfernt.") if ok else "Unbekannte ID.")
    if path == "/tasten/reihe":
        n = 0
        for aid, e in sorted(portal.store.snapshot().items(), key=lambda kv: kv[1].get("name", "").lower()):
            if not e.get("ersatz_id") and n < radio.PLAETZE:
                portal.store.set_ersatz(aid, str(radio.PLATZ_ERSTE + n))
                n += 1
        return redirect("/tasten", f"{n} Einträge auf Favorit 1 bis {n} gelegt.")
    if path == "/tasten/uebernehmen":
        aid = f("aid")
        if aid not in portal.store.snapshot():
            return redirect("/tasten", "Unbekannte Airable-ID.")
        sid, hint = _uebernehmen(portal, f("uuid"))
        if not sid:
            return redirect(f"/tasten/vorschlag?id={aid}", hint)
        portal.store.set_ersatz(aid, sid)
        return redirect("/tasten", f"Ersatz: „{lib.sender(sid)['name']}“. {hint}")

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


GET_SEITEN = {"/": seite_favoriten, "/suche": seite_suche, "/sender": seite_sender, "/tasten": seite_tasten,
              "/tasten/vorschlag": seite_vorschlag, "/podcasts": seite_podcasts, "/podcast": seite_podcast, "/neu": lambda p, q: seite_neu(p, q)}


def sicherung(portal) -> Result:
    """Alle Daten als eine JSON-Datei zum Herunterladen (Sender, Favoriten, FAV-Zuordnung, Podcasts)."""
    import json
    data = {"erstellt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "version": __version__, "sender": portal.library.all_senders(), "favoriten": portal.library.favorites(),
            "airable": portal.store.snapshot(), "podcasts": portal.podcasts.all()}
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
