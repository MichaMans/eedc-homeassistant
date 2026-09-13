"""**Der Monatspfad kennt K3** — ein zugeordneter Gesamtzähler zählt auch bei
getrennter Strommessung (N-451), der Abzug folgt der Stufe (N-462), und der
Stundenpfad zählt mit (N-461).

## Der Befund in einem Satz

`get_wp_strom_kwh` hat bei ``getrennte_strommessung`` (F5) **ausschließlich**
``strom_heizen_kwh + strom_warmwasser_kwh`` gelesen. Wer den Schalter setzte,
ohne (oder ohne **beide**) getrennten Zähler zu pflegen, bekam in **jeder**
Monats-Sicht 0 kWh oder die Hälfte — während derselbe Wert im Monatsabschluss
sichtbar danebenstand und der **Tagespfad** ihn seit dem 26.08.2026 (Etappe 3,
`530996f5`) zählt. Das ist **K3** wörtlich verletzt (SOLL §3.2, W-1/W-1b):
*„Ein Kennzeichen darf niemals dazu führen, dass ein vorhandener Zähler
ignoriert wird."*

## Die sieben Lagen, an denen alles hängt

| Lage | Gerät | Zeile | Tag (vorher) | Monat **vorher** | Monat **nachher** |
| --- | --- | --- | --- | --- | --- |
| **i** | Luft-Luft F5 | Gesamt 500 | 500 | **0** | **500** |
| **ii** | Luft-Wasser F5 | Gesamt 1000 + Heizen 600 | 1000 | **600** | **1000** |
| **iii** | Luft-Wasser F5 | Gesamt 1000 | 1000 | **0** | **1000** |
| **iv** | Luft-Wasser F5 | Gesamt 1000 + Heizen 600 + WW 400 | 1000 | 1000 | 1000 |
| **v** | Luft-Wasser F5 | Heizen 600 + WW 400 | 1000 | 1000 | 1000 |
| **vi** | Luft-Wasser, F5 **aus** | Gesamt 1000 | 1000 | 1000 | 1000 |
| **vii** | Luft-Luft F5 | Gesamt 500 + Heizen 300 + **WW 120** | 500 | **420** | **500** |

**Lage vii ist die schärfste** und der Grund, warum „vollständig" an der
**Registry** hängt und nicht an zwei Feldnamen: Eine Split-Klimaanlage hat
keinen Warmwasserkreis (``strom_warmwasser_kwh`` trägt ``!luft_luft``,
N-304/B5). Ein dort liegengebliebener Altwert (N-393-Klasse) darf die
Aufteilung nicht für vollständig erklären — sonst bliebe K3 für genau die Lage
verletzt, die N-451 als Erstes nennt.

## Zwei Regeln, eine Stelle

* **K3 in einer Stelle** — {@link backend.core.field_definitions.wp_strom_stufe}.
  Tag und Monat beantworten *„belegt?"* verschieden (Zuordnung gegen Zeilenwert)
  und **müssen** das; geteilt wird die **Vorrangkette**.
* **Der Abzug folgt der Stufe, nicht dem Kennzeichen** (N-462, SOLL-§9-E7/
  Option A). Fällt eine Zeile auf den Gesamtzähler zurück, steckt der Kühlstrom
  darin — wie im Nicht-getrennt-Zweig — und muss abgezogen werden. Am
  Kennzeichen festgemacht, zeigte dasselbe Gerät mit denselben Zählern **3,0
  statt 3,75**.

⚠ **Die Grenze, die bleibt** (Fall ix): Der Monat entscheidet an der **Zeile**,
der Tag an der **Zuordnung**. Ein zugeordneter feiner Zähler ohne Wert in der
Zeile lässt den Monat auf den Gesamtzähler fallen und den Tag nicht — der
Daten-Checker nennt genau diesen Monat (N-443), und K10 hält fest, dass er das
weiterhin tut.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.core.berechnungen.modus_split import ModusSplit, teilmengen_passen
from backend.core.berechnungen.tages_stapel import falte_tages_stapel
from backend.core.berechnungen.waermepumpe_kennzahl import arbeitszahl
from backend.core.betriebsmodus import HEIZEN, KUEHLEN
from backend.core.field_definitions import get_wp_strom_kwh
from backend.models import (  # noqa: F401
    Anlage, Investition, InvestitionMonatsdaten, Monatsdaten,
)
from backend.services.daten_checker import DatenChecker
from backend.services.snapshot.komponenten_beitraege import (
    investition_beitraege,
    investition_hourly_eintraege,
)

JAHR, MONAT = 2025, 6
DATUM = date(JAHR, MONAT, 10)

#: Klassische Wärmepumpe mit getrennter Strommessung — beide Achsen.
LW_F5 = {"wp_art": "luft_wasser", "getrennte_strommessung": True}
#: Split-Klimaanlage mit F5 — **eine** Achse (kein Warmwasserkreis, N-304/B5).
LL_F5 = {"wp_art": "luft_luft", "getrennte_strommessung": True}
#: Dieselbe Wärmepumpe ohne das Kennzeichen — die Referenz.
LW_AUS = {"wp_art": "luft_wasser"}

ZEILE_I = {"stromverbrauch_kwh": 500.0}
ZEILE_II = {"stromverbrauch_kwh": 1000.0, "strom_heizen_kwh": 600.0}
ZEILE_III = {"stromverbrauch_kwh": 1000.0}
ZEILE_IV = {"stromverbrauch_kwh": 1000.0, "strom_heizen_kwh": 600.0,
            "strom_warmwasser_kwh": 400.0}
ZEILE_V = {"strom_heizen_kwh": 600.0, "strom_warmwasser_kwh": 400.0}
ZEILE_VII = {"stromverbrauch_kwh": 500.0, "strom_heizen_kwh": 300.0,
             "strom_warmwasser_kwh": 120.0}
#: Lage viii — ein Warmwasser-Strom von **0,0** im Sommer ist eine Messung.
ZEILE_VIII = {"stromverbrauch_kwh": 1000.0, "strom_heizen_kwh": 600.0,
              "strom_warmwasser_kwh": 0.0}


class _Inv:
    """Das schlanke Double der Layer-Proben — Typ, Parameter, kein Parent."""

    def __init__(self, params: dict, inv_id: int = 7):
        self.id = inv_id
        self.typ = "waermepumpe"
        self.parameter = params
        self.parent_investition_id = None

    def ist_aktiv_an(self, _datum) -> bool:
        return True


def _tages_felder(params: dict, zeile: dict) -> list[str]:
    """Welche Felder trägt der **Tagespfad** für diese Lage? (die echte Tür)"""
    return [
        b.feld for b in investition_beitraege(
            _Inv(params), {}, ist_verfuegbar=lambda f: f in zeile,
        )
    ]


# ═══════════════════════════════════════════════════════════════════════════
# K1 · Stufe 1 bleibt, wie sie war — **beide Seiten**
# ═══════════════════════════════════════════════════════════════════════════


def test_k1_vollstaendige_feine_achse_verdraengt_den_gesamtzaehler():
    """**Lage iv + v: nichts ändert sich, und zwar auf beiden Seiten.**

    ⚠ Die Beitragsfelder gehören zwingend dazu. Der Monatswert allein bliebe
    auch dann 1000, wenn Stufe 1 ersatzlos gestrichen wäre — der Gesamtzähler
    trägt in Lage iv zufällig dieselbe Summe. Erst die **Feldmenge** des
    Tagespfads unterscheidet „die feine Achse trägt" von „der Gesamtzähler
    trägt", und nur so kann der Sprengsatz rot melden.
    """
    assert get_wp_strom_kwh(ZEILE_IV, LW_F5) == pytest.approx(1000.0)
    assert set(_tages_felder(LW_F5, ZEILE_IV)) == {
        "strom_heizen_kwh", "strom_warmwasser_kwh",
    }, "Stufe 1 verdrängt den Gesamtzähler — sonst zählte derselbe Strom zweimal"

    assert get_wp_strom_kwh(ZEILE_V, LW_F5) == pytest.approx(1000.0)
    assert set(_tages_felder(LW_F5, ZEILE_V)) == {
        "strom_heizen_kwh", "strom_warmwasser_kwh",
    }

    # #183 bleibt ausgeschlossen: Wo beide Funktions-Arbeitszahlen entstehen
    # können, zählt der Gesamtzähler NICHT mit.
    assert "stromverbrauch_kwh" not in _tages_felder(LW_F5, ZEILE_IV)


# ═══════════════════════════════════════════════════════════════════════════
# K2 · Stufe 2 — der Gesamtzähler, und **nichts** addiert (K1)
# ═══════════════════════════════════════════════════════════════════════════


def test_k2_der_gesamtzaehler_wird_nicht_um_die_feinen_ergaenzt():
    """**Lage iii: 1000, nicht 1000 + 0 und erst recht nicht 1000 + 600.**

    ``stromverbrauch_kwh`` ist der Zählerstand des **ganzen** Geräts — dieselbe
    Bauform wie der Nicht-getrennt-Zweig. Wird etwas addiert, ist es
    Doppelzählung.
    """
    assert get_wp_strom_kwh(ZEILE_III, LW_F5) == pytest.approx(1000.0)
    # Lage ii trägt eine feine Seite — sie darf den Gesamtwert nicht erhöhen.
    assert get_wp_strom_kwh(ZEILE_II, LW_F5) == pytest.approx(1000.0)
    assert get_wp_strom_kwh(ZEILE_II, LW_F5) != pytest.approx(1600.0)


# ═══════════════════════════════════════════════════════════════════════════
# K3 · Stufe 2 gilt auch bei **einer** vorhandenen Seite — bis in die Route
# ═══════════════════════════════════════════════════════════════════════════


async def _anlage_mit_wp(db, *, parameter: dict, zeile: dict,
                         name: str = "N-451") -> Anlage:
    """Anlage + eine Wärmepumpe mit **einer** Monatszeile (Juni 2025)."""
    anlage = Anlage(anlagenname=name, leistung_kwp=10.0,
                    installationsdatum=date(2025, 1, 1))
    db.add(anlage)
    await db.flush()
    inv = Investition(
        anlage_id=anlage.id, typ="waermepumpe", bezeichnung="WP",
        anschaffungsdatum=date(2025, 1, 1), anschaffungskosten_gesamt=20000.0,
        parameter=dict(parameter),
    )
    db.add(inv)
    await db.flush()
    db.add(InvestitionMonatsdaten(
        investition_id=inv.id, jahr=JAHR, monat=MONAT,
        verbrauch_daten=dict(zeile), source_provenance={},
    ))
    db.add(Monatsdaten(anlage_id=anlage.id, jahr=JAHR, monat=MONAT,
                       einspeisung_kwh=200.0, netzbezug_kwh=150.0))
    await db.commit()
    return anlage


async def _wp_fakt(db, anlage_id):
    from backend.services.monats_fakten import lade_monats_fakten

    fakten = await lade_monats_fakten(
        db, anlage_id, von=(JAHR, MONAT), bis=(JAHR, MONAT),
    )
    assert fakten, "Der Monat fehlt ganz — die Fixture trägt nicht."
    return fakten[0].wp


#: Die Wärme, die in allen Monats-Proben über dem Strom steht.
WAERME = {"heizenergie_kwh": 1800.0, "warmwasser_kwh": 1200.0}


async def test_k3_eine_feine_seite_genuegt_nicht_und_die_arbeitszahl_faellt_auf_3(db):
    """**Lage ii an der Route: 1000 kWh statt 600 — und 3,0 statt 5,0.**

    Die 5,0 war die Wärme **beider** Funktionen über dem Strom **einer** — R2
    wörtlich verletzt (Q und E mit verschiedener Abgrenzung), plausibel genug,
    dass kein Plausibilitäts-Prüfer anschlägt (der greift ab 7,0).
    """
    anlage = await _anlage_mit_wp(db, parameter=LW_F5, zeile={**ZEILE_II, **WAERME})
    wp = await _wp_fakt(db, anlage.id)

    assert wp.strom_kwh == pytest.approx(1000.0)
    assert wp.waerme_kwh == pytest.approx(3000.0)
    az = arbeitszahl(
        wp.waerme_kwh, wp.strom_kwh,
        strom_funktionsfremd_kwh=wp.modus_strom_funktionsfremd_abzug_kwh,
    )
    assert az.wert == pytest.approx(3.0)


async def test_k3_die_gemessene_null_bleibt_eine_messung(db):
    """**Lage viii: `is not None`, nicht truthy.**

    Ein Warmwasser-Strom von 0,0 im Sommer ist eine Messung. Mit einer
    truthy-Prüfung fiele diese Zeile auf den Gesamtzähler — 1000 statt 600 —,
    und die Anlage bekäme einen Verbrauch, den sie nicht hatte.
    """
    assert get_wp_strom_kwh(ZEILE_VIII, LW_F5) == pytest.approx(600.0)

    anlage = await _anlage_mit_wp(db, parameter=LW_F5,
                                  zeile={**ZEILE_VIII, **WAERME})
    wp = await _wp_fakt(db, anlage.id)
    assert wp.strom_kwh == pytest.approx(600.0)


# ═══════════════════════════════════════════════════════════════════════════
# K4 · **Registry-Achsen**, nicht Feldnamen — die Luft-Luft-Falle
# ═══════════════════════════════════════════════════════════════════════════


async def test_k4_ein_altwert_erklaert_die_aufteilung_nicht_fuer_vollstaendig(db):
    """**Lage vii: 500, nicht 420.**

    Die Split-Klimaanlage hat **eine** feine Achse. „Beide Feldnamen stehen in
    der Zeile" würde 300 + 120 summieren — eine Achse, die das Gerät nicht hat
    (N-304/B5) — und den Gesamtzähler verwerfen. Der Tagespfad sagt 500; alles
    andere wäre K3 in genau der Lage, die N-451 als Erstes nennt.
    """
    assert get_wp_strom_kwh(ZEILE_VII, LL_F5) == pytest.approx(500.0)
    assert get_wp_strom_kwh(ZEILE_VII, LL_F5) != pytest.approx(420.0)
    assert _tages_felder(LL_F5, ZEILE_VII) == ["stromverbrauch_kwh"]

    anlage = await _anlage_mit_wp(db, parameter=LL_F5,
                                  zeile={**ZEILE_VII, "heizenergie_kwh": 1500.0})
    wp = await _wp_fakt(db, anlage.id)
    assert wp.strom_kwh == pytest.approx(500.0)


# ═══════════════════════════════════════════════════════════════════════════
# K5 · Stufe 3 — was gemessen ist, trägt
# ═══════════════════════════════════════════════════════════════════════════


def test_k5_ohne_gesamtzaehler_traegt_die_unvollstaendige_aufteilung():
    """**Lage v und die halbe Lage i2: kein Gesamtzähler, also die feinen.**

    Sie zu verwerfen hieße den Block verschwinden zu lassen — genau der Befund
    aus #263, den Etappe 3 im Tagespfad repariert hat.
    """
    assert get_wp_strom_kwh(ZEILE_V, LW_F5) == pytest.approx(1000.0)
    assert get_wp_strom_kwh({"strom_heizen_kwh": 300.0}, LW_F5) == pytest.approx(300.0)
    assert get_wp_strom_kwh({"strom_heizen_kwh": 300.0}, LL_F5) == pytest.approx(300.0)


# ═══════════════════════════════════════════════════════════════════════════
# K6 · **S1**: Tag und Monat nennen dieselbe Zahl
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lage,params,zeile", [
    ("i", LL_F5, ZEILE_I),
    ("ii", LW_F5, ZEILE_II),
    ("iii", LW_F5, ZEILE_III),
    ("iv", LW_F5, ZEILE_IV),
    ("v", LW_F5, ZEILE_V),
    ("vi", LW_AUS, ZEILE_III),
    ("vii", LL_F5, ZEILE_VII),
])
def test_k6_tag_und_monat_nennen_dieselbe_menge(lage, params, zeile):
    """**Dieselben Zeilenzahlen durch beide Türen — SOLL §3.3/S1.**

    Der Tag liefert *Feldnamen* (er kennt die Zuordnung), der Monat eine
    *Menge*. Summiert man die Zeilenwerte der Tagesfelder, muss dieselbe Zahl
    herauskommen. Vier von sieben Lagen taten das bis zum 13.09.2026 nicht.
    """
    tag = sum(zeile[f] for f in _tages_felder(params, zeile))
    assert get_wp_strom_kwh(zeile, params) == pytest.approx(tag), (
        f"Lage {lage}: Tag {tag} ≠ Monat {get_wp_strom_kwh(zeile, params)}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# K7 · Die Teilmengen-Invariante behält den Split (N-451, Folge 1)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lage,params,zeile,bezug", [
    ("i", LL_F5, ZEILE_I, 500.0),
    ("ii", LW_F5, ZEILE_II, 1000.0),
    ("iii", LW_F5, ZEILE_III, 1000.0),
])
def test_k7_der_modus_split_wird_nicht_mehr_still_verworfen(lage, params, zeile, bezug):
    """**Σ Teilmengen ≤ Gesamt — mit einem Gesamt, das es gibt.**

    ``teilmengen_passen`` prüft gegen ``get_wp_strom_kwh``. Solange die 0 bzw.
    die Hälfte dort stand, fiel jeder Split durch die Invariante, wurde
    verworfen **und** löschte gespeicherte Altwerte
    (``modus_split_schreiben``). Der Anwender sah dann gar keine Aufteilung —
    ohne jede Meldung, denn eine verworfene Aufteilung hinterlässt keine Spur.
    """
    split = ModusSplit(
        kwh_je_modus={HEIZEN: bezug * 0.8, KUEHLEN: bezug * 0.2},
        abdeckung_h=720.0, bezug_kwh=bezug,
    )
    assert teilmengen_passen(split, get_wp_strom_kwh(zeile, params)) is True, (
        f"Lage {lage}: der Split fällt weiterhin durch die Invariante"
    )


# ═══════════════════════════════════════════════════════════════════════════
# K8 · **N-462**: der Abzug folgt der Stufe — Monatspfad
# ═══════════════════════════════════════════════════════════════════════════


#: Ein **abgeleiteter** Modus-Split in der Monatszeile: Heizen 800, Kühlen 200.
MODUS_ABGELEITET = {
    "modus_strom_heizen_kwh": 800.0,
    "modus_strom_kuehlen_kwh": 200.0,
    "modus_abdeckung_h": 720.0,
}
#: 2400 + 600 = 3000 kWh Wärme über 800 kWh Nenner ⇒ 3,75.
WAERME_A3 = {"heizenergie_kwh": 2400.0, "warmwasser_kwh": 600.0}


async def _az_mit_abzug(db, *, parameter: dict, zeile: dict, name: str):
    anlage = await _anlage_mit_wp(db, parameter=parameter, zeile=zeile, name=name)
    wp = await _wp_fakt(db, anlage.id)
    return wp, arbeitszahl(
        wp.waerme_kwh, wp.strom_kwh,
        strom_funktionsfremd_kwh=wp.modus_strom_funktionsfremd_abzug_kwh,
    )


async def test_k8_der_kuehlanteil_kuerzt_den_gesamtzaehler_auch_bei_f5(db):
    """**Lage B gegen Referenz C: 3,75 = 3,75 — vorher 3,0 gegen 3,75.**

    Dieselbe Physik, dieselben Zähler, ein Schalter Unterschied. Fällt die Zeile
    auf den Gesamtzähler zurück, steckt der Kühlstrom darin und muss abgezogen
    werden; am Kennzeichen festgemacht, wäre die Anlage 20 % schlechter
    ausgewiesen worden, als sie ist (SOLL §3.3/S1 — die Verletzung, gegen die
    E7 gebaut wurde, mit umgekehrtem Vorzeichen).
    """
    wp_b, az_b = await _az_mit_abzug(
        db, parameter=LW_F5, zeile={**ZEILE_II, **MODUS_ABGELEITET, **WAERME_A3},
        name="N-462-B",
    )
    wp_c, az_c = await _az_mit_abzug(
        db, parameter=LW_AUS, zeile={**ZEILE_III, **MODUS_ABGELEITET, **WAERME_A3},
        name="N-462-C",
    )

    assert wp_b.strom_kwh == pytest.approx(1000.0)
    assert wp_b.modus_strom_kuehlen_kwh == pytest.approx(200.0), (
        "Die MENGE bleibt unberührt — sie trägt Balken und Restmenge (K1)."
    )
    assert wp_b.modus_strom_funktionsfremd_abzug_kwh == pytest.approx(200.0)
    assert az_b.nenner_kwh == pytest.approx(800.0)
    assert az_b.wert == pytest.approx(3.75)
    assert az_c.wert == pytest.approx(3.75), "die Referenz ohne Kennzeichen"


async def test_k8_bei_vollstaendiger_feiner_achse_kuerzt_er_weiterhin_nicht(db):
    """**Die Gegenprobe, ohne die K8 nichts beweist** (Lage D, #183).

    Ist der Nenner die feine Summe, verteilt der abgeleitete Split sie nur — er
    stellt nichts daneben. Der Abzug bleibt 0, die Arbeitszahl 3,0. Genau der
    Fall, für den E7/Option A am 12.09.2026 gebaut wurde.
    """
    wp_d, az_d = await _az_mit_abzug(
        db, parameter=LW_F5, zeile={**ZEILE_IV, **MODUS_ABGELEITET, **WAERME_A3},
        name="N-462-D",
    )
    assert wp_d.strom_kwh == pytest.approx(1000.0)
    assert wp_d.modus_strom_kuehlen_kwh == pytest.approx(200.0)
    assert wp_d.modus_strom_funktionsfremd_abzug_kwh == pytest.approx(0.0)
    assert az_d.nenner_kwh == pytest.approx(1000.0)
    assert az_d.wert == pytest.approx(3.0)


# ═══════════════════════════════════════════════════════════════════════════
# K9 · **N-462**: der Abzug folgt der Stufe — Tagespfad (Bestandsdefekt)
# ═══════════════════════════════════════════════════════════════════════════


TAG_SPLIT = ModusSplit(
    kwh_je_modus={HEIZEN: 800.0, KUEHLEN: 200.0},
    abdeckung_h=24.0, bezug_kwh=1000.0,
)


def _stapel(stufe):
    return falte_tages_stapel(
        {},                                   # kein gemessener Zweig
        {"7": 1000.0},
        {"7": TAG_SPLIT},
        {"7": _Inv(LW_F5)},
        DATUM,
        stufe_je_inv=({"7": stufe} if stufe else None),
    )


def test_k9_der_tagesstapel_zieht_ab_wenn_der_bezug_der_gesamtzaehler_ist():
    """**Der Bestandsdefekt, unabhängig von N-451: 200,0 statt 0,0.**

    Seit Etappe 3 ist ``bezug_kwh`` in der F5-Lücke der **Gesamtzähler** —
    ``komponenten_kwh[waermepumpe_<id>]`` trägt ihn. ``hat_split`` kam aber
    weiter aus dem Kennzeichen, und damit blieb der Kühlstrom im Nenner:
    gemessen 3,00 statt 3,75, 20 % durch einen Schalter, der hier nichts misst.
    Klasse **N-450** — dieselben *Eingänge*, nicht nur derselbe Layer.
    """
    stapel_gesamt = _stapel("gesamt")
    assert stapel_gesamt.bezug_kwh == pytest.approx(1000.0)
    assert stapel_gesamt.kuehlen_kwh == pytest.approx(200.0), "die Menge bleibt (K1)"
    assert stapel_gesamt.funktionsfremd_abzug_kwh == pytest.approx(200.0)

    az = arbeitszahl(
        3000.0, stapel_gesamt.bezug_kwh,
        strom_funktionsfremd_kwh=stapel_gesamt.funktionsfremd_abzug_kwh,
    )
    assert az.nenner_kwh == pytest.approx(800.0)
    assert az.wert == pytest.approx(3.75)

    # Gegenprobe: ist der Bezug die feine Summe, kürzt eine Verteilung nichts.
    assert _stapel("fein").funktionsfremd_abzug_kwh == pytest.approx(0.0)


async def test_k9_die_stufe_kommt_aus_derselben_zuordnung_wie_der_tageswert(db):
    """**Die Tür, die den Tagesstapel speist** —
    ``aggregator.get_wp_strom_stufe_je_investition``.

    Sie stellt dieselbe Frage mit denselben Eingängen wie
    ``investition_beitraege``: Registry-Achsen des Geräts, Zähler über
    HA-Mapping **oder** MQTT. Ohne sie müsste die Faltung raten — und riete das
    Kennzeichen.
    """
    from backend.services.snapshot.aggregator import (
        get_wp_strom_stufe_je_investition,
    )

    async def _stufe(mapping_felder: dict) -> str:
        anlage = await _anlage_mit_wp(db, parameter=LW_F5, zeile=ZEILE_III,
                                      name=f"N-462-{sorted(mapping_felder)}")
        inv = (await db.execute(
            select(Investition).where(Investition.anlage_id == anlage.id)
        )).scalars().first()
        anlage.sensor_mapping = {"investitionen": {str(inv.id): {"felder": {
            f: {"strategie": "sensor", "sensor_id": f"sensor.{f}"}
            for f in mapping_felder
        }}}}
        await db.commit()
        stufen = await get_wp_strom_stufe_je_investition(
            db, anlage, {str(inv.id): inv},
        )
        return stufen[str(inv.id)]

    assert await _stufe({"stromverbrauch_kwh"}) == "gesamt"
    assert await _stufe({"strom_heizen_kwh"}) == "fein", (
        "Stufe 3 — ohne Gesamtzähler trägt, was gemessen ist"
    )
    assert await _stufe({"stromverbrauch_kwh", "strom_heizen_kwh"}) == "gesamt"
    assert await _stufe({
        "stromverbrauch_kwh", "strom_heizen_kwh", "strom_warmwasser_kwh",
    }) == "fein"


# ═══════════════════════════════════════════════════════════════════════════
# K10 · Die **Pflicht** der feinen Zähler bleibt (N-443)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lage,params,zeile,erwartet", [
    ("i", LL_F5, ZEILE_I, "Heizen"),
    ("ii", LW_F5, ZEILE_II, "Warmwasser"),
    ("iii", LW_F5, ZEILE_III, "Heizen"),
])
async def test_k10_der_checker_mahnt_die_fehlende_seite_unveraendert_an(
    db, lage, params, zeile, erwartet,
):
    """**Zwei verschiedene Fragen — kein Widerspruch.**

    N-451 macht den *Bilanzwert* richtig. Die *Pflicht* bleibt, weil ohne die
    getrennten Zähler die **Funktions**-Arbeitszahlen nicht entstehen können —
    der Gesamtzähler kann nicht sagen, welcher Teil wohin ging. Hinge die
    Meldung am Stromwert, verstummte sie mit diesem Bau, und der Anwender
    verlöre den einzigen Hinweis auf den fehlenden Zähler.
    """
    anlage = await _anlage_mit_wp(
        db, parameter=params, zeile={**zeile, **WAERME}, name=f"N-443-{lage}",
    )
    geladen = (await db.execute(
        select(Anlage)
        .options(selectinload(Anlage.investitionen)
                 .selectinload(Investition.monatsdaten))
        .where(Anlage.id == anlage.id)
    )).scalar_one()
    wp = next(i for i in geladen.investitionen if i.typ == "waermepumpe")
    monatsdaten = list((await db.execute(
        select(Monatsdaten).where(Monatsdaten.anlage_id == anlage.id)
    )).scalars().all())
    assert monatsdaten, "ohne Anlagen-Monatszeile prueft der Check gar nichts"

    ergebnisse = DatenChecker(db)._check_wp_monatsdaten(
        wp, wp.bezeichnung, wp.parameter, monatsdaten,
    )
    meldungen = [e.meldung for e in ergebnisse if "fehlt" in e.meldung]
    assert any(erwartet in m for m in meldungen), (
        f"Lage {lage}: erwartet eine Meldung zu Strom {erwartet}, "
        f"bekommen: {meldungen}"
    )


@pytest.mark.parametrize("lage,params,zugeordnet,erwartet_info", [
    ("iii — nur der Gesamtzähler", LW_F5, {"stromverbrauch_kwh"}, False),
    ("ii — eine feine Seite", LW_F5,
     {"stromverbrauch_kwh", "strom_heizen_kwh"}, False),
    ("iv — beide feinen Seiten", LW_F5,
     {"stromverbrauch_kwh", "strom_heizen_kwh", "strom_warmwasser_kwh"}, True),
    ("Klimaanlage — eine Achse, mehr geht nicht", LL_F5,
     {"stromverbrauch_kwh", "strom_heizen_kwh"}, False),
])
def test_k10_der_checker_nennt_den_gesamtzaehler_nicht_mehr_obsolet(
    lage, params, zugeordnet, erwartet_info,
):
    """**Ein Rat, der die Zahl gelöscht hätte** — die INFO folgt jetzt der Stufe.

    Die Meldung *„Alter Gesamt-Stromverbrauch-Sensor … obsolet"* sagt dem
    Anwender wörtlich, der Sensor werde ignoriert und beim nächsten Speichern
    des Mappings automatisch entfernt. **Gemessen** (13.09.2026): Sie feuerte an
    allen drei F5-Lagen — also auch dort, wo genau dieser Sensor seit N-451 der
    einzige Träger des Stromverbrauchs ist. Wer ihr gefolgt wäre, hätte jede
    Monats-Sicht wieder auf 0 gesetzt.

    ⚠ An einer **Split-Klimaanlage** feuert sie nie mehr: Sie hat nur eine feine
    Achse (N-304/B5), ihre Aufteilung kann nie vollständig sein — der
    Gesamtzähler ist dort immer die Wahrheit.
    """
    class _Anlage:
        sensor_mapping = {"investitionen": {"7": {"felder": {
            f: {"strategie": "sensor", "sensor_id": f"sensor.{f}"}
            for f in zugeordnet
        }}}}

    class _InvMitMapping(_Inv):
        def __init__(self):
            super().__init__(params)
            self.bezeichnung = "WP"
            self.anschaffungsdatum = date(2025, 1, 1)
            self.stilllegungsdatum = None
            self.aktiv = True
            self.anlage = _Anlage()
            self.monatsdaten = []

        def ist_aktiv_im_monat(self, _j, _m):
            return True

    class _Md:
        jahr, monat = JAHR, MONAT

    ergebnisse = DatenChecker(None)._check_wp_monatsdaten(
        _InvMitMapping(), "WP", params, [_Md()],
    )
    obsolet = [e for e in ergebnisse if "obsolet" in e.meldung]
    assert bool(obsolet) is erwartet_info, f"Lage {lage}: {[e.meldung for e in obsolet]}"


# ═══════════════════════════════════════════════════════════════════════════
# K11 · Der **Stundenpfad** zählt mit (N-461)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lage,params,zeile", [
    ("i", LL_F5, ZEILE_I),
    ("ii", LW_F5, ZEILE_II),
    ("iii", LW_F5, ZEILE_III),
    ("vii", LL_F5, ZEILE_VII),
])
def test_k11_der_stundenverlauf_ist_nicht_mehr_leer(lage, params, zeile):
    """**Dieselbe Sicht, zwei Auskünfte — bis zum 13.09.2026.**

    ``investition_hourly_eintraege`` routet seit #298 durch
    ``investition_beitraege`` (K3-treu) und warf das Ergebnis danach weg, weil
    ``_categorize_counter`` unter F5 für ``stromverbrauch_kwh`` ``None``
    lieferte. Folge: derselbe Tag zeigte in der Tagessumme den vollen Wert und
    im **Stundenverlauf** (Cockpit → Tag / Energieprofil) eine leere
    Wärmepumpen-Spalte.
    """
    eintraege = investition_hourly_eintraege(
        _Inv(params), {}, ist_verfuegbar=lambda f: f in zeile,
    )
    assert [(e.feld, e.kategorie) for e in eintraege] == [
        ("stromverbrauch_kwh", "verbrauch_wp"),
    ], f"Lage {lage}: die Stundenkategorie trägt nicht, was der Tag gewählt hat"


def test_k11_die_feinen_zaehler_bleiben_die_stundenquelle_wenn_sie_tragen():
    """**Die Gegenprobe: kein zweiter Weg zu derselben Menge.**

    Bei vollständiger feiner Achse liefert die Beitragsschicht die beiden
    Summanden — und nur die. Lieferte ``_categorize_counter`` den Gesamtzähler
    zusätzlich, stünde dieselbe Kilowattstunde zweimal in der Stunde.
    """
    eintraege = investition_hourly_eintraege(
        _Inv(LW_F5), {}, ist_verfuegbar=lambda f: f in ZEILE_IV,
    )
    assert {e.feld for e in eintraege} == {
        "strom_heizen_kwh", "strom_warmwasser_kwh",
    }
    assert all(e.kategorie == "verbrauch_wp" for e in eintraege)
