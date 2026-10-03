#!/usr/bin/env bash
# Radio-Portal auf dem Server einrichten.
# Aufruf (auf dem Server):  sudo bash deploy/deploy.sh <quellverzeichnis>
# Normalerweise ueber deploy/push.sh vom PC aus. Idempotent: darf beliebig oft laufen.
# dnsmasq wird NICHT eingeschaltet - das ist ein bewusster Schritt (README, "Inbetriebnahme").
set -euo pipefail

SRC="${1:-/tmp/radio-portal-src}"
APP=/opt/radio-portal/app
ETC=/etc/radio-portal
STATE=/var/lib/radio-portal

[ -d "$SRC/radioportal" ] && [ -f "$SRC/deploy/haus.env" ] || { echo "Quellverzeichnis $SRC unvollstaendig"; exit 1; }
# shellcheck disable=SC1091
. "$SRC/deploy/haus.env"
: "${PI_IP:?}" "${LAN:?}" "${FRITZBOX:?}" "${RADIO_IPS:?}" "${PORT:?}"

# --- Benutzer + Verzeichnisse -------------------------------------------------
id radioportal &>/dev/null || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin radioportal
install -d -m 755 /opt/radio-portal "$APP" "$ETC"
install -d -o radioportal -g radioportal -m 750 "$STATE"

# --- Code (nur Standardbibliothek, kein venv) ---------------------------------
rm -rf "$APP/radioportal"
cp -r "$SRC/radioportal" "$APP/radioportal"
find "$APP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
chmod -R u=rwX,go=rX "$APP"
python3 -m compileall -q "$APP/radioportal"

# --- Konfiguration ------------------------------------------------------------
cat > "$ETC/radio-portal.env" <<EOF
# Erzeugt von deploy.sh aus deploy/haus.env - nicht von Hand aendern.
PORT=$PORT
PI_IP=$PI_IP
RADIO_IPS="$RADIO_IPS"
MITSCHNITT=${MITSCHNITT:-nein}
TESTMENUE=${TESTMENUE:-nein}
UMLAUTE=${UMLAUTE:-umschreiben}
PORTAL_URL=${PORTAL_URL:-}
EOF
chmod 644 "$ETC/radio-portal.env"

# --- Firewall (ufw ist default-deny): Portal und DNS nur fuers Heimnetz --------
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    LAN_RE="${LAN//./\\.}"
    ufw status | grep -Eq "^${PORT}/tcp\s.*${LAN_RE}" || \
        ufw allow from "$LAN" to any port "$PORT" proto tcp comment 'Radio-Portal (LAN-only)'
    ufw status | grep -Eq "^53\s.*${LAN_RE}" || \
        ufw allow from "$LAN" to any port 53 comment 'Radio-Portal DNS (LAN-only)'
fi

# --- systemd ------------------------------------------------------------------
install -m 755 "$SRC/deploy/radio-portal-umleitung" /usr/local/sbin/radio-portal-umleitung
install -m 644 "$SRC/deploy/radio-portal.service" "$SRC/deploy/radio-portal-umleitung.service" \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now radio-portal.service radio-portal-umleitung.service
systemctl restart radio-portal.service
systemctl restart radio-portal-umleitung.service   # wendet geaenderte RADIO_IPS an

# --- dnsmasq-Konfiguration (nur erzeugen und pruefen) --------------------------
if command -v dnsmasq &>/dev/null; then
    NEU=$(mktemp)
    sed -e "s/@PI_IP@/$PI_IP/g" -e "s/@FRITZBOX@/$FRITZBOX/g"         "$SRC/deploy/dnsmasq-radio-portal.conf.in" > "$NEU"
    dnsmasq --test --conf-file="$NEU"
    # Nur bei geaenderter Konfiguration neu starten: ein Neustart leert den Cache und stoert
    # das Radio unnoetig. Das erste Einschalten ist ein bewusster Schritt (README).
    if ! cmp -s "$NEU" /etc/dnsmasq.d/radio-portal.conf; then
        install -m 644 "$NEU" /etc/dnsmasq.d/radio-portal.conf
        if systemctl is-active --quiet dnsmasq; then systemctl restart dnsmasq; echo "dnsmasq: Konfiguration geaendert, neu gestartet"; fi
    fi
    rm -f "$NEU"
else
    echo "WARNUNG: dnsmasq ist nicht installiert (sudo apt install dnsmasq), DNS-Umleitung fehlt"
fi

# --- Status -------------------------------------------------------------------
sleep 1
echo
echo "radio-portal:   $(systemctl is-active radio-portal.service)"
echo "umleitung:      $(systemctl is-active radio-portal-umleitung.service)"
echo "dnsmasq:        $(systemctl is-active dnsmasq 2>/dev/null || true) (Autostart: $(systemctl is-enabled dnsmasq 2>/dev/null || true))"
echo "Oberflaeche:    http://$(hostname):$PORT/"
