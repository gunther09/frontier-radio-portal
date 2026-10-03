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
                                              ├── kann es selbst → eigene Antwort
                                              └── sonst → weiter an den Übergangsdienst von Airable
PC im Heimnetz ────> http://SERVER:8095  (Weboberfläche)
```

## Was es kann

- **Weiche:** Alles, was es nicht selbst kann, geht an Airable weiter. Fällt Airable aus oder
  wird abgeschaltet, zeigt das Radio nur noch die eigenen Menüs. Das Radio bleibt in jedem Fall nutzbar.
- **Favoriten:** Im Hauptmenü des Radios steht ganz oben *Favoriten*. Gepflegt werden sie am PC in
  der Weboberfläche (Reihenfolge, Suche bei radio-browser.info, Sender per Adresse mit Prüfung).
- **FAV-Liste und Stationstasten überleben Airable:** Die FAV-Taste (bei anderen Modellen auch die
  Stationstasten) speichert im Radio nur Airable-IDs. Für jede erfasste ID wählt man einen
  Ersatz-Sender; das Portal beantwortet den Eintrag dann selbst. Neue FAV-Einträge, die man am
  Radio aus unseren Menüs anlegt, tragen schon unsere IDs.
- **Eigene Podcasts** (RSS, Suche über iTunes): Menü *Eigene Podcasts* am Radio, ungehörte Folgen
  mit `*`. Podcast-Server leiten oft mehrfach weiter und nutzen https: das löst der Server auf und
  reicht die Folge bei Bedarf als http durch, denn das Radio folgt höchstens einer Weiterleitung und
  kann kein https.
- **Stream-Prüfung:** Erreichbar? MP3? http oder https? Playlists (`.m3u`, `.pls`) und
  Shoutcast-Antworten (`ICY 200 OK`) werden gelesen. Weiterleitungen löst der Server beim
  Abspielen auf (Sitzungskennungen in den Adressen verfallen sonst).
- **Statusanzeige:** Die Startseite zeigt, wann sich das Radio zuletzt gemeldet hat. Fragt es
  statt des Servers den Router, läuft es still über Airable weiter, das fällt hier auf.
- **Testmenü** (`TESTMENUE=ja`): Umlaute, AAC, nur-https, 302-Weiterleitung, langer Name. Beim IWR 294: Umlaute
  gehen, AAC nicht (nur MP3), https über den Server und 302 gehen.
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
   Menüs, aber kein Stream), **DNS 1 = Server, DNS 2 = Router** (fällt der Server aus, spielt
   das Radio direkt über Airable weiter), Zeit-Update auf NET. Radio kurz vom Strom trennen.

**Rückweg:** `sudo systemctl disable --now dnsmasq`.

### Einstellungen (`deploy/haus.env`, landen in `/etc/radio-portal/radio-portal.env`)

| Name | Bedeutung |
|---|---|
| `PI`, `PI_IP`, `LAN`, `FRITZBOX` | SSH-Ziel, IP des Servers, Heimnetz, Router (für ufw und dnsmasq) |
| `RADIO_IPS` | IPs der Radios, durch Leerzeichen getrennt (Umleitung und Statusanzeige) |
| `PORT` | Port des Portals (Vorgabe 8095) |
| `MITSCHNITT` | `ja`: jede Anfrage ins Journal und Antworten als Dateien (**privat**, enthält die Kennung des Radios). Nur zur Diagnose |
| `TESTMENUE` | `ja`: Eintrag *Test* unten im Hauptmenü |
| `UMLAUTE` | `umschreiben` (ae/oe/ue/ss, wie Airable; Vorgabe) oder `utf8` (Umlaute, ß, é bleiben; Typografie wie „ “ – wird ersetzt, Emoji und fremde Schriften fallen weg). Beim IWR 294 funktioniert `utf8` |
| `PORTAL_URL` | Adresse der Oberfläche, wie sie am Radio angezeigt wird |

Daten liegen in `/var/lib/radio-portal`: `sender.json`, `favoriten.json`, `airable.json` (erfasste
Tasten-IDs und Ersatz), `podcasts.json`, `radio.json`. Eigene IDs (Sender ab 1000001, Podcasts ab
5000001) werden **nie neu vergeben**, denn die FAV-Liste und die Tasten des Radios merken sie sich.

## Wenn das Radio plötzlich das alte Menü zeigt

Das Radio läuft dann still über Airable. Häufigste Ursache: Es hat seine Netzwerkeinstellungen
verloren (DHCP, DNS = Router) oder hält eine alte DNS-Antwort fest. Prüfen:

- Startseite der Oberfläche: Wann hat sich das Radio zuletzt gemeldet?
- Am Radio: *Einstellungen → Netzwerk*: IP, DNS 1 und DNS 2 prüfen.
- Danach **Netzstecker ziehen**: Das Radio merkt sich DNS-Antworten minutenlang und
  verwirft sie nur beim Neustart.

## Entwicklung

`python -m unittest` (ohne Netz), lokal `python -m radioportal` (`HOST`, `PORT`, `DATA_DIR`,
`MITSCHNITT`). Modulübersicht: `server.py` (Routing, Weiche), `radio.py` (Antworten für das Radio),
`airable.py` (Durchreichen, ID-Sammler, Mitschnitt), `stream.py` (Ketten, Relay), `probe.py`
(Stream-Prüfung), `radiobrowser.py`, `podcasts.py`, `library.py`, `store.py`, `web.py`, `xmlitems.py`.

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

**Features:** a switch that forwards everything it cannot answer to Airable's transition service
(the radio keeps working if Airable disappears); favourites managed in a web UI and shown at the top
of the radio's menu; replacement stations for the preset keys (the radio only stores Airable IDs);
own podcasts from RSS feeds with unheard episodes marked `*` (the radio follows at most one redirect
and cannot do https, so the server resolves redirect chains and relays https sources as http);
stream probing (MP3 / http / https / playlists / Shoutcast `ICY 200 OK`); a status line showing when
the radio last contacted the portal; a test menu; a one-file data backup.

**Setup:** configure `deploy/haus.env` (see the table above), run `bash deploy/push.sh`, check on
port 8095, then enable dnsmasq and set the radio to a static network config with DNS 1 = server,
DNS 2 = router (the radio falls back to Airable directly if the server is down). Rollback:
`sudo systemctl disable --now dnsmasq`. If the radio suddenly shows the old Airable menu it has
probably lost its network settings (DHCP) or holds a stale DNS answer: check its network settings
and pull the power plug. The web page has no login and is meant for the home network only.
Licence: MIT.
