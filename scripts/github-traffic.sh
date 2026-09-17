#!/usr/bin/env bash
# Archiviert GitHub Traffic-Daten (Clones + Views) in CSV-Dateien.
# GitHub liefert nur die letzten 14 Tage — dieses Script sichert sie vor dem Verfall.
#
# Nutzung:
#   ./scripts/github-traffic.sh                    # Alle 3 Repos
#   ./scripts/github-traffic.sh supernova1963/eedc # Einzelnes Repo
#
# Cronjob (täglich um 6:00):
#   0 6 * * * /home/gernot/claude/eedc-homeassistant/scripts/github-traffic.sh

set -euo pipefail

REPOS=("supernova1963/eedc-homeassistant" "supernova1963/eedc" "supernova1963/eedc-community")
DATA_DIR="${HOME}/claude/github-traffic"

# Einzelnes Repo als Argument?
if [[ $# -gt 0 ]]; then
  REPOS=("$1")
fi

mkdir -p "$DATA_DIR"

for repo in "${REPOS[@]}"; do
  repo_name="${repo##*/}"
  csv_clones="${DATA_DIR}/${repo_name}-clones.csv"
  csv_views="${DATA_DIR}/${repo_name}-views.csv"

  # Header anlegen falls neu
  [[ -f "$csv_clones" ]] || echo "date,unique,total" > "$csv_clones"
  [[ -f "$csv_views" ]]  || echo "date,unique,total" > "$csv_views"

  # Clones
  gh api "repos/${repo}/traffic/clones" --jq '.clones[] | "\(.timestamp[:10]),\(.uniques),\(.count)"' 2>/dev/null | while IFS= read -r line; do
    date_val="${line%%,*}"
    grep -q "^${date_val}," "$csv_clones" 2>/dev/null || echo "$line" >> "$csv_clones"
  done

  # Views
  gh api "repos/${repo}/traffic/views" --jq '.views[] | "\(.timestamp[:10]),\(.uniques),\(.count)"' 2>/dev/null | while IFS= read -r line; do
    date_val="${line%%,*}"
    grep -q "^${date_val}," "$csv_views" 2>/dev/null || echo "$line" >> "$csv_views"
  done

  echo "[$(date +%Y-%m-%d)] ${repo_name}: OK"
done

# ---------------------------------------------------------------------------
# HA-Analytics: wie viele (opt-in) Home-Assistant-Installationen unser Add-on
# melden, je Version. Quelle: https://analytics.home-assistant.io/addons.json —
# nur der aktuelle Stand, KEIN Verlauf (gemessen 17.09.2026: `data.json`
# fuehrt Add-ons nicht in `history`). Deshalb hier taeglich wegschreiben.
#
# Slugs: `bc122c22_eedc` = sha1 der Repo-URL (klein), `3ffcb5f8_eedc` = dieselbe
# URL mit `.git` — beide sind wir. Zaehlt nur Installationen mit eingeschalteter
# Nutzungs-Statistik (am 17.09.2026: 413.940 von 683.700 aktiven Installationen
# melden Add-ons, rund 60 %); der Rest ist unsichtbar.
# ---------------------------------------------------------------------------
csv_ha="${DATA_DIR}/ha-analytics-eedc.csv"
[[ -f "$csv_ha" ]] || echo "date,slug,total,versions" > "$csv_ha"
heute="$(date +%Y-%m-%d)"
if grep -q "^${heute}," "$csv_ha" 2>/dev/null; then
  echo "[${heute}] ha-analytics: schon erfasst"
else
  curl -fsS --max-time 30 "https://analytics.home-assistant.io/addons.json" \
    | python3 -c '
import json, sys
daten = json.load(sys.stdin)
heute = sys.argv[1]
for slug, eintrag in sorted(daten.items()):
    if not slug.endswith("_eedc"):
        continue
    versionen = ";".join(f"{v}={n}" for v, n in sorted(eintrag["versions"].items(), key=lambda kv: -kv[1]))
    total = eintrag["total"]
    print(f"{heute},{slug},{total},{versionen}")
' "$heute" >> "$csv_ha" \
    && echo "[${heute}] ha-analytics: OK" \
    || echo "[${heute}] ha-analytics: FEHLER (Abruf oder Auswertung)" >&2
fi
