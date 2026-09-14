"""Die nachgestellten Wärme/Klima-Anlagen — Geräte, Monatswerte, Tagesprofile.

**Reine Rechnung, kein Schreibpfad.** Beide Schreiber (`--db` und `--api`) lesen
dieselben Funktionen; eine zweite Zahlenquelle wäre genau die Drift, gegen die
`monats_fakten.py` gebaut ist.

## Was hier nachgestellt wird

Die Lagen aus [`docs/HANDBUCH_WAERME_KLIMA.md`](../../docs/HANDBUCH_WAERME_KLIMA.md)
§6 — sie sind laut Konzept §11.6 *„der Ersatz für den fehlenden Gegenprüfer"*
(der Maintainer besitzt weder Wärmepumpe noch Klimaanlage):

| Gerät | Anlage | Lage |
| --- | --- | --- |
| **Bosch Climate 5000 Multisplit** | die bestehende Demo-Anlage | **A/E** — Luft-Luft neben der Luft-Wasser-Daikin ⇒ Cockpit-Arbeitszahl „—" mit dem Grund *Wärmepumpe und Klimaanlage in einer Zahl*, die Daikin behält ihre Zahl im Hub |
| **Vaillant aroTHERM plus** | Prüfstand Wärme/Klima | **G** — EIN Wärmemengenzähler über Heizung und Warmwasser (`waerme_kwh`), getrennte Stromzähler ⇒ AZ gesamt 3,0, Heizen/Warmwasser „—" |
| **Nibe S1255** | Prüfstand Wärme/Klima | **D** — Sole-Wasser mit Kältemengenzähler ⇒ Arbeitszahl Kühlen 3,00 |
| **Stiebel Eltron WWK 300** | Prüfstand Wärme/Klima | **F** — Brauchwasser-WP ⇒ nur Strom + Warmwasser, keine Heiz-Achse |

## Warum die Jahressummen exakt getroffen werden

Die Handbuch-Zahlen sind **prüfbare Zusagen** (3000 · 3,0 · 3,00), keine
Illustration. Ein Generator, der jeden Monat einzeln rundet, verfehlt sie um
Zehntel — und eine Nachmessung, die „3,0002" akzeptieren muss, prüft nichts
mehr. Deshalb:

1. Jedes Feld hat eine **Jahressumme** und ein **Roh-Gewichtsprofil** je Monat.
2. Das Profil wird über die **im Lauf erzeugten Monate desselben Kalenderjahrs**
   normiert — ein Teiljahr bekommt damit seinen Anteil, kein volles Jahr.
3. Nach dem Runden trägt der **letzte erzeugte Monat des Jahres** den Rest, so
   dass die Jahressumme exakt stimmt.

Für ein volles Kalenderjahr (hier: 2025) gilt damit Σ = Jahressumme auf die
Nachkommastelle genau; die Nachmessung kann gegen das Handbuch tabellieren.

## Determinismus

Es gibt **keine Zufallszahl und keine echte Uhr**. Die Tagesstreuung kommt aus
einer festen Funktion der Ordnungszahl des Datums (`_tagesfaktor`), die
Monatsprofile stehen als Konstanten. Zweimal ausgeführt liefert dieses Modul
bitgleiche Zahlen — das ist die Voraussetzung dafür, dass ein zweiter
Seed-Lauf nichts ändert.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta
from typing import Iterable, Optional

# ── Marke und Fenster ────────────────────────────────────────────────────────

#: Marke an jedem erzeugten Gerät. Der Seeder erkennt daran seine eigenen
#: Zeilen und **aktualisiert** sie, statt ein zweites Gerät anzulegen.
PRUEFSTAND_MARKE = "wk15"

#: Monatsfenster des **Generators**. **Feste Konstanten, keine `date.today()`**
#: — ein Generator, der die Prozessuhr liest, liefert morgen andere Zahlen.
#:
#: ⚠ Er reicht bis **September 2026**, gespeichert wird aber nur bis August
#: ({@link MONAT_BIS_GESPEICHERT}). Der Grund ist die Normierung: Sie läuft je
#: Kalenderjahr über die erzeugten Monate — ließe man September hier weg und
#: bräuchte ihn für die Tagesebene, ergäben zwei Aufrufe zwei verschiedene
#: Zahlenreihen für Januar bis August 2026. **Der laufende Monat ist deshalb
#: erzeugt und ungespeichert**, genau wie in einer echten Anlage.
MONAT_VON = (2024, 9)
MONAT_BIS = (2026, 9)

#: Bis hierhin schreibt der Seeder Monatszeilen — **September 2026 bleibt
#: leer**. Er ist der laufende Monat: im Lab kommt er über MQTT, in der Demo-DB
#: aus den Tages-Snapshots.
MONAT_BIS_GESPEICHERT = (2026, 8)

#: Tagesfenster der **Prüfstand-Anlage** (sie bringt keine Tageszeilen mit, also
#: wählt der Seed sie). Es reicht bewusst bis kurz vor „heute", damit der
#: **laufende** Monat (September 2026) aus den Tages-Snapshots entsteht — der
#: Nicht-DB-Pfad, den Cockpit → Monat für einen Monat ohne gespeicherte Zeile
#: geht.
TAG_VON = date(2026, 4, 8)
TAG_BIS = date(2026, 9, 13)

#: Name der neuen Anlage.
PRUEFSTAND_ANLAGE = "Prüfstand Wärme/Klima"


# ── Gewichtsprofile je Monat (roh, werden normiert) ──────────────────────────
#
# Sie beschreiben die **Form** über das Jahr, nicht die Menge. Die Menge steht
# als Jahressumme am Gerät.

_HEIZEN = {1: 19.0, 2: 16.0, 3: 12.0, 4: 7.0, 5: 3.0, 6: 0.0,
           7: 0.0, 8: 0.0, 9: 2.0, 10: 7.0, 11: 14.0, 12: 20.0}
_WARMWASSER = {1: 10.0, 2: 9.0, 3: 9.0, 4: 8.0, 5: 7.5, 6: 7.0,
               7: 7.0, 8: 7.0, 9: 7.5, 10: 8.0, 11: 9.0, 12: 10.0}
#: Kühlbetrieb einer Wärmepumpe — nur die drei Sommermonate.
_KUEHLEN_WP = {6: 30.0, 7: 40.0, 8: 30.0}
#: Kühlbetrieb einer Split-Klimaanlage — sie läuft früher an und länger nach.
_KUEHLEN_KLIMA = {5: 5.0, 6: 25.0, 7: 35.0, 8: 25.0, 9: 10.0}
#: Heizbetrieb der Klimaanlage (ein Wintergarten, kein Haus).
_HEIZEN_KLIMA = {1: 25.0, 2: 20.0, 3: 15.0, 4: 5.0,
                 10: 5.0, 11: 12.0, 12: 18.0}
_KONSTANT = {m: 1.0 for m in range(1, 13)}

#: Feldname der Modus-Abdeckung. Er steht hier als Konstante statt als
#: Zeichenkette im Code, weil er im Backend in `core/betriebsmodus.py`
#: (``MODUS_ABDECKUNG_FELD``) definiert ist — ein Tippfehler wäre hier stumm.
MODUS_ABDECKUNG_FELD = "modus_abdeckung_h"

#: Saisonale Arbeitszahl-Form: im Winter schlechter, im Übergang besser. Sie
#: moduliert **nur** die Wärme-Profile — die Jahressumme bleibt exakt, die
#: monatliche Arbeitszahl wird dadurch realistisch ungleich.
_COP_SAISON = {1: 0.85, 2: 0.88, 3: 0.98, 4: 1.10, 5: 1.20, 6: 1.25,
               7: 1.25, 8: 1.22, 9: 1.15, 10: 1.02, 11: 0.92, 12: 0.86}


def _normiere(profil: dict[int, float]) -> dict[int, float]:
    summe = sum(profil.values())
    if summe <= 0:
        return {m: 0.0 for m in profil}
    return {m: w / summe for m, w in profil.items()}


def _kombiniere(teile: Iterable[tuple[float, dict[int, float]]]) -> dict[int, float]:
    """Gewichtete Summe **normierter** Profile — z. B. Strom = 600 Heizen + 400 WW."""
    aus = {m: 0.0 for m in range(1, 13)}
    for anteil, profil in teile:
        norm = _normiere(profil)
        for m in range(1, 13):
            aus[m] += anteil * norm.get(m, 0.0)
    return aus


def _mal_saison(profil: dict[int, float], saison: dict[int, float]) -> dict[int, float]:
    return {m: profil.get(m, 0.0) * saison.get(m, 1.0) for m in range(1, 13)}


# ── Geräte ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Feld:
    """Ein Monatsfeld: Jahressumme + Form über das Jahr."""
    name: str
    jahressumme: float
    profil: dict[int, float]


@dataclass(frozen=True)
class Geraet:
    """Ein nachgestelltes Gerät samt Stammdaten und Erfassungsweg."""
    schluessel: str
    #: ``"demo"`` = in die bestehende Demo-Anlage, ``"pruefstand"`` = neue Anlage.
    ziel: str
    bezeichnung: str
    lage: str
    anschaffungsdatum: date
    anschaffungskosten_gesamt: float
    #: Was das Ersatzgerät gekostet hätte. **0 ist eine gültige Antwort** (so
    #: sagt es der Daten-Checker wörtlich) — ein Neubau ersetzt keine Heizung.
    anschaffungskosten_alternativ: float
    parameter: dict
    felder: tuple[Feld, ...]
    #: Feldnamen, für die ein **Zähler** zugeordnet wird (sensor_mapping +
    #: Tages-Snapshots). Felder, die nur hier fehlen, gelten als von Hand
    #: gepflegte Monatswerte — genau die Lage, in der Tag und Monat
    #: verschiedene Wege gehen (Kanon K3).
    zaehler: tuple[str, ...] = ()
    #: Trägt das Gerät ein Betriebsmodus-Signal? Dann schreibt der Seed die
    #: Modus-Spur in ``tages_energie_profil.betriebsmodus_je_wp``.
    modus_signal: bool = False
    #: Was der Monatsabschluss aus diesem Signal **gespeichert** hätte:
    #:
    #: * ``"keine"`` — nichts (kein Modus-Sensor),
    #: * ``"abdeckung"`` — nur die Abdeckung. Für ein Gerät, dessen Aufteilung
    #:   ohnehin aus **gemessenen** Betriebsart-Zählern kommt (K2): Die
    #:   Abdeckung beschreibt das Signal, die Aufteilung die Zähler.
    #: * ``"abgeleitet"`` — Abdeckung **und** der aus dem Modus gerechnete
    #:   Split. ⛔ Ohne ihn wäre die Abdeckung schädlich: Sie allein zieht den
    #:   ganzen Strom des Geräts in den **Bezug** des Balkens
    #:   (`imd_monatsaggregat.py:355`), und ohne Teilmengen landet er
    #:   vollständig unter *nicht aufgeteilt* — gemessen 2250 von 2550 kWh.
    modus_monatsfelder: str = "keine"
    typ: str = "waermepumpe"


def _jahresprofil_waerme(strom_profil: dict[int, float]) -> dict[int, float]:
    """Wärme folgt dem Strom, moduliert mit der saisonalen Arbeitszahl."""
    return _mal_saison(strom_profil, _COP_SAISON)


# — A/E: die Split-Klimaanlage neben der Daikin (dietmar1968) ————————————
#
# EIN Gesamtstromzähler, drei Innengeräte, Betriebsmodus am Gerät. Wärme meldet
# sie nicht (bauartbedingt kein Wärmemengenzähler) — genau dadurch entsteht im
# Cockpit der Grund „Wärmepumpe und Klimaanlage in einer Zahl".
#
# ⚠ `alter_energietraeger="nichts"`: Sie ersetzt keine Heizung. Ohne das stünde
# eine Gas-Ersparnis für Wärme, die das Gerät nie erzeugt hat (A9/N-304).
_MULTISPLIT_STROM = _kombiniere([(150.0, _HEIZEN_KLIMA), (260.0, _KUEHLEN_KLIMA),
                                 (110.0, _KONSTANT)])

MULTISPLIT = Geraet(
    schluessel="multisplit",
    ziel="demo",
    bezeichnung="Bosch Climate 5000 Multisplit",
    lage="A/E",
    anschaffungsdatum=date(2024, 9, 1),
    anschaffungskosten_gesamt=7400.0,
    anschaffungskosten_alternativ=0.0,
    parameter={
        "wp_art": "luft_luft",
        "effizienz_modus": "gesamt_jaz",
        "leistung_kw": 7.0,
        "getrennte_strommessung": False,
        "kuehlung_art": "aktiv",
        "alter_energietraeger": "nichts",
        # Der Daten-Checker verlangt beide: `jaz` fuer den Effizienz-Modus
        # `gesamt_jaz`, `anschaffungskosten_alternativ` als Gegenrechnung.
        # ⚠ Die gepflegte JAZ macht die Waerme NICHT abgeleitet — das
        # entscheidet allein die Provenance-Marke der Zeile
        # (`heizwaerme_ist_abgeleitet`), und der Seed setzt sie nicht.
        "jaz": 3.2,
        "innengeraete": [
            {"id": 1, "bezeichnung": "Wintergarten"},
            {"id": 2, "bezeichnung": "Wohnzimmer"},
            {"id": 3, "bezeichnung": "Dachgeschoss"},
        ],
        "pruefstand": PRUEFSTAND_MARKE,
        "pruefstand_lage": "A/E",
    },
    felder=(
        Feld("stromverbrauch_kwh", 520.0, _MULTISPLIT_STROM),
        Feld("betriebsart_strom_heizen_kwh", 150.0, _HEIZEN_KLIMA),
        Feld("betriebsart_strom_kuehlen_kwh", 260.0, _KUEHLEN_KLIMA),
    ),
    # Nur der Gesamtzähler und der Modus hängen am Tag. Die zwei
    # Betriebsart-Werte sind **Monatswerte** (wie aus dem Monatsabschluss) —
    # damit zeigt der Monat die GEMESSENE Aufteilung (K2) und der Tag die
    # ABGELEITETE aus der Modus-Spur. Beide Wege sind so klickbar.
    zaehler=("stromverbrauch_kwh",),
    modus_signal=True,
    modus_monatsfelder="abdeckung",
)

# — G: ein Wärmemengenzähler für Heizung und Warmwasser ————————————————
_VAILLANT_STROM = _kombiniere([(600.0, _HEIZEN), (400.0, _WARMWASSER)])

VAILLANT = Geraet(
    schluessel="vaillant",
    ziel="pruefstand",
    bezeichnung="Vaillant aroTHERM plus",
    lage="G",
    anschaffungsdatum=date(2024, 9, 1),
    anschaffungskosten_gesamt=24500.0,
    anschaffungskosten_alternativ=9000.0,
    parameter={
        "wp_art": "luft_wasser",
        "effizienz_modus": "gesamt_jaz",
        "leistung_kw": 10.0,
        "getrennte_strommessung": True,
        "kuehlung_art": "keine",
        "vorlauftemperatur": 35,
        "alter_energietraeger": "gas",
        "alter_preis_cent_kwh": 12.0,
        "alternativ_zusatzkosten_jahr": 320.0,
        "pv_anteil_prozent": 30,
        "jaz": 3.0,
        "heizwaermebedarf_kwh": 9000,
        "warmwasserbedarf_kwh": 2400,
        "pruefstand": PRUEFSTAND_MARKE,
        "pruefstand_lage": "G",
    },
    felder=(
        Feld("strom_heizen_kwh", 600.0, _HEIZEN),
        Feld("strom_warmwasser_kwh", 400.0, _WARMWASSER),
        # 3000 ÷ (600 + 400) = 3,0 — die Zahl, die im Handbuch steht.
        # `heizenergie_kwh` und `warmwasser_kwh` bleiben LEER: der eine Zähler
        # misst beides, und wer ihn auf „Heizwärme" legt, bekommt 5,0 statt 3,0.
        Feld("waerme_kwh", 3000.0, _jahresprofil_waerme(_VAILLANT_STROM)),
    ),
    zaehler=("strom_heizen_kwh", "strom_warmwasser_kwh", "waerme_kwh"),
    # ⭐ **Das Gerät mit dem Betriebsmodus-Sensor und OHNE Betriebsart-Zähler.**
    # Nur so ist der **abgeleitete** Modus-Split überhaupt sichtbar: Ein
    # einziger gemessener Betriebsart-Zähler schaltet das Gerät ganz auf den
    # gemessenen Weg (K2), und dann verschwindet die Abdeckung. Die Nibe
    # daneben zeigt den gemessenen Weg — beide Wege in einer Anlage.
    modus_signal=True,
    modus_monatsfelder="abgeleitet",
)

# — D: Sole-Wasser mit Kältemengenzähler ————————————————————————————————
_NIBE_HEIZ_STROM = _HEIZEN
_NIBE_WW_STROM = _WARMWASSER

NIBE = Geraet(
    schluessel="nibe",
    ziel="pruefstand",
    bezeichnung="Nibe S1255 Erdwärme",
    lage="D",
    anschaffungsdatum=date(2024, 9, 1),
    anschaffungskosten_gesamt=31800.0,
    anschaffungskosten_alternativ=11000.0,
    parameter={
        "wp_art": "sole_wasser",
        "effizienz_modus": "gesamt_jaz",
        "leistung_kw": 8.0,
        "getrennte_strommessung": True,
        "kuehlung_art": "aktiv",
        "vorlauftemperatur": 35,
        "alter_energietraeger": "oel",
        "alter_preis_cent_kwh": 10.0,
        "alternativ_zusatzkosten_jahr": 280.0,
        "pv_anteil_prozent": 25,
        "jaz": 4.2,
        "heizwaermebedarf_kwh": 11000,
        "warmwasserbedarf_kwh": 2800,
        "pruefstand": PRUEFSTAND_MARKE,
        "pruefstand_lage": "D",
    },
    felder=(
        Feld("strom_heizen_kwh", 900.0, _NIBE_HEIZ_STROM),
        Feld("heizenergie_kwh", 4050.0, _jahresprofil_waerme(_NIBE_HEIZ_STROM)),
        Feld("strom_warmwasser_kwh", 350.0, _NIBE_WW_STROM),
        Feld("warmwasser_kwh", 1050.0, _jahresprofil_waerme(_NIBE_WW_STROM)),
        # 900 ÷ 300 = 3,00 — Handbuch D, an Hub und Cockpit dieselbe Zahl.
        Feld("betriebsart_strom_kuehlen_kwh", 300.0, _KUEHLEN_WP),
        Feld("betriebsart_nutzenergie_kuehlen_kwh", 900.0, _KUEHLEN_WP),
    ),
    zaehler=(
        "strom_heizen_kwh", "heizenergie_kwh",
        "strom_warmwasser_kwh", "warmwasser_kwh",
        "betriebsart_strom_kuehlen_kwh", "betriebsart_nutzenergie_kuehlen_kwh",
    ),
    modus_signal=True,
    # In den Kühlmonaten gewinnt der **gemessene** Zähler (K2), in den übrigen
    # trägt der abgeleitete Split — dieselbe Anlage, zwei Wege, je nach Monat.
    modus_monatsfelder="abgeleitet",
)

# — F: Brauchwasser-Wärmepumpe ——————————————————————————————————————————
STIEBEL = Geraet(
    schluessel="stiebel",
    ziel="pruefstand",
    bezeichnung="Stiebel Eltron WWK 300",
    lage="F",
    anschaffungsdatum=date(2024, 9, 1),
    anschaffungskosten_gesamt=3200.0,
    anschaffungskosten_alternativ=1200.0,
    parameter={
        "wp_art": "brauchwasser",
        "effizienz_modus": "gesamt_jaz",
        "leistung_kw": 1.5,
        "getrennte_strommessung": False,
        "kuehlung_art": "keine",
        "alter_energietraeger": "strom",
        "alter_preis_cent_kwh": 30.0,
        "jaz": 3.0,
        "warmwasserbedarf_kwh": 1800,
        "pruefstand": PRUEFSTAND_MARKE,
        "pruefstand_lage": "F",
    },
    felder=(
        Feld("stromverbrauch_kwh", 600.0, _WARMWASSER),
        # 1800 ÷ 600 = 3,0 — Handbuch F.
        Feld("warmwasser_kwh", 1800.0, _jahresprofil_waerme(_WARMWASSER)),
    ),
    zaehler=("stromverbrauch_kwh", "warmwasser_kwh"),
)

GERAETE: tuple[Geraet, ...] = (MULTISPLIT, VAILLANT, NIBE, STIEBEL)

#: Die PV der Prüfstand-Anlage. **Kontext, kein Prüfgegenstand** — sie steht
#: hier, weil `POST /api/anlagen/` ein `leistung_kwp > 0` erzwingt und weil
#: Cockpit/Aussichten sonst eine Anlage ohne jede Erzeugung zeigen müssten.
PRUEFSTAND_PV_KWP = 8.0
_PV_PROFIL = {1: 25.0, 2: 45.0, 3: 80.0, 4: 105.0, 5: 120.0, 6: 125.0,
              7: 128.0, 8: 110.0, 9: 78.0, 10: 45.0, 11: 22.0, 12: 17.0}
PRUEFSTAND_PV = Geraet(
    schluessel="pv_pruefstand",
    ziel="pruefstand",
    bezeichnung="Süddach Prüfstand",
    lage="—",
    anschaffungsdatum=date(2024, 9, 1),
    anschaffungskosten_gesamt=11200.0,
    anschaffungskosten_alternativ=0.0,
    parameter={"anzahl_module": 16, "modul_leistung_wp": 500,
               "ausrichtung_grad": 0, "neigung_grad": 30,
               "pruefstand": PRUEFSTAND_MARKE},
    felder=(Feld("pv_erzeugung_kwh", 7600.0, _PV_PROFIL),),
    zaehler=("pv_erzeugung_kwh",),
    typ="pv-module",
)

#: Haushaltsgrundlast der Prüfstand-Anlage (kWh/Monat, ohne Wärmepumpen).
HAUSHALT_KWH_MONAT = 280.0

#: Tarife der Prüfstand-Anlage. Ohne Tarif keine Ersparnis — Handbuch G
#: verlangt sie ausdrücklich („Ersparnis und CO₂ vollständig").
TARIFE = (
    {"gueltig_ab": date(2024, 9, 1), "gueltig_bis": date(2025, 12, 31),
     "netzbezug_arbeitspreis_cent_kwh": 31.0, "einspeiseverguetung_cent_kwh": 8.1,
     "grundpreis_euro_monat": 13.0, "tarifname": "Prüfstand Basis 2024",
     "anbieter": "Stadtwerke Prüfstand"},
    {"gueltig_ab": date(2026, 1, 1), "gueltig_bis": None,
     "netzbezug_arbeitspreis_cent_kwh": 29.5, "einspeiseverguetung_cent_kwh": 8.0,
     "grundpreis_euro_monat": 13.5, "tarifname": "Prüfstand Basis 2026",
     "anbieter": "Stadtwerke Prüfstand"},
)


# ── Monatsachse ──────────────────────────────────────────────────────────────

def monatsachse(von: tuple[int, int] = MONAT_VON,
                bis: tuple[int, int] = MONAT_BIS) -> list[tuple[int, int]]:
    """Alle ``(jahr, monat)`` von *von* bis *bis*, beide inklusive."""
    jahr, monat = von
    aus: list[tuple[int, int]] = []
    while (jahr, monat) <= bis:
        aus.append((jahr, monat))
        jahr, monat = (jahr + 1, 1) if monat == 12 else (jahr, monat + 1)
    return aus


def _rundung(wert: float) -> float:
    return round(wert, 1)


def monatswerte(
    geraet: Geraet, monate: Optional[list[tuple[int, int]]] = None,
) -> dict[tuple[int, int], dict[str, float]]:
    """``{(jahr, monat): {feld: kwh}}`` — exakt auf die Jahressumme normiert.

    Die Normierung läuft **je Kalenderjahr über die erzeugten Monate**: Ein
    volles Jahr trifft die Jahressumme auf die Nachkommastelle, ein Teiljahr
    seinen Anteil daran. Der letzte erzeugte Monat eines Jahres trägt den
    Rundungsrest — ohne das verfehlte die Summe die Handbuch-Zahl um Zehntel,
    und die Nachmessung hätte nichts mehr, wogegen sie prüfen könnte.
    """
    monate = monate or monatsachse()
    monate = [(j, m) for j, m in monate
              if date(j, m, calendar.monthrange(j, m)[1]) >= geraet.anschaffungsdatum]
    aus: dict[tuple[int, int], dict[str, float]] = {(j, m): {} for j, m in monate}

    jahre: dict[int, list[int]] = {}
    for j, m in monate:
        jahre.setdefault(j, []).append(m)

    for feld in geraet.felder:
        for jahr, monatsliste in jahre.items():
            gewichte = {m: feld.profil.get(m, 0.0) for m in monatsliste}
            summe = sum(gewichte.values())
            if summe <= 0:
                for m in monatsliste:
                    aus[(jahr, m)][feld.name] = 0.0
                continue
            ziel = _rundung(feld.jahressumme * summe / sum(
                feld.profil.get(m, 0.0) for m in range(1, 13)
            ))
            werte = {m: _rundung(feld.jahressumme * g / sum(
                feld.profil.get(mm, 0.0) for mm in range(1, 13)
            )) for m, g in gewichte.items()}
            rest = _rundung(ziel - sum(werte.values()))
            letzter = max(monatsliste)
            werte[letzter] = _rundung(werte[letzter] + rest)
            for m, w in werte.items():
                aus[(jahr, m)][feld.name] = max(0.0, w)

    # Monate, in denen das Gerät gar nichts tut, tragen keine Nullzeile: eine
    # gepflegte 0 ist eine Messung („diesen Monat nichts"), und für die
    # Bilanzgröße ist sie richtig — für eine Teilmenge wäre sie eine Behauptung.
    # ⚠ **Ein zugeordneter Zähler liefert jeden Monat einen Wert — auch die 0.**
    # Sie ist dann eine Messung („diesen Monat keine Heizwärme") und keine
    # Leerstelle; der Daten-Checker meldete sonst „Heizwärme fehlt in 6
    # Monat(en)" für einen Sommer, in dem nicht geheizt wurde.
    #
    # ⛔ **Betriebsart-Felder bleiben ausgenommen.** Ein gemessener Zähler
    # schaltet das Gerät *ganz* auf den gemessenen Weg (K2) — eine 0 im Winter
    # nähme der Klimaanlage damit ganzjährig den abgeleiteten Split.
    behalten = set(_BILANZ_FELDER) | {
        f for f in geraet.zaehler if not f.startswith("betriebsart_")
    }
    for schluessel, werte in aus.items():
        for name in list(werte):
            if werte[name] <= 0 and name not in behalten:
                del werte[name]

    # ⭐ **Die Abdeckung gehört zum Signal, nicht zur Aufteilung.** Sie sagt,
    # wie viele Stunden eedc den Betriebsmodus mitlesen konnte — und das gilt
    # unabhängig davon, ob die ausgewiesene Aufteilung am Ende aus Zählern
    # (K2 gemessen) oder aus dem Modus kommt. Ohne sie stünde im Monat „Modus
    # erfasst: 0 h" neben einer Aufteilung, die sichtbar existiert.
    if geraet.modus_monatsfelder != "keine":
        for (jahr, monat), werte in aus.items():
            if not werte:
                continue
            stunden = calendar.monthrange(jahr, monat)[1] * 24
            # 8 % Ausfall: eine Abdeckung von exakt 100 % gibt es nicht, und
            # der Rest ist genau die Erklärung für „nicht aufgeteilt".
            werte[MODUS_ABDECKUNG_FELD] = round(stunden * 0.92, 1)
            if geraet.modus_monatsfelder != "abgeleitet":
                continue
            for quelle, ziel in _MODUS_ABGELEITET_AUS.items():
                if werte.get(quelle, 0.0) > 0:
                    werte[ziel] = round(werte[quelle] * MODUS_TREFFERQUOTE, 1)
    return aus


#: Anteil des Stroms, den der abgeleitete Split einer Betriebsart zuordnen
#: kann. Der Rest ist Standby, Automatik ohne Ist-Signal und die Zeit ohne
#: Signal — er heißt *nicht aufgeteilt* und soll im Bild **vorkommen**.
MODUS_TREFFERQUOTE = 0.93

#: Summand → abgeleitete Teilmenge. Zwei Familien, zwei Namen (E-G): der
#: Summand ist ein physischer Zähler, die Teilmenge ein Ausschnitt aus dem
#: Gesamtverbrauch.
_MODUS_ABGELEITET_AUS = {
    "strom_heizen_kwh": "modus_strom_heizen_kwh",
    "strom_warmwasser_kwh": "modus_strom_warmwasser_kwh",
}


#: Felder, die auch mit 0 in der Zeile stehen dürfen — die Bilanzgrößen. Eine
#: Teilmenge (Betriebsart) mit 0 wäre dagegen die Aussage „gemessen, es kam
#: nichts heraus" und würde das Gerät auf den gemessenen Weg schalten (K2).
_BILANZ_FELDER = frozenset({
    "stromverbrauch_kwh", "strom_heizen_kwh", "strom_warmwasser_kwh",
    "pv_erzeugung_kwh",
})


def jahressumme_erzeugt(geraet: Geraet, jahr: int,
                        monate: Optional[list[tuple[int, int]]] = None) -> dict[str, float]:
    """Σ je Feld über ein Kalenderjahr — die Zahl, gegen die das Handbuch prüft."""
    werte = monatswerte(geraet, monate)
    aus: dict[str, float] = {}
    for (j, _m), zeile in werte.items():
        if j != jahr:
            continue
        for name, wert in zeile.items():
            aus[name] = round(aus.get(name, 0.0) + wert, 3)
    return aus


# ── Tagesachse ───────────────────────────────────────────────────────────────

def tagesachse(von: date = TAG_VON, bis: date = TAG_BIS) -> list[date]:
    tage: list[date] = []
    d = von
    while d <= bis:
        tage.append(d)
        d += timedelta(days=1)
    return tage


def _tagesfaktor(datum: date) -> float:
    """Deterministische Tagesstreuung um 1,0 (0,82 … 1,18).

    **Keine Zufallszahl.** Eine Streuung ist nötig, damit der Monatsverlauf
    nicht wie ein Rechteck aussieht; sie muss aber bei jedem Lauf dieselbe
    sein, sonst ändert ein zweiter Seed-Lauf die Zahlen.
    """
    rest = (datum.toordinal() * 7919) % 37
    return 0.82 + (rest / 36.0) * 0.36


def _tagesanteile(jahr: int, monat: int) -> dict[int, float]:
    """Anteil jedes Tages am Monat — Σ über den **ganzen** Monat = 1,0.

    ⚠ Normiert wird über alle Tage des Monats, nicht über die Tage im
    Seed-Fenster. Ein angeschnittener Monat bekommt so seinen echten Anteil;
    über das Fenster zu normieren hieße, einen halben Monat auf einen ganzen
    hochzurechnen.
    """
    tage = calendar.monthrange(jahr, monat)[1]
    faktoren = {t: _tagesfaktor(date(jahr, monat, t)) for t in range(1, tage + 1)}
    summe = sum(faktoren.values())
    return {t: f / summe for t, f in faktoren.items()}


#: Stundenform je Feld-Familie. Werte sind relative Gewichte über 24 Stunden.
def _form(aktiv: dict[int, float], grund: float = 0.0) -> list[float]:
    return [aktiv.get(h, grund) for h in range(24)]


_STUNDENFORM = {
    # Heizen: Morgenspitze und Abendspitze, nachts abgesenkt.
    "heizen": _form({0: 0.6, 1: 0.6, 2: 0.6, 3: 0.7, 4: 0.9, 5: 1.6, 6: 2.2,
                     7: 2.0, 8: 1.4, 9: 1.0, 10: 0.8, 11: 0.7, 12: 0.7,
                     13: 0.7, 14: 0.7, 15: 0.8, 16: 1.1, 17: 1.6, 18: 2.0,
                     19: 2.0, 20: 1.6, 21: 1.2, 22: 0.9, 23: 0.7}),
    # Warmwasser: zwei schmale Ladefenster.
    "warmwasser": _form({5: 1.5, 6: 3.0, 7: 2.0, 17: 1.5, 18: 3.0, 19: 2.0},
                        grund=0.05),
    # Kühlen: Nachmittag und früher Abend.
    "kuehlen": _form({10: 0.4, 11: 0.8, 12: 1.4, 13: 2.0, 14: 2.4, 15: 2.6,
                      16: 2.4, 17: 2.0, 18: 1.5, 19: 1.0, 20: 0.6, 21: 0.3}),
    "konstant": [1.0] * 24,
    # PV: Tagesbogen.
    "pv": _form({6: 0.2, 7: 0.6, 8: 1.3, 9: 2.2, 10: 3.1, 11: 3.8, 12: 4.2,
                 13: 4.2, 14: 3.8, 15: 3.1, 16: 2.2, 17: 1.3, 18: 0.6,
                 19: 0.2}),
    # Haushalt: Grundlast mit Morgen-/Abendspitze.
    "haushalt": _form({0: 0.5, 1: 0.4, 2: 0.4, 3: 0.4, 4: 0.5, 5: 0.7, 6: 1.2,
                       7: 1.6, 8: 1.3, 9: 1.0, 10: 0.9, 11: 1.0, 12: 1.3,
                       13: 1.0, 14: 0.9, 15: 0.9, 16: 1.1, 17: 1.6, 18: 2.2,
                       19: 2.2, 20: 1.9, 21: 1.5, 22: 1.0, 23: 0.7}),
}

#: Feldname → Stundenform **und** Betriebsart. Die Betriebsart ist zugleich der
#: Modus, den die Modus-Spur für diese Stunde schreibt.
FELD_FAMILIE: dict[str, tuple[str, Optional[str]]] = {
    "stromverbrauch_kwh": ("konstant", None),
    "strom_heizen_kwh": ("heizen", "heizen"),
    "strom_warmwasser_kwh": ("warmwasser", "warmwasser"),
    "heizenergie_kwh": ("heizen", None),
    "warmwasser_kwh": ("warmwasser", None),
    "waerme_kwh": ("heizen", None),
    "betriebsart_strom_heizen_kwh": ("heizen", "heizen"),
    "betriebsart_strom_kuehlen_kwh": ("kuehlen", "kuehlen"),
    "betriebsart_nutzenergie_kuehlen_kwh": ("kuehlen", None),
    "pv_erzeugung_kwh": ("pv", None),
}


def _verteile(menge: float, form: list[float]) -> list[float]:
    summe = sum(form)
    if summe <= 0 or menge <= 0:
        return [0.0] * 24
    return [round(menge * f / summe, 4) for f in form]


@dataclass
class Tagesbild:
    """Was ein Gerät an einem Tag getan hat — Mengen je Feld und je Stunde."""
    datum: date
    #: ``{feld: tages_kwh}``
    tag: dict[str, float] = dc_field(default_factory=dict)
    #: ``{feld: [24 Stundenwerte]}``
    stunden: dict[str, list[float]] = dc_field(default_factory=dict)

    @property
    def strom_je_stunde(self) -> list[float]:
        """Der **elektrische** Verbrauch je Stunde — die Bilanzgröße des Geräts.

        ⛔ **Die drei Familien dürfen nicht addiert werden** (Konzept §3): Ein
        Gesamtzähler *ist* die Menge, die Summanden (Heizen + Warmwasser)
        ergeben sie, und die Teilmengen (Betriebsart) sind ein **Ausschnitt**
        daraus. Wer sie zusammenzählt, schreibt denselben Strom zweimal — der
        Fehler, an dem im Forum schon ein Tester gescheitert ist.

        Die Vorrangkette hier ist dieselbe wie in
        ``field_definitions.wp_strom_stufe``: Gesamtzähler schlägt Summanden;
        ein **gemessener** Betriebsart-Zähler ohne eigene Achse (Kühlstrom)
        kommt hinzu, weil ihn die Summanden nicht enthalten (W-16).

        Thermische Felder zählen nie mit — der Modus-Split normiert auf den
        Strom, und Wärme in dieselbe Summe zu legen wäre die
        Einheiten-Verwechslung, aus der schon eine Doppelzählung entstand.
        """
        aus = [0.0] * 24
        if "stromverbrauch_kwh" in self.stunden:
            quellen = ["stromverbrauch_kwh"]
        else:
            quellen = [f for f in ("strom_heizen_kwh", "strom_warmwasser_kwh",
                                   "betriebsart_strom_kuehlen_kwh")
                       if f in self.stunden]
        for feld in quellen:
            for h in range(24):
                aus[h] += self.stunden[feld][h]
        return [round(w, 4) for w in aus]


def tagesbild(geraet: Geraet, datum: date,
              monate: Optional[dict[tuple[int, int], dict[str, float]]] = None) -> Tagesbild:
    """Das Tagesbild eines Geräts — aus seinem Monatswert heruntergebrochen.

    Damit gilt: Σ Tage eines Monats ≈ Monatswert (bis auf die Rundung je
    Stunde). Der Tag ist also keine zweite Zahlenquelle, sondern dieselbe.
    """
    monate = monate if monate is not None else monatswerte(geraet)
    zeile = monate.get((datum.year, datum.month), {})
    anteil = _tagesanteile(datum.year, datum.month).get(datum.day, 0.0)
    bild = Tagesbild(datum=datum)
    for feld, monatswert in zeile.items():
        if feld == MODUS_ABDECKUNG_FELD or feld.startswith("modus_strom_"):
            # Die Abdeckung ist eine Stundenzahl, der abgeleitete Split eine
            # **Teilmenge** des Stroms — beides gehört nicht in die Tagesmengen.
            # Der Tag rechnet seinen Split selbst aus der Modus-Spur.
            continue
        familie, _modus = FELD_FAMILIE.get(feld, ("konstant", None))
        tageswert = round(monatswert * anteil, 4)
        if tageswert <= 0:
            continue
        bild.tag[feld] = tageswert
        form = (_gesamtstrom_form(zeile)
                if feld == "stromverbrauch_kwh" else _STUNDENFORM[familie])
        bild.stunden[feld] = _verteile(tageswert, form)
    return bild


def _gesamtstrom_form(zeile: dict[str, float]) -> list[float]:
    """Die Stundenform eines **Gesamtzählers** — aus dem, was das Gerät tut.

    ⛔ **Eine flache Form wäre eine Falschaussage über die Tageszeit.** Sie hat
    beim ersten Bau genau den Fehler erzeugt, gegen den der Modus-Split
    gebaut ist: Eine Klimaanlage, deren Strom gleichmäßig über 24 Stunden lag,
    verbrachte die Hälfte ihres Verbrauchs in Stunden ohne Kühlbetrieb — und
    *nicht aufgeteilt* wurde so groß wie *Kühlen*, obwohl das Gerät nachts
    nichts tat.

    Die Form ist deshalb die gewichtete Summe der Betriebsart-Formen dieses
    Monats plus einer Grundlast für den Rest (Standby).
    """
    gesamt = zeile.get("stromverbrauch_kwh", 0.0)
    teile: list[tuple[float, list[float]]] = []
    verteilt = 0.0
    for feld, wert in zeile.items():
        familie, modus = FELD_FAMILIE.get(feld, ("konstant", None))
        if modus is None or feld == "stromverbrauch_kwh" or wert <= 0:
            continue
        teile.append((wert, _STUNDENFORM[familie]))
        verteilt += wert
    if not teile:
        # Ein Gerät ganz ohne Betriebsart-Zähler (Brauchwasser-WP): Dann sagt
        # die **Nutzenergie**, wann es lief. Die Grundlast-Form wäre hier eine
        # Behauptung über die Tageszeit, die den Messwerten widerspricht —
        # ein Warmwasserspeicher lädt morgens und abends, nicht um drei Uhr.
        for feld, wert in zeile.items():
            familie, _modus = FELD_FAMILIE.get(feld, ("konstant", None))
            if feld == "stromverbrauch_kwh" or wert <= 0 or familie == "konstant":
                continue
            teile.append((wert, _STUNDENFORM[familie]))
        return _summiere_formen(teile) if teile else list(_STUNDENFORM["konstant"])
    rest = max(0.0, gesamt - verteilt)
    if rest > 0:
        teile.append((rest, _STUNDENFORM["konstant"]))
    return _summiere_formen(teile)


def _summiere_formen(teile: list[tuple[float, list[float]]]) -> list[float]:
    aus = [0.0] * 24
    for gewicht, form in teile:
        summe = sum(form) or 1.0
        for h in range(24):
            aus[h] += gewicht * form[h] / summe
    return aus


def modus_je_stunde(geraet: Geraet, bild: Tagesbild) -> dict[int, str]:
    """Der Betriebsmodus je Stunde — aus dem, was das Gerät in ihr getan hat.

    **Kein Rateweg.** Gewählt wird die Betriebsart mit dem größten
    Strombeitrag dieser Stunde; liegt keine über der Schwelle, steht ``aus``.
    Der Kanon kennt dafür sieben Werte (`core/betriebsmodus.py`); benutzt
    werden hier ``heizen`` · ``warmwasser`` · ``kuehlen`` · ``aus`` —
    und in den Randstunden einer Übergangszeit ``unbestimmt``, damit die
    Zeile *nicht aufgeteilt* im Bild auch wirklich vorkommt.
    """
    if not geraet.modus_signal:
        return {}
    beitraege: dict[str, list[float]] = {}
    for feld, werte in bild.stunden.items():
        _familie, modus = FELD_FAMILIE.get(feld, ("konstant", None))
        if modus is None:
            continue
        beitraege.setdefault(modus, [0.0] * 24)
        for h in range(24):
            beitraege[modus][h] += werte[h]
    # Ein Gerät ohne eigene Betriebsart-Felder (nur Gesamtzähler) bekommt seinen
    # Modus aus der Jahreszeit — sonst hätte die Klimaanlage keine Spur, obwohl
    # genau sie den Modus-Sensor trägt.
    if not beitraege:
        return {}
    aus: dict[int, str] = {}
    for h in range(24):
        kandidaten = {m: w[h] for m, w in beitraege.items() if w[h] > 0.002}
        if not kandidaten:
            # Jede vierte Standby-Stunde bleibt `unbestimmt` (Automatik ohne
            # Ist-Signal) — sie fällt in „nicht aufgeteilt" und macht die
            # Zeile *Modus erfasst* im Bild überhaupt erklärungsbedürftig.
            aus[h] = "unbestimmt" if (bild.datum.toordinal() + h) % 4 == 0 else "aus"
            continue
        aus[h] = max(kandidaten.items(), key=lambda kv: kv[1])[0]
    return aus


def betriebsart_monatswerte_aus_tagen(
    geraet: Geraet, tage: Iterable[date],
) -> dict[tuple[int, int], dict[str, float]]:
    """Σ der Tagesbilder je Monat — für die Gegenprobe Tag ↔ Monat."""
    monate = monatswerte(geraet)
    aus: dict[tuple[int, int], dict[str, float]] = {}
    for datum in tage:
        bild = tagesbild(geraet, datum, monate)
        ziel = aus.setdefault((datum.year, datum.month), {})
        for feld, wert in bild.tag.items():
            ziel[feld] = round(ziel.get(feld, 0.0) + wert, 3)
    return aus


# ── Sensor-Namen ─────────────────────────────────────────────────────────────

def demo_sensor(geraet: Geraet, feld: str) -> str:
    """Der Demo-Sensorname eines Feldes — Bauform wie `reseed-v4-tag-demo.py`."""
    return f"sensor.demo_{geraet.schluessel}_{feld}"


def snapshot_key(inv_id: int, feld: str) -> str:
    """Der Schlüssel der Zählerreihe (``inv:{id}:{feld}``)."""
    return f"inv:{inv_id}:{feld}"


#: Öffentliche Namen für die zwei Helfer, die auch der Seeder braucht
#: (Haushalts- und PV-Stundenform der Prüfstand-Anlage).
STUNDENFORM = _STUNDENFORM
verteile_auf_stunden = _verteile


#: Startstände der kumulativen Zähler. Frei gewählt, aber fest — ein
#: wandernder Startstand machte zwei Läufe unterscheidbar.
ZAEHLER_STARTSTAND = {
    "stromverbrauch_kwh": 1200.0,
    "strom_heizen_kwh": 900.0,
    "strom_warmwasser_kwh": 500.0,
    "heizenergie_kwh": 4200.0,
    "warmwasser_kwh": 1600.0,
    "waerme_kwh": 5200.0,
    "betriebsart_strom_heizen_kwh": 300.0,
    "betriebsart_strom_kuehlen_kwh": 250.0,
    "betriebsart_nutzenergie_kuehlen_kwh": 700.0,
    "pv_erzeugung_kwh": 9000.0,
}
