"""Zwei Kanten in EINEM Monat — Zubau und Stichtag überlagern sich.

**Der Fall** (Michael, 2026-09-19): Der 4,3-kWp-String kam am **08.09.** dazu,
der Monat ist am **19.09.** erst zur Hälfte um. Beide Gründe kürzen denselben
September, aber an **verschiedenen Enden**:

    September      1 ─────── 8 ─────── 19 ─────── 30
    String ab dem 8.         ████████████████████    23 Tage
    Monat bis zum 19.  ██████████████               19 Tage
    beides erfüllt:          ▓▓▓▓▓▓▓▓                12 Tage  ← das ist der Wert

**Warum man das nicht nacheinander rechnen darf.** Der naheliegende Weg —
erst das Gerätefenster, dann das Ergebnis mit dem Monatsfenster skalieren —
multipliziert die beiden Anteile (23/30 × 19/30 = 0,486) statt sie zu schneiden
(12/30 = 0,400). Für den String sind das **21 % zu viel**. Auch „das kleinere
der beiden Fenster nehmen" trifft es nicht: das wären 19 Tage, nicht 12.

**Die Lösung braucht keinen Umbau.** ``monatsfenster_investition`` bildet
intern ``von = max(Monatsanfang, ab)`` und ``nach = min(Monatsende, bis)`` —
das **ist** der Schnitt, sobald man ihm beide Kanten als **Datum** gibt statt
zwei fertige Fenster zu verrechnen. ``soll_fuer_monat`` bekommt dafür
``bis_stichtag``; je Gerät wird die obere Kante zu ``min(Stilllegung, Stichtag)``.

⚠ ``bis_stichtag`` ist **optional und wirkt nur, wo es übergeben wird**. Der
Gemeinschaftsdatensatz (`community_service.py`) und die Jahres-Route
(`/pvgis/soll/{id}/{jahr}`) rufen weiterhin ohne — sie liefern die
Monats-**Erwartung**, in der ein angefangener Monat vollständig zählt.

**Schwestern:**

* ``test_soll_je_monat_zubau.py`` — dieselbe Geräte-Kante ohne Stichtag
  (Jahres-Route, Auswertungen → Prognose).
* ``test_soll_anteil_laufender_monat_n69.py`` — der Stichtag allein (N-69).
* ``test_soll_anschaffungsmonat_366.py`` — die Anschaffungs-Kante allein (F-34/#366).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.api.routes.aktueller_monat import _load_soll_pv
from backend.models import Anlage, Investition
from backend.models.pvgis_prognose import PVGISPrognose
from backend.services.pvgis_soll import lade_erzeuger, lade_soll_quelle, soll_fuer_monat

BKW_JE_MONAT = 300.0     # seit 2023-09-01
STRING_JE_MONAT = 600.0  # seit 2026-09-08
STRING_AB = date(2026, 9, 8)
STICHTAG = date(2026, 9, 19)

# September hat 30 Tage.
BKW_BIS_STICHTAG = BKW_JE_MONAT * 19 / 30        # 190,0
STRING_BIS_STICHTAG = STRING_JE_MONAT * 12 / 30  # 240,0 — der Schnitt 08.–19.
STRING_GANZER_MONAT = STRING_JE_MONAT * 23 / 30  # 460,0 — nur die Geräte-Kante


def _monatswerte(je_monat: float) -> list[dict]:
    return [{"monat": m, "e_m": je_monat} for m in range(1, 13)]


async def _seed(db) -> int:
    anlage = Anlage(anlagenname="Zubau im laufenden Monat", leistung_kwp=5.15,
                    installationsdatum=date(2023, 9, 1))
    db.add(anlage)
    await db.flush()

    bkw = Investition(anlage_id=anlage.id, typ="balkonkraftwerk", bezeichnung="BKW",
                      anschaffungsdatum=date(2023, 9, 1), leistung_kwp=0.85)
    string = Investition(anlage_id=anlage.id, typ="pv-module", bezeichnung="String 1",
                         anschaffungsdatum=STRING_AB, leistung_kwp=4.3)
    db.add_all([bkw, string])
    await db.flush()

    db.add(PVGISPrognose(
        anlage_id=anlage.id, latitude=50.8, longitude=6.2,
        neigung_grad=23.0, ausrichtung_grad=0.0, gesamt_leistung_kwp=5.15,
        abgerufen_am=datetime(2026, 9, 17, 23, 39),
        jahresertrag_kwh=(BKW_JE_MONAT + STRING_JE_MONAT) * 12,
        spezifischer_ertrag_kwh_kwp=1003.0,
        monatswerte=_monatswerte(BKW_JE_MONAT + STRING_JE_MONAT),
        module_monatswerte={
            str(bkw.id): _monatswerte(BKW_JE_MONAT),
            str(string.id): _monatswerte(STRING_JE_MONAT),
        },
        ist_aktiv=True,
    ))
    await db.commit()
    return anlage.id


# ============================================================================
# Die Formel — der Schnitt, nicht das Produkt
# ============================================================================


async def test_zubau_und_stichtag_im_selben_monat_schneiden_sich(db):
    """September bis zum 19.: BKW 19 Tage + String 12 Tage (nicht 23, nicht 19)."""
    anlage_id = await _seed(db)
    quelle = await lade_soll_quelle(db, anlage_id)
    erzeuger = await lade_erzeuger(db, anlage_id)

    ergebnis = soll_fuer_monat(quelle, erzeuger, 2026, 9, bis_stichtag=STICHTAG)

    assert ergebnis == pytest.approx(BKW_BIS_STICHTAG + STRING_BIS_STICHTAG, abs=0.05)


async def test_der_naive_weg_waere_messbar_zu_hoch(db):
    """Gegenprobe: erst Geräte-Kante, dann Monatsfenster — 21 % zu viel am String.

    Diese Probe hält die ZAHL fest, die der falsche Weg liefert. Sie ist der
    Grund, warum `bis_stichtag` in den SoT wandert, statt am Aufrufer aus zwei
    fertigen Fenstern zusammengesetzt zu werden.
    """
    anlage_id = await _seed(db)
    quelle = await lade_soll_quelle(db, anlage_id)
    erzeuger = await lade_erzeuger(db, anlage_id)

    richtig = soll_fuer_monat(quelle, erzeuger, 2026, 9, bis_stichtag=STICHTAG)
    monatserwartung = soll_fuer_monat(quelle, erzeuger, 2026, 9)
    naiv = monatserwartung * 19 / 30  # (BKW + String×23/30) × 19/30

    assert naiv > richtig
    assert naiv == pytest.approx(481.3, abs=0.5)
    assert richtig == pytest.approx(430.0, abs=0.5)


async def test_ohne_stichtag_bleibt_es_die_monatserwartung(db):
    """Ohne `bis_stichtag` zählt der ganze September — nur die Geräte-Kante wirkt."""
    anlage_id = await _seed(db)
    quelle = await lade_soll_quelle(db, anlage_id)
    erzeuger = await lade_erzeuger(db, anlage_id)

    ergebnis = soll_fuer_monat(quelle, erzeuger, 2026, 9)

    assert ergebnis == pytest.approx(BKW_JE_MONAT + STRING_GANZER_MONAT, abs=0.05)


async def test_abgeschlossener_monat_ist_vom_stichtag_unberuehrt(db):
    """Oktober, Stichtag im Dezember: beide Geräte voll — die Kante greift nicht."""
    anlage_id = await _seed(db)
    quelle = await lade_soll_quelle(db, anlage_id)
    erzeuger = await lade_erzeuger(db, anlage_id)

    ergebnis = soll_fuer_monat(quelle, erzeuger, 2026, 10, bis_stichtag=date(2026, 12, 1))

    assert ergebnis == pytest.approx(BKW_JE_MONAT + STRING_JE_MONAT, abs=0.05)


async def test_monat_in_der_zukunft_hat_kein_gekuerztes_soll(db):
    """Dezember am 19.09.: null Tage vergangen ⇒ `None`, nicht 0."""
    anlage_id = await _seed(db)
    quelle = await lade_soll_quelle(db, anlage_id)
    erzeuger = await lade_erzeuger(db, anlage_id)

    assert soll_fuer_monat(quelle, erzeuger, 2026, 12, bis_stichtag=STICHTAG) is None
    # Ohne Stichtag ist derselbe Monat sehr wohl bewertbar (Jahres-Erwartung).
    assert soll_fuer_monat(quelle, erzeuger, 2026, 12) is not None


# ============================================================================
# Die Route — Cockpit/Monat + Jahr
# ============================================================================


async def test_cockpit_vor_dem_zubau_misst_die_kleine_anlage(db):
    """Mai 2026: 300 kWh (BKW), nicht 900 (ganze Anlage) — der gemeldete Schaden."""
    anlage_id = await _seed(db)

    soll = await _load_soll_pv(anlage_id, 2026, 5, db, heute=STICHTAG)

    assert soll.monat == pytest.approx(BKW_JE_MONAT, abs=0.05)
    assert soll.anteilig == pytest.approx(BKW_JE_MONAT, abs=0.05)  # Mai ist rum


async def test_cockpit_traegt_beide_kanten_im_zubau_monat(db):
    """September am 19.: `anteilig` schneidet, `monat` trägt die Monatserwartung."""
    anlage_id = await _seed(db)

    soll = await _load_soll_pv(anlage_id, 2026, 9, db, heute=STICHTAG)

    assert soll.anteilig == pytest.approx(BKW_BIS_STICHTAG + STRING_BIS_STICHTAG, abs=0.05)
    assert soll.monat == pytest.approx(BKW_JE_MONAT + STRING_GANZER_MONAT, abs=0.05)
    # Die zwei Lesarten sind im angefangenen Monat verschieden — genau dafür gibt
    # es sie (T89667 #155): die Quote rechnet gegen `anteilig`, der
    # Fortschritts-Bezug steht mit `monat` daneben.
    assert soll.anteilig < soll.monat


async def test_cockpit_ohne_aktive_prognose_liefert_nichts(db):
    """P5: alle Prognosen deaktiviert ⇒ beide Lesarten `None`."""
    anlage_id = await _seed(db)
    from sqlalchemy import select
    result = await db.execute(select(PVGISPrognose).where(PVGISPrognose.anlage_id == anlage_id))
    for p in result.scalars().all():
        p.ist_aktiv = False
    await db.commit()

    soll = await _load_soll_pv(anlage_id, 2026, 9, db, heute=STICHTAG)

    assert soll.anteilig is None
    assert soll.monat is None
