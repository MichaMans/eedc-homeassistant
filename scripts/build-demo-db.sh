#!/usr/bin/env bash
#
# build-demo-db.sh — Kanonische Demo-DB für Dev-/Guest-Box aus EINER Quelle bauen.
#
# Hintergrund: Die Demo-DB entsteht aus dem Master (eedc/data/eedc.db) PLUS zwei
# Seeds, die bisher nur verstreut/manuell liefen → die Guest-Box driftete weg
# (Tag-/Aussicht-Seeds fehlten). Dieses Script bündelt den kompletten Aufbau
# idempotent an einer Stelle, damit Dev- und Guest-Box reproduzierbar identisch
# befüllt sind.
#
# Pipeline:
#   1) Master kopieren  (eedc/data/eedc.db — enthält bereits die P1-Komponenten
#      VW ID.3 + Heizstab, Invest 11/12).
#   2) Aussicht-Historie: TEP 2025-10-15..2025-12-30 um +175 Tage geschoben
#      (→ 2026-04-08..2026-06-23) für die Stunden-Prognose/Tag-Sichten in 2026.
#   3) Tag-Reseed (scripts/reseed-v4-tag-demo.py): TZ pv_prognose/SOLL,
#      sensor_mapping WP4/WB5, kumulative sensor_snapshots (WP-Wärme/JAZ, E-Mob).
#   4) Optional: leere Test-Anlage „Ferienhaus Süd" (Sammel-Screen/Leerzustand)
#      aus einer vorhandenen DB übernehmen.
#   4e) Prüfstand Wärme/Klima (WK-15): Split-Klimaanlage in der Demo-Anlage +
#      zweite Anlage mit den Handbuch-Lagen G/D/F. Idempotent, eigener Seeder.
#   5) Laufzeit-Cruft (api_cache, Streu-Zeilen jenseits der Seed-Daten) leeren.
#
# Aufruf:
#   scripts/build-demo-db.sh OUTPUT.db [--extra-anlage-from DB.db]
#   scripts/build-demo-db.sh OUTPUT.db --basis eedc/data/devbox-r27-demo.db
#
# ⛔ **--basis ist kein Komfort, sondern der Weg, der heute funktioniert.**
# Gemessen am 14.09.2026: Der Master `eedc/data/eedc.db` trägt **0 Anlagen,
# 0 Investitionen, 0 Monatsdaten** (nur `activity_log` und `migrations` sind
# gefüllt) — die Dev-Box-DB ist irgendwann geleert worden. Schritt 3 bricht
# darauf mit `IndexError` ab, weil `reseed-v4-tag-demo.py` keine TEP-Tage
# findet. Solange das so ist, entsteht die nächste Demo-DB als **Kopie der
# letzten plus Seed**: `--basis` nimmt dann die Vorgänger-DB statt des Masters
# und überspringt die Schritte 2–4d, die dort schon gelaufen sind.
# Default OUTPUT: ./scratch-demo.db
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MASTER="$REPO/eedc/data/eedc.db"

OUT="${1:-$REPO/scratch-demo.db}"
EXTRA_ANLAGE_DB=""
BASIS=""
shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --extra-anlage-from) EXTRA_ANLAGE_DB="${2:?--extra-anlage-from braucht eine DB}"; shift 2;;
    --basis)             BASIS="${2:?--basis braucht eine DB}"; shift 2;;
    *) echo "Unbekannte Option: $1"; exit 2;;
  esac
done

QUELLE="${BASIS:-$MASTER}"
[ -f "$QUELLE" ] || { echo "FEHLER: Quell-DB fehlt: $QUELLE"; exit 1; }

# Letzter geseedeter Tag = Obergrenze; alles darüber ist Laufzeit-Cruft.
SEED_BIS="2026-06-23"

echo "==> [1/7] Quelle ($QUELLE) → $OUT"
cp "$QUELLE" "$OUT"
rm -f "$OUT-wal" "$OUT-shm"

if [ -n "$BASIS" ]; then
  echo "==> [2..4d/7] uebersprungen (--basis: die Vorgaenger-DB traegt sie schon)"
else
echo "==> [2/7] Aussicht-Historie: TEP +175 Tage (2025-10-15..12-30 → 2026)"
TEP_COLS="anlage_id,datum,stunde,pv_kw,verbrauch_kw,einspeisung_kw,netzbezug_kw,batterie_kw,waermepumpe_kw,wallbox_kw,wp_starts_anzahl,ueberschuss_kw,defizit_kw,temperatur_c,globalstrahlung_wm2,bewoelkung_prozent,niederschlag_mm,wetter_code,soc_prozent,strompreis_cent,boersenpreis_cent,komponenten,source_provenance,created_at,wp_betriebsstunden"
TEP_SEL="anlage_id,date(datum,'+175 days'),stunde,pv_kw,verbrauch_kw,einspeisung_kw,netzbezug_kw,batterie_kw,waermepumpe_kw,wallbox_kw,wp_starts_anzahl,ueberschuss_kw,defizit_kw,temperatur_c,globalstrahlung_wm2,bewoelkung_prozent,niederschlag_mm,wetter_code,soc_prozent,strompreis_cent,boersenpreis_cent,komponenten,source_provenance,created_at,wp_betriebsstunden"
sqlite3 "$OUT" "
  DELETE FROM tages_energie_profil
   WHERE datum BETWEEN date('2025-10-15','+175 days') AND date('2025-12-30','+175 days');
  INSERT INTO tages_energie_profil ($TEP_COLS)
  SELECT $TEP_SEL FROM tages_energie_profil
   WHERE datum BETWEEN '2025-10-15' AND '2025-12-30';
"

echo "==> [3/6] Tag-Reseed (TZ-Prognose, sensor_mapping, snapshots)"
# Master hat sensor_mapping = NULL; der Reseed erwartet gültiges JSON.
sqlite3 "$OUT" "UPDATE anlagen SET sensor_mapping='{}' WHERE sensor_mapping IS NULL OR sensor_mapping='';"
EEDC_RESEED_DB="$OUT" python3 "$SCRIPT_DIR/reseed-v4-tag-demo.py"

echo "==> [4/6] Sommer-Reseed Apr–Jun 2026 (plausible PV/Einspeisung/Temp + PR-Aggregate)"
# Korrigiert die saisonale Rückwärts-Abbildung des +175-Seeds (Dezember auf Juni)
# und füllt die TZ-Aggregate (PR/Spitzen/Temp), die der Tag-Reseed nicht erzeugt.
EEDC_RESEED_DB="$OUT" python3 "$SCRIPT_DIR/seed-v4-sommer-2026.py"

echo "==> [4b] Monats-Aggregate 2026 (Apr–Jun) additiv aus 2025-H1"
# R5-1/2/3 (Rainer simon42): monatsdaten/IMD enden im Master 2025-12, während die
# TEP/TZ-Tagesdaten via +175-Shift bis 2026-06-23 reichen → Cockpit/Monat sprang auf
# „Dez 2025", Cockpit/Jahr kannte 2026 gar nicht (Monat/Jahr wählen „neuester mit
# Daten"). Wir spiegeln genau die DREI Monate MIT 2026-Tagesdaten (Apr/Mai/Jun) als
# Monats-Aggregate aus 2025-H1 (reale, plausible Werte; Juni ≈ Sommer-Seed-Einspeisung)
# → Monat-Default = Jun 2026, Jahr kennt 2026 als Teiljahr. Additiv (Master unberührt),
# nur Monate mit Tagesdaten (keine leeren Aggregat-Geister). pv_erzeugung_kwh ist
# bewusst Legacy/NULL (PV kommt aus IMD der PV-Module).
sqlite3 "$OUT" "
  INSERT INTO monatsdaten (anlage_id,jahr,monat,einspeisung_kwh,netzbezug_kwh,pv_erzeugung_kwh,direktverbrauch_kwh,eigenverbrauch_kwh,gesamtverbrauch_kwh,batterie_ladung_kwh,batterie_entladung_kwh,batterie_ladung_netz_kwh,batterie_ladepreis_cent,netzbezug_durchschnittspreis_cent,kraftstoffpreis_euro,gaspreis_cent_kwh,globalstrahlung_kwh_m2,sonnenstunden,durchschnittstemperatur,ueberschuss_kwh,defizit_kwh,batterie_vollzyklen,performance_ratio,peak_netzbezug_kw,sonderkosten_euro,sonderkosten_beschreibung,datenquelle,notizen,source_provenance,source_hash,created_at,updated_at)
  SELECT anlage_id,2026,monat,einspeisung_kwh,netzbezug_kwh,pv_erzeugung_kwh,direktverbrauch_kwh,eigenverbrauch_kwh,gesamtverbrauch_kwh,batterie_ladung_kwh,batterie_entladung_kwh,batterie_ladung_netz_kwh,batterie_ladepreis_cent,netzbezug_durchschnittspreis_cent,kraftstoffpreis_euro,gaspreis_cent_kwh,globalstrahlung_kwh_m2,sonnenstunden,durchschnittstemperatur,ueberschuss_kwh,defizit_kwh,batterie_vollzyklen,performance_ratio,peak_netzbezug_kw,sonderkosten_euro,sonderkosten_beschreibung,datenquelle,notizen,source_provenance,source_hash,created_at,updated_at
  FROM monatsdaten WHERE jahr=2025 AND monat IN (4,5,6);
  INSERT INTO investition_monatsdaten (investition_id,jahr,monat,verbrauch_daten,einsparung_monat_euro,co2_einsparung_kg,source_provenance,source_hash,created_at,updated_at)
  SELECT investition_id,2026,monat,verbrauch_daten,einsparung_monat_euro,co2_einsparung_kg,source_provenance,source_hash,created_at,updated_at
  FROM investition_monatsdaten WHERE jahr=2025 AND monat IN (4,5,6);
"

echo "==> [4c] Heizstab-Stunden (Sonstiges-Verbraucher) auf BHKW-Tagen (Tag-Demo-Parität)"
# Der Heizstab (sonstige_12) fehlte in der Stunden-Demo (TEP komponenten) → Cockpit/
# Tag konnte den Sonstiges-Verbraucher nie zeigen, obwohl der Monat ihn hat. Wir
# setzen ein plausibles Morgen-/Abend-Profil (Senke = NEGATIV) auf genau den Tagen,
# die schon einen Mini-BHKW (sonstige_10) tragen → BEIDE Sonstiges-Blöcke auf
# denselben Tagen demonstrierbar (z. B. 2026-06-23). Klassifikation als Verbraucher
# erfolgt automatisch (inv.typ=sonstiges + parameter.kategorie=verbraucher → Senke).
sqlite3 "$OUT" "
  UPDATE tages_energie_profil
     SET komponenten = json_set(komponenten, '\$.sonstige_12', -0.6)
   WHERE datum IN (SELECT DISTINCT datum FROM tages_energie_profil WHERE komponenten LIKE '%sonstige_10%')
     AND stunde IN (6, 7, 19, 20, 21);
"

echo "==> [4d] Infothek-Demo (Einstellungen-V4: Infothek-Kachel + N:M-Verknüpfung)"
# Der Master hat keine Infothek-Einträge → die Einstellungen-V4-Infothek-Kachel
# zeigte in der Demo nur den Leerzustand. Drei plausible Einträge (Stromvertrag,
# Versicherung, Wartungsvertrag mit WP-Verknüpfung) demonstrieren die Kurz-Liste
# + die Investitions-Verknüpfung. Kategorien = SoT-Keys (config/infothekKategorien).
# Idempotent (OUT ist frische Master-Kopie; Guard schützt zusätzlich).
sqlite3 "$OUT" "
  INSERT INTO infothek_eintraege (anlage_id,bezeichnung,kategorie,notizen,investition_id,sortierung,aktiv,in_anlagendoku,created_at,updated_at)
  SELECT 1,'Stromtarif Ökostrom 2026','stromvertrag','Grundpreis 12,90 €/Monat · Arbeitspreis 31,5 ct/kWh',NULL,0,1,1,datetime('now'),datetime('now')
  WHERE NOT EXISTS (SELECT 1 FROM infothek_eintraege WHERE anlage_id=1 AND bezeichnung='Stromtarif Ökostrom 2026');
  INSERT INTO infothek_eintraege (anlage_id,bezeichnung,kategorie,notizen,investition_id,sortierung,aktiv,in_anlagendoku,created_at,updated_at)
  SELECT 1,'PV-Versicherung Police 2023','versicherung','Allgefahrendeckung · Selbstbehalt 150 €',NULL,1,1,1,datetime('now'),datetime('now')
  WHERE NOT EXISTS (SELECT 1 FROM infothek_eintraege WHERE anlage_id=1 AND bezeichnung='PV-Versicherung Police 2023');
  INSERT INTO infothek_eintraege (anlage_id,bezeichnung,kategorie,notizen,investition_id,sortierung,aktiv,in_anlagendoku,created_at,updated_at)
  SELECT 1,'Wartungsvertrag Wärmepumpe','wartungsvertrag','Jährliche Wartung · nächster Termin Herbst 2026',
    (SELECT id FROM investitionen WHERE anlage_id=1 AND typ='waermepumpe' LIMIT 1),2,1,1,datetime('now'),datetime('now')
  WHERE NOT EXISTS (SELECT 1 FROM infothek_eintraege WHERE anlage_id=1 AND bezeichnung='Wartungsvertrag Wärmepumpe');
"

fi

if [ -n "$EXTRA_ANLAGE_DB" ]; then
  echo "==> [5/7] Leere Test-Anlage Ferienhaus Sued (Sammel-Screen) aus $EXTRA_ANLAGE_DB uebernehmen"
  sqlite3 "$OUT" "ATTACH '$EXTRA_ANLAGE_DB' AS src;
    INSERT OR IGNORE INTO anlagen SELECT * FROM src.anlagen WHERE id=2;
    DETACH src;"
else
  echo "==> [5/7] (keine Extra-Anlage)"
fi

echo "==> [6/7] Laufzeit-Cruft leeren (api_cache + Streu-Zeilen > $SEED_BIS)"
sqlite3 "$OUT" "
  DELETE FROM api_cache;
  DELETE FROM tages_zusammenfassung WHERE datum > '$SEED_BIS';
  DELETE FROM tages_energie_profil  WHERE datum > '$SEED_BIS';
"
# ⛔ **Der Pruefstand-Seed laeuft NACH dem Aufraeumen, nicht davor.** Schritt 6
# loescht alle Tageszeilen jenseits von $SEED_BIS — die Pruefstand-Anlage
# reicht mit ihrem Tagesfenster aber bis kurz vor „heute", damit der LAUFENDE
# Monat aus den Tages-Snapshots entsteht. Vor dem Aufraeumen geseedet waere
# genau dieser Teil wieder weg.
echo "==> [7/7] Pruefstand Waerme/Klima (WK-15)"
python3 "$SCRIPT_DIR/seed-pruefstand-waerme-klima.py" --db "$OUT"

sqlite3 "$OUT" "VACUUM;"
rm -f "$OUT-wal" "$OUT-shm"

n_anlagen=$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM anlagen;')
n_invest=$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM investitionen;')
n_tep=$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM tages_energie_profil;')
n_tz=$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM tages_zusammenfassung;')
n_snap=$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM sensor_snapshots;')
echo "==> Fertig: $OUT"
echo "    anlagen=$n_anlagen invest=$n_invest TEP=$n_tep TZ=$n_tz snapshots=$n_snap"
