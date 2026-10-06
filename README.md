# frontier-radio-portal

**Deutsch** · [English](#english)

Eigenes Portal für alte Internetradios mit Frontier-Silicon-Modul (getestet nur mit dem
Tevion IWR 294, Firmware 1.5.6), seit Frontier sein Portal Nuvola am 31.10.2024 abgeschaltet hat.
Die Radios holen Menüs und Sender per unverschlüsseltem HTTP von
`<marke>.wifiradiofrontier.com` und prüfen dabei nichts. Dieser Dienst beantwortet diese
Anfragen selbst. Die Firmware der Radios bleibt unangetastet.

```
Radio ── DNS ───────> Server (dnsmasq): *.wifiradiofrontier.com → Server, alles andere → Router
Radio ── HTTP :80 ──> Server ── Umleitung nur für Radio-IPs ──> radioportal :8095
                                              ├── Senderliste, Podcasts, Abspielen → eigene Antwort
                                              └── Unbekanntes → Übergangsdienst von Airable (solange es ihn gibt)
PC im Heimnetz ────> http://SERVER:8095  (Weboberfläche)
```

## Was es kann

- **Eine einfache Senderliste:** Unter *Internet Radio* zeigt das Radio direkt deine Sender, in der
  Reihenfolge der Weboberfläche, darunter *Podcasts*. Airables Menüs kommen nicht mehr vor.
- **FAV-Taste einmal vorprogrammieren:** Die FAV-Liste liegt im Radio (Nummer und Beschriftung, von
  außen nicht änderbar; beim Abspielen schlägt das Radio nur die Nummer nach). Jeder Sender hat eine
  feste Nummer ab 1000001, die **nie** neu vergeben wird und immer denselben Sender spielt. Man spielt
  den Sender am Radio und hält FAV gedrückt, fertig. In der Weboberfläche lassen sich Name und
  Stream-Adresse ändern; *Ausblenden* nimmt einen Sender nur aus dem Menü, auf der FAV-Taste spielt er
  weiter. Löschen geht nur bei Sendern, die das Radio nie gespielt hat.
- **Sender finden:** Suche bei radio-browser.info oder Sender per Adresse, jeweils mit Prüfung.
- **Eigene Podcasts** (RSS, Suche über iTunes), ungehörte Folgen mit `*`. Podcast-Server leiten oft
  mehrfach weiter und nutzen https: das löst der Server auf und reicht die Folge bei Bedarf als http
  durch, denn das Radio folgt höchstens einer Weiterleitung und kann kein https.
- **Stream-Prüfung:** Erreichbar? MP3? http oder https? Playlists (`.m3u`, `.pls`) und
  Shoutcast-Antworten (`ICY 200 OK`) werden gelesen. Weiterleitungen löst der Server beim
  Abspielen auf (höchstens 5 Sekunden, Sitzungskennungen in den Adressen verfallen sonst).
  Nur-https-Sender holt der Server und reicht sie als http durch.
- **Fehlersuche auf der Startseite:** wann sich das Radio zuletzt gemeldet hat, und die letzten
  Anfragen des Radios (nur im Speicher, ohne die Kennung des Radios). Zeigt das Radio das alte
  Airable-Menü, sieht man hier, dass es am Portal vorbeiläuft.
- **Sicherung:** `http://SERVER:8095/sicherung.json` lädt alle Daten als eine Datei.

Nur Python-Standardbibliothek, ab Python 3.10. Kein pip.

## Installation (Debian/Raspberry Pi OS)

Voraussetzungen: Server mit fester IP, `dnsmasq`, `iptables`, `ufw` (optional), SSH mit sudo.
Dem Radio im Router eine feste IP geben.

1. `deploy/haus.env.example` nach `deploy/haus.env` kopieren und anpassen.
2. Vom PC aus (Git Bash/Linux): `bash deploy/push.sh`. Das legt Benutzer `radioportal`, den
   Dienst `radio-portal`, die Umleitung von Port 80 (nur für die Radio-IPs), Firewall-Regeln
   und die dnsmasq-Konfiguration an. dnsmasq selbst wird **nicht** eingeschaltet. Das Skript darf
   beliebig oft laufen; dnsmasq startet es nur bei geänderter Konfiguration neu.
3. Prüfen, ohne Radio:
   ```
   curl -H "Host: aldi.wifiradiofrontier.com" "http://SERVER:8095/setupapp/aldi/asp/BrowseXML/loginXML.asp?token=0"
   ```
4. `sudo systemctl enable --now dnsmasq`, dann `nslookup aldi.wifiradiofrontier.com SERVER`
   (muss die Server-IP liefern) und `nslookup example.com SERVER` (die echte Adresse).
5. Am Radio manuelle Netzwerkeinstellungen: eigene IP, Gateway = Router (ohne Gateway laden
   Menüs, aber kein Stream), **DNS 1 und DNS 2 = Server**, Zeit-Update auf NET. Danach den
   Netzstecker ziehen: Das Radio merkt sich DNS-Antworten lange.

**Warum auch DNS 2?** Das IWR 294 fragt den zweiten DNS-Server regelmäßig, auch wenn der erste
antwortet. Steht dort der Router, läuft das Radio immer wieder still direkt über Airable (altes
Menü, eigene Sender und Podcasts fehlen, Einträge der FAV-Taste mit eigenen Nummern spielen nicht).
Der Preis: Ist der Server aus, gibt es kein Internetradio (UKW und AUX gehen weiter).

**Rückweg:** Am Radio DNS 1 und DNS 2 auf den Router stellen, dann `sudo systemctl disable --now dnsmasq`.

### Einstellungen (`deploy/haus.env`, landen in `/etc/radio-portal/radio-portal.env`)

| Name | Bedeutung |
|---|---|
| `PI`, `PI_IP`, `LAN`, `FRITZBOX` | SSH-Ziel, IP des Servers, Heimnetz, Router (für ufw und dnsmasq) |
| `RADIO_IPS` | IPs der Radios, durch Leerzeichen getrennt (Umleitung und Statusanzeige) |
| `PORT` | Port des Portals (Vorgabe 8095) |
| `UMLAUTE` | `umschreiben` (ae/oe/ue/ss, wie Airable; Vorgabe) oder `utf8` (Umlaute, ß, é bleiben; Typografie wie „ “ – wird ersetzt, Emoji und fremde Schriften fallen weg). Beim IWR 294 funktioniert `utf8` |
| `PORTAL_URL` | Adresse der Oberfläche, wie sie am Radio angezeigt wird |

Daten liegen in `/var/lib/radio-portal`: `sender.json` (alle Sender), `favoriten.json` (die
Senderliste am Radio), `podcasts.json`, `radio.json`. Eigene IDs (Sender ab 1000001, Podcasts ab
5000001) werden **nie neu vergeben**, denn die FAV-Liste des Radios merkt sie sich.

## Wenn das Radio plötzlich das alte Menü zeigt

Das Radio läuft dann still über Airable. Prüfen:

- Startseite der Oberfläche: Wann hat sich das Radio zuletzt gemeldet? Was steht unter
  *Letzte Anfragen des Radios*?
- Am Radio: *Einstellungen → Netzwerk → Einstellungen anzeigen*: IP, Gateway, DNS 1 **und** DNS 2.
  Hat das Radio die manuellen Einstellungen verloren (DHCP), steht dort der Router.
- Danach **Netzstecker ziehen**: Das Radio verwirft gemerkte DNS-Antworten nur beim Neustart.

## Entwicklung

`python -m unittest` (ohne Netz), lokal `python -m radioportal` (`HOST`, `PORT`, `DATA_DIR`).
Modulübersicht: `server.py` (Routing, Weiche, Relay), `radio.py` (Antworten für das Radio),
`airable.py` (Durchreichen von Unbekanntem), `stream.py` (Ketten, Relay), `probe.py`
(Stream-Prüfung), `radiobrowser.py`, `podcasts.py`, `library.py` (Sender und Senderliste),
`store.py`, `web.py`, `xmlitems.py`.

Die Weboberfläche hat keine Anmeldung und gehört **nicht** ins Internet. POST-Anfragen werden nur
mit passendem `Origin` angenommen.

Lizenz: MIT. Kein Code aus anderen Radio-Projekten (z. B. GPL-3.0) übernommen; Grundlage ist
beobachtetes Verhalten.

---

## English

A self-hosted portal for old Frontier Silicon internet radios (tested only with the Tevion IWR 294)
after Frontier shut down its Nuvola portal on 2024-10-31. The radios fetch menus over plain HTTP
from `<brand>.wifiradiofrontier.com` and verify nothing, so this service answers those requests
itself. Firmware stays untouched. Python standard library only (3.10+).

**Features:** the radio's *Internet Radio* menu shows your own station list (order set in the web UI)
followed by *Podcasts*; Airable's menus are gone, only unknown requests are still forwarded to
Airable's transition service. Every station has a permanent ID (from 1000001, never reused) that
always plays the same station, so you program the radio's FAV list once on the device; the web UI
can rename a station or repair its stream URL, and hiding a station keeps its FAV entry working.
Station search via radio-browser.info or by URL, both probed (MP3 / http / https / playlists /
Shoutcast `ICY 200 OK`); https-only streams are relayed as http. Own podcasts from RSS feeds with
unheard episodes marked `*` (the radio follows at most one redirect and cannot do https, so the server
resolves redirect chains and relays https sources). The start page shows when the radio last
contacted the portal and its recent requests (in memory only, without the radio's ID). One-file
data backup.

**Setup:** configure `deploy/haus.env` (see the table above), run `bash deploy/push.sh`, check on
port 8095, then enable dnsmasq and set the radio to a static network config with **both DNS 1 and
DNS 2 = server** and pull its power plug. The IWR 294 regularly queries the secondary DNS server even
when the first one answers; if that is the router, the radio silently bypasses the portal (old Airable
menu, own stations and podcasts missing). Rollback: point the radio's DNS back to the router, then
`sudo systemctl disable --now dnsmasq`. The web page has no login and is meant for the home network
only. Licence: MIT.
