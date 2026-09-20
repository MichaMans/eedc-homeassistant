"""Das Jahres-SOLL kennt den Zubau — ein Modul zählt erst ab seiner Anschaffung.

**Der gemessene Schaden** (Michael, 2026-09-18): Anlage „Zuhause", 5,15 kWp auf
dem Papier — aber davon hingen bis zum 08.09.2026 nur die 0,85 kWp eines
Balkonkraftwerks am Netz; der 4,3-kWp-String kam zehn Tage vor der Messung dazu.
Die Sicht „Auswertungen → Prognose" stellte dem IST von Januar bis August
trotzdem das SOLL der **vollen** Anlage gegenüber: 743 kWh gegen 3.977 kWh,
gemeldet als „−81 % · Unter Plan" in jedem einzelnen Monat. Gegen die Leistung,
die es in diesen Monaten wirklich gab (656 kWh), liegt dieselbe Anlage bei
**+13 %**.

**Warum das keine neue Formel braucht.** `services/pvgis_soll.py::soll_fuer_monat`
rechnet das seit dem 19.08.2026 richtig — es filtert auf `ist_aktiv_im_monat` und
kürzt den Anschaffungsmonat tagesgenau. Nur hatte die Funktion genau **einen**
Aufrufer (`community_service.py`); die Lesestellen der App entpackten
`pvgis.monatswerte` weiter roh. Das ist dieselbe Drift, die der Docstring der
Datei selbst protokolliert („keine dieser Stellen kürzt den Anschaffungsmonat;
das tut nur pv_strings.py") — und dieselbe Klasse wie F-34/#366, dort nur eine
Ebene tiefer (Anschaffungs-TAG statt Anschaffungs-MONAT).

⚠ Bewusst **nicht** hier: die Kürzung auf den laufenden Monat (N-69). Diese
Route liefert die Jahres-**Erwartung**, in der ein Oktober vollständig zählt,
auch wenn er noch nicht stattgefunden hat.

**Schwestern** — die drei Datums-Ebenen des SOLL, je eine Datei:

* ``test_soll_anschaffungsmonat_366.py`` — der Anschaffungs-**Tag** kürzt den
  Monat (F-34/#366). Dieselbe Klasse wie hier, eine Ebene tiefer.
* ``test_soll_anteil_laufender_monat_n69.py`` — der **Stichtag** kürzt den
  laufenden Monat (N-69). Die Ebene, die diese Route ausdrücklich NICHT bedient.
* ``test_pv_strings_pvgis_auswahl.py`` — die Sicht, die den Maßstab schon vorher
  richtig kürzte; sie ist der Grund, warum dieselbe Seite zwei Antworten gab.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.api.routes.pvgis import get_soll_je_monat
from backend.models import Anlage, Investition
from backend.models.pvgis_prognose import PVGISPrognose

# Runde Zahlen, damit die Erwartung im Kopf nachrechenbar bleibt.
BKW_JE_MONAT = 100.0     # 0,85 kWp, seit 2023-09-01
STRING_JE_MONAT = 400.0  # 4,3 kWp, seit 2026-09-08
STRING_AB = date(2026, 9, 8)


def _monatswerte(je_monat: float) -> list[dict]:
    return [{"monat": m, "e_m": je_monat} for m in range(1, 13)]


async def _seed(db, *, mit_modulaufloesung: bool = True) -> tuple[int, int, int]:
    """Michaels Aufbau: BKW seit 2023, String seit dem 08.09.2026."""
    anlage = Anlage(anlagenname="Zubau-Fall", leistung_kwp=5.15,
                    installationsdatum=date(2023, 9, 1))
    db.add(anlage)
    await db.flush()

    bkw = Investition(anlage_id=anlage.id, typ="balkonkraftwerk",
                      bezeichnung="Mein Balkonkraftwerk",
                      anschaffungsdatum=date(2023, 9, 1), leistung_kwp=0.85)
    string = Investition(anlage_id=anlage.id, typ="pv-module",
                         bezeichnung="PV-Module String 1",
                         anschaffungsdatum=STRING_AB, leistung_kwp=4.3)
    db.add_all([bkw, string])
    await db.flush()

    module_monatswerte = None
    if mit_modulaufloesung:
        module_monatswerte = {
            str(bkw.id): _monatswerte(BKW_JE_MONAT),
            str(string.id): _monatswerte(STRING_JE_MONAT),
        }
    db.add(PVGISPrognose(
        anlage_id=anlage.id, latitude=50.8, longitude=6.2,
        neigung_grad=23.0, ausrichtung_grad=0.0, gesamt_leistung_kwp=5.15,
        abgerufen_am=datetime(2026, 9, 17, 23, 39),
        jahresertrag_kwh=(BKW_JE_MONAT + STRING_JE_MONAT) * 12,
        spezifischer_ertrag_kwh_kwp=1003.0,
        monatswerte=_monatswerte(BKW_JE_MONAT + STRING_JE_MONAT),
        module_monatswerte=module_monatswerte,
        ist_aktiv=True,
    ))
    await db.commit()
    return anlage.id, bkw.id, string.id


def _soll(antwort, monat: int):
    return next(m["soll_kwh"] for m in antwort["monate"] if m["monat"] == monat)


# ============================================================================
# Der gemeldete Fall
# ============================================================================


async def test_vor_dem_zubau_traegt_nur_das_alte_geraet(db):
    """Januar–August 2026: 100 kWh (BKW), nicht 500 kWh (ganze Anlage)."""
    anlage_id, _, _ = await _seed(db)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2026, db=db)

    for monat in range(1, 9):
        assert _soll(antwort, monat) == pytest.approx(BKW_JE_MONAT), f"Monat {monat}"


async def test_zubau_monat_zaehlt_den_string_tagesgenau(db):
    """September: BKW voll + String für 23 der 30 Tage (08.–30.09. inklusive)."""
    anlage_id, _, _ = await _seed(db)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2026, db=db)

    erwartet = BKW_JE_MONAT + STRING_JE_MONAT * 23 / 30
    assert _soll(antwort, 9) == pytest.approx(erwartet, abs=0.05)


async def test_nach_dem_zubau_traegt_die_ganze_anlage(db):
    """Oktober–Dezember: beide Geräte voll — auch als Monate in der Zukunft.

    Gegenprobe zur N-69-Kürzung: diese Route liefert die Jahres-ERWARTUNG, sie
    kürzt NICHT auf den Stichtag. Ein Dezember zählt vollständig.
    """
    anlage_id, _, _ = await _seed(db)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2026, db=db)

    for monat in (10, 11, 12):
        assert _soll(antwort, monat) == pytest.approx(
            BKW_JE_MONAT + STRING_JE_MONAT), f"Monat {monat}"


async def test_jahressumme_liegt_zwischen_altgeraet_und_voller_anlage(db):
    """Das Jahr 2026 ist weder 1.200 (nur BKW) noch 6.000 (Anlage ganzjährig)."""
    anlage_id, _, _ = await _seed(db)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2026, db=db)

    erwartet = BKW_JE_MONAT * 12 + STRING_JE_MONAT * (3 + 23 / 30)
    assert antwort["jahr_kwh"] == pytest.approx(erwartet, abs=0.1)
    assert 1200.0 < antwort["jahr_kwh"] < 6000.0


# ============================================================================
# Die Kanten
# ============================================================================


async def test_jahr_vor_der_anlage_hat_gar_kein_soll(db):
    """2022: kein Erzeuger aktiv ⇒ `None` je Monat, kein 0-Wert.

    `None` heißt „kein Maßstab"; eine 0 hieße „0 kWh erwartet" und ergäbe eine
    SOLL-Erfüllung von 0 % für ein Jahr, das es nicht gab.
    """
    anlage_id, _, _ = await _seed(db)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2022, db=db)

    assert all(m["soll_kwh"] is None for m in antwort["monate"])
    assert antwort["jahr_kwh"] is None


async def test_anschaffungsjahr_des_bkw_beginnt_im_september(db):
    """2023: Januar–August ohne SOLL, September–Dezember mit BKW-SOLL."""
    anlage_id, _, _ = await _seed(db)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2023, db=db)

    assert all(_soll(antwort, m) is None for m in range(1, 9))
    # 01.09. ist der erste Tag des Monats ⇒ ungekürzt.
    assert _soll(antwort, 9) == pytest.approx(BKW_JE_MONAT)
    assert _soll(antwort, 12) == pytest.approx(BKW_JE_MONAT)


async def test_ohne_modulaufloesung_greift_die_benannte_naeherung(db):
    """Fällt `module_monatswerte` weg, kürzt der SoT anlagenweit am frühesten Gerät.

    Dann trägt Januar 2026 das **volle** Anlagen-SOLL (500), weil das früheste
    Gerät seit 2023 läuft — die Näherung, die `soll_fuer_monat` im Docstring
    ausdrücklich benennt. Der Test hält fest, dass wir sie bekommen und nicht
    still etwas anderes.
    """
    anlage_id, _, _ = await _seed(db, mit_modulaufloesung=False)
    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2026, db=db)

    assert _soll(antwort, 1) == pytest.approx(BKW_JE_MONAT + STRING_JE_MONAT)


async def test_ohne_aktive_prognose_kein_soll(db):
    """P5: wer alle Prognosen deaktiviert, bekommt keine SOLL-Werte."""
    anlage_id, _, _ = await _seed(db)
    from sqlalchemy import select
    result = await db.execute(select(PVGISPrognose).where(
        PVGISPrognose.anlage_id == anlage_id))
    for p in result.scalars().all():
        p.ist_aktiv = False
    await db.commit()

    antwort = await get_soll_je_monat(anlage_id=anlage_id, jahr=2026, db=db)
    assert all(m["soll_kwh"] is None for m in antwort["monate"])
    assert antwort["jahr_kwh"] is None
