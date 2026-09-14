#!/usr/bin/env python3
"""MQTT-Feeder für die nachgestellten Wärme/Klima-Anlagen (WK-15).

Er speist den **laufenden** Monat: kumulative Zählerstände und Live-Leistung
der Prüfstand-Geräte auf die eedc-Inbound-Topics, alle fünf Minuten.

```bash
# Topic-Liste per SSH holen, Broker direkt (der Weg, der von der Dev-Box geht)
scripts/pruefstand-mqtt-feeder.py --broker 10.100.1.167 --user eedc \\
    --password '…' --ssh root@10.100.1.167 --anlage 2

# oder auf dem Lab selbst, mit erreichbarer API
scripts/pruefstand-mqtt-feeder.py --broker core-mosquitto --user eedc \\
    --password '…' --api http://local-eedc:8099 --anlage 2

scripts/pruefstand-mqtt-feeder.py … --einmal     # ein Durchlauf, dann Schluss
```

## Woher die Zahlen kommen

Aus **demselben** Datenmodul wie der Seed (`pruefstand_waerme_klima/daten.py`).
Der Stand eines Zählers ist der Startstand plus alles, was das Gerät bis jetzt
verbraucht hat — Monat für Monat, Tag für Tag, Stunde für Stunde. Damit ist er
**monoton** (die Voraussetzung jedes Zählerpfads, s. `snapshot/reader.delta`)
und passt zu den gespeicherten Monaten.

## Was er NICHT kann — und warum (gemessen 14.09.2026)

* **Den Betriebsmodus.** Die Erwartungsliste führt Zustandsfelder ausdrücklich
  mit leerem `topic` (`mqtt_topic_registry.py`, #263 K-2): Der Inbound-Parser
  ist `float(payload)`, ein Modus-String käme dort nie an. Der Feeder meldet
  das und überspringt sie — ein erfundenes Topic wäre eine Lücke, die niemand
  schließen kann.
* **Den laufenden Monat rückwirkend füllen.** `mqtt_monats_deltas` braucht
  einen Stand am **Monatsersten** (`snapshot/reader.get_snapshot`, ±5 min).
  Ein Feeder, der mitten im Monat startet, liefert den rechten Rand — der
  linke fehlt, und die Monatsmenge bleibt (zu Recht) ohne Aussage. Sichtbar
  wird er sofort im **Live**-Bild und ab dem Folgetag in *Cockpit → Tag*.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pruefstand_waerme_klima import daten as D  # noqa: E402


# ── Bestandsaufnahme über API oder SSH ───────────────────────────────────────

def _hole(pfad: str, api: Optional[str], ssh: Optional[str],
          timeout: float = 30.0) -> Any:
    """Eine GET-Abfrage — direkt über HTTP oder per `ssh … curl`.

    **Warum zwei Wege:** Der Lab-API-Port ist von der Dev-Box nicht erreichbar
    (gemessen), der Mosquitto-Port dagegen schon. Der Feeder läuft deshalb auf
    der Dev-Box (dort liegt `paho-mqtt`), holt seine Bestandsaufnahme aber über
    dieselbe SSH-Verbindung, über die auch der Seeder läuft.
    """
    if api:
        with urllib.request.urlopen(f"{api.rstrip('/')}{pfad}", timeout=timeout) as a:
            return json.loads(a.read().decode())
    if ssh:
        # ⚠ **Die URL in Anführungszeichen.** Die Login-Shell des HAOS-SSH-
        # Add-ons ist `zsh`, und die bricht bei einem unquotierten `?` mit
        # „no matches found" ab, bevor `curl` überhaupt startet — gemessen am
        # 14.09.2026 an genau dieser Zeile.
        roh = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh,
             f"curl -s -m 30 'http://local-eedc:8099{pfad}'"],
            capture_output=True, text=True, timeout=timeout + 30,
        )
        if roh.returncode != 0:
            raise SystemExit(f"ssh/curl fehlgeschlagen: {roh.stderr[:300]}")
        return json.loads(roh.stdout)
    raise SystemExit("weder --api noch --ssh angegeben")


def topic_erwartung(anlage_id: int, api, ssh) -> list[dict]:
    antwort = _hole(f"/api/live/mqtt/topics?anlage_id={anlage_id}", api, ssh)
    return antwort.get("topics", antwort) if isinstance(antwort, dict) else antwort


def pruefstand_geraete(anlage_id: int, api, ssh) -> list[tuple[int, D.Geraet]]:
    """Die Investitions-IDs der Seed-Geräte dieser Anlage — an ihrer Marke erkannt."""
    invs = _hole(f"/api/investitionen/?anlage_id={anlage_id}", api, ssh) or []
    nach_bezeichnung = {g.bezeichnung: g for g in (*D.GERAETE, D.PRUEFSTAND_PV)}
    aus: list[tuple[int, D.Geraet]] = []
    for inv in invs:
        param = inv.get("parameter") or {}
        if param.get("pruefstand") != D.PRUEFSTAND_MARKE:
            continue
        geraet = nach_bezeichnung.get(inv.get("bezeichnung"))
        if geraet:
            aus.append((int(inv["id"]), geraet))
    return aus


# ── Zählerstände ─────────────────────────────────────────────────────────────

def _monatsstand(geraet: D.Geraet, feld: str, bis_monat: tuple[int, int]) -> float:
    """Σ aller Monatswerte **vor** ``bis_monat``."""
    summe = 0.0
    for (jahr, monat), zeile in D.monatswerte(geraet).items():
        if (jahr, monat) >= bis_monat:
            continue
        summe += zeile.get(feld, 0.0)
    return summe


def zaehlerstand(geraet: D.Geraet, feld: str, jetzt: datetime) -> float:
    """Der kumulative Stand eines Zählers zum Zeitpunkt *jetzt*.

    Startstand + alle abgeschlossenen Monate + die Tage dieses Monats + der
    Anteil des heutigen Tages bis zur aktuellen Minute. **Monoton wachsend** —
    jeder Rücksprung machte den Tages- und Monatswert zur Nicht-Aussage
    (`snapshot/reader.delta`).
    """
    heute = jetzt.date()
    stand = D.ZAEHLER_STARTSTAND.get(feld, 0.0)
    stand += _monatsstand(geraet, feld, (heute.year, heute.month))
    monate = D.monatswerte(geraet)
    for tag_nr in range(1, heute.day):
        bild = D.tagesbild(geraet, date(heute.year, heute.month, tag_nr), monate)
        stand += bild.tag.get(feld, 0.0)
    bild = D.tagesbild(geraet, heute, monate)
    stunden = bild.stunden.get(feld)
    if stunden:
        stand += sum(stunden[: jetzt.hour])
        stand += stunden[jetzt.hour] * (jetzt.minute / 60.0)
    return round(stand, 3)


def leistung_w(geraet: D.Geraet, jetzt: datetime) -> float:
    """Die Momentanleistung — die Energie dieser Stunde, in Watt gelesen."""
    bild = D.tagesbild(geraet, jetzt.date())
    kwh = sum(w[jetzt.hour] for w in bild.stunden.values()
              if len(w) > jetzt.hour) if bild.stunden else 0.0
    strom = bild.strom_je_stunde
    kwh = strom[jetzt.hour] if strom else kwh
    return round(kwh * 1000.0, 1)


# ── MQTT ─────────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    import re
    slug = name.strip().replace(" ", "_")
    slug = re.sub(r"[^\w.\-]", "", slug)
    return slug or "unnamed"


class Feeder:
    def __init__(self, args):
        self.args = args
        self.api = args.api
        self.ssh = args.ssh
        self.anlage_id = args.anlage
        anlagen = _hole("/api/anlagen/", self.api, self.ssh) or []
        treffer = next((a for a in anlagen if int(a["id"]) == self.anlage_id), None)
        if not treffer:
            raise SystemExit(f"Anlage {self.anlage_id} nicht gefunden")
        self.anlage_name = treffer["anlagenname"]
        self.praefix = f"eedc/{self.anlage_id}_{_slug(self.anlage_name)}"
        self.geraete = pruefstand_geraete(self.anlage_id, self.api, self.ssh)
        if not self.geraete:
            raise SystemExit(
                f"Keine Geräte mit parameter.pruefstand={D.PRUEFSTAND_MARKE!r} "
                f"in Anlage {self.anlage_id} — erst seeden.")
        erwartung = topic_erwartung(self.anlage_id, self.api, self.ssh)
        self.erwartet = {t.get("topic") for t in erwartung if t.get("topic")}
        # ⛔ **Der Endpunkt liefert nur `topic`/`label`/`beschreibung`/`anlage`/
        # `typ`** — gemessen am 14.09.2026 am Lab. Die reichen Schlüssel aus
        # `build_expected_topics` (`feld`, `kategorie`, `zustand`, `bedarf` …)
        # kommen dort **nicht** an. Ein Zustandsfeld lässt sich aus der Antwort
        # also gar nicht erkennen; wer es versucht, bekommt stumm eine leere
        # Liste. Die Aussage steht deshalb an den Geräten selbst.
        self.geraete_mit_modus = [
            g.bezeichnung for _i, g in self.geraete if g.modus_signal
        ]

    # — Nachrichtenliste ————————————————————————————————————————————

    def nachrichten(self, jetzt: datetime) -> list[tuple[str, str]]:
        aus: list[tuple[str, str]] = []
        for inv_id, geraet in self.geraete:
            basis = f"{self.praefix}/energy/inv/{inv_id}_{_slug(geraet.bezeichnung)}"
            live = f"{self.praefix}/live/inv/{inv_id}_{_slug(geraet.bezeichnung)}"
            for feld in geraet.zaehler:
                aus.append((f"{basis}/{feld}",
                            f"{zaehlerstand(geraet, feld, jetzt):.3f}"))
            if geraet.typ == "waermepumpe":
                aus.append((f"{live}/leistung_w", f"{leistung_w(geraet, jetzt):.1f}"))
            elif geraet.typ == "pv-module":
                bild = D.tagesbild(geraet, jetzt.date())
                stunden = bild.stunden.get("pv_erzeugung_kwh") or [0.0] * 24
                aus.append((f"{live}/leistung_w",
                            f"{stunden[jetzt.hour] * 1000.0:.1f}"))
        aus.extend(self._basis_nachrichten(jetzt))
        return aus

    def _basis_nachrichten(self, jetzt: datetime) -> list[tuple[str, str]]:
        """Netzbezug und Einspeisung der Anlage — ohne sie ist das Live-Bild leer.

        Sie werden aus denselben Geräteprofilen gebildet: Erzeugung minus
        Verbrauch je Stunde, aufsummiert zu einem kumulativen Stand.
        """
        pv = next((g for _i, g in self.geraete if g.typ == "pv-module"), None)
        if pv is None:
            return []
        heute = jetzt.date()
        erz_h = (D.tagesbild(pv, heute).stunden.get("pv_erzeugung_kwh")
                 or [0.0] * 24)
        verbrauch_h = [0.0] * 24
        for _inv_id, geraet in self.geraete:
            if geraet.typ != "waermepumpe":
                continue
            for h, wert in enumerate(D.tagesbild(geraet, heute).strom_je_stunde):
                verbrauch_h[h] += wert
        haushalt = D.verteile_auf_stunden(
            D.HAUSHALT_KWH_MONAT / calendar.monthrange(heute.year, heute.month)[1],
            D.STUNDENFORM["haushalt"],
        )
        anteil = jetzt.minute / 60.0
        einspeisung = netzbezug = 0.0
        for h in range(jetzt.hour + 1):
            faktor = anteil if h == jetzt.hour else 1.0
            ueber = max(0.0, erz_h[h] - verbrauch_h[h] - haushalt[h]) * faktor
            unter = max(0.0, verbrauch_h[h] + haushalt[h] - erz_h[h]) * faktor
            einspeisung += ueber
            netzbezug += unter
        # Kumulativ über die gespeicherten Monate hinweg, damit der Stand wie
        # ein Lebenszähler aussieht und nicht täglich zurückspringt.
        vortage = (heute - D.TAG_VON).days
        basis_e, basis_n = 12000.0 + vortage * 4.0, 8000.0 + vortage * 3.0
        h = jetzt.hour
        leistung_pv = erz_h[h] * 1000.0
        leistung_verbrauch = (verbrauch_h[h] + haushalt[h]) * 1000.0
        return [
            (f"{self.praefix}/energy/einspeisung_kwh", f"{basis_e + einspeisung:.3f}"),
            (f"{self.praefix}/energy/netzbezug_kwh", f"{basis_n + netzbezug:.3f}"),
            (f"{self.praefix}/live/einspeisung_w",
             f"{max(0.0, leistung_pv - leistung_verbrauch):.1f}"),
            (f"{self.praefix}/live/netzbezug_w",
             f"{max(0.0, leistung_verbrauch - leistung_pv):.1f}"),
        ]

    # — Senden ————————————————————————————————————————————————————

    def lauf(self) -> int:
        import paho.mqtt.client as mqtt

        # ⛔ **Die Client-ID trägt die Anlagen-ID — gemessen am 14.09.2026.**
        # Mit einer festen ID kicken sich zwei Feeder gegenseitig: Ein Broker
        # trennt die bestehende Sitzung, sobald sich eine zweite mit derselben
        # Client-ID anmeldet (MQTT 3.1.1 §3.1.4-2). Beide reconnecten, beide
        # werden wieder getrennt — und im Log stand trotzdem „20 Topics
        # gesendet", weil `publish()` in den Puffer schreibt und nicht meldet,
        # dass niemand zuhört. Auf der Lab-Seite kam genau eine der beiden
        # Anlagen an.
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"eedc-pruefstand-feeder-{self.anlage_id}")
        if self.args.user:
            client.username_pw_set(self.args.user, self.args.password)
        client.connect(self.args.broker, self.args.port, keepalive=60)
        client.loop_start()
        if self.geraete_mit_modus:
            print("Hinweis: Der Betriebsmodus kommt NICHT über MQTT — ein "
                  "Zustandsfeld bekommt in der Registry kein Topic "
                  "(mqtt_topic_registry.py, #263 K-2: der Inbound-Parser ist "
                  "float(payload)). Betroffene Geräte: "
                  + " · ".join(self.geraete_mit_modus), flush=True)
        runden = 0
        try:
            while True:
                jetzt = datetime.now()
                nachrichten = self.nachrichten(jetzt)
                unbekannt = [t for t, _w in nachrichten if t not in self.erwartet]
                fehler = 0
                for topic, wert in nachrichten:
                    ergebnis = client.publish(topic, wert, qos=0, retain=False)
                    if ergebnis.rc != mqtt.MQTT_ERR_SUCCESS:
                        fehler += 1
                runden += 1
                print(f"[{jetzt:%Y-%m-%d %H:%M:%S}] {len(nachrichten)} Topics "
                      f"gesendet" + (f", {fehler} NICHT angenommen" if fehler else "")
                      + (f" ({len(unbekannt)} nicht in der Erwartungsliste: "
                         f"{unbekannt[:3]})" if unbekannt else ""),
                      flush=True)
                if self.args.einmal:
                    break
                time.sleep(self.args.intervall)
        except KeyboardInterrupt:
            print("abgebrochen", flush=True)
        finally:
            client.loop_stop()
            client.disconnect()
        return runden


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--broker", required=True)
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--api", help="Basis-URL einer erreichbaren eedc-Instanz")
    p.add_argument("--ssh", help="SSH-Ziel, über das die API erreichbar ist "
                                 "(z. B. root@10.100.1.167)")
    p.add_argument("--anlage", type=int, required=True)
    p.add_argument("--intervall", type=int, default=300,
                   help="Sekunden zwischen zwei Runden (Default 300 = 5 min)")
    p.add_argument("--einmal", action="store_true")
    args = p.parse_args()
    Feeder(args).lauf()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
