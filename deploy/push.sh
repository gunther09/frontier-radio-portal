#!/usr/bin/env bash
# Vom PC (Git Bash) aus: Code und deploy/ auf den Server bringen und dort deploy.sh ausfuehren.
#   bash deploy/push.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f deploy/haus.env ] || { echo "deploy/haus.env fehlt (Vorlage: deploy/haus.env.example)"; exit 1; }
# shellcheck disable=SC1091
. deploy/haus.env
TMP=/tmp/radio-portal-src

# haus.env liegt danach kurz in /tmp (Modus 700) - deploy.sh kopiert sie weg, danach loeschen.
tar czf - --exclude=__pycache__ radioportal deploy \
    | ssh "$PI" "umask 077 && rm -rf $TMP && mkdir -m 700 $TMP && tar xzf - -C $TMP"
ssh "$PI" "sudo bash $TMP/deploy/deploy.sh $TMP; rc=\$?; rm -rf $TMP; exit \$rc"
