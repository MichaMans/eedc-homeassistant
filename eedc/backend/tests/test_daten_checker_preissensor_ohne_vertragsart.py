"""Preissensor zugeordnet, Vertragsart nicht gesetzt — der Checker sagt es (#412).

Schwesterdateien: `test_daten_checker_strompreis_gleichstand.py` (dieselbe
Kategorie), `test_monatsabschluss_feld_dyn_tarif.py` (das Feld, das dadurch
erscheint), `test_aufgeloester_monatspreis_kaskade.py` (was eedc stattdessen
rechnet).

**Der Fall.** `Strompreis.vertragsart` ist ein optionales Dropdown ohne
Vorbelegung — in der mitgelieferten Demo-Datenbank ist es bei allen Tarifen
leer. Wer einen Tibber-/aWATTar-Sensor zuordnet, die Vertragsart aber nie
umstellt, sah bis 11.09.2026 das Feld „Ø Strompreis" im Monatsabschluss **nie**
und konnte seinen abgerechneten Ø auch nachträglich nicht eintragen.

⚠ **INFO, nicht WARNING.** eedc rechnet seit #412 auch ohne die Angabe mit den
gemessenen Stundenpreisen — die Vertragsart ändert an keiner Zahl mehr etwas.
Ein Mangel-Ton wäre hier falsch (dieselbe Lehre wie beim Wärmestrom-Hinweis,
dietmar1968 #89667/87: der Hinweis beschreibt einen **Zustand**, statt einen
Mangel zu behaupten).
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.models import Anlage, Strompreis
from backend.services.daten_checker.kategorien import CheckKategorie, CheckSeverity
from backend.services.daten_checker.stammdaten import StammdatenChecks

MELDUNG = "Strompreis-Sensor zugeordnet"


def _anlage(*, mit_sensor: bool, vertragsart=None) -> Anlage:
    anlage = Anlage(anlagenname="Preis", leistung_kwp=10.0)
    anlage.sensor_mapping = (
        {"basis": {"strompreis": {"sensor_id": "sensor.tibber"}}} if mit_sensor else {}
    )
    anlage.strompreise = [Strompreis(
        gueltig_ab=date(2025, 1, 1), verwendung="allgemein",
        netzbezug_arbeitspreis_cent_kwh=30.0, einspeiseverguetung_cent_kwh=8.0,
        vertragsart=vertragsart,
    )]
    anlage.investitionen = []
    return anlage


def _meldungen(anlage) -> list:
    ergebnisse = StammdatenChecks()._check_strompreise(anlage)
    return [e for e in ergebnisse if MELDUNG in e.meldung]


def test_sensor_ohne_vertragsart_wird_gemeldet():
    treffer = _meldungen(_anlage(mit_sensor=True))

    assert len(treffer) == 1
    assert treffer[0].schwere == CheckSeverity.INFO
    assert treffer[0].kategorie == CheckKategorie.STROMPREISE


def test_die_meldung_sagt_dass_nichts_zu_tun_ist():
    """⚠ Der Ton ist der Gegenstand, nicht nur das Vorkommen.

    Wer liest, dass etwas „fehlt", sucht einen Fehler. Hier fehlt nichts —
    eedc rechnet bereits richtig.
    """
    treffer = _meldungen(_anlage(mit_sensor=True))

    assert "nötig ist das nicht" in treffer[0].details
    assert "Stundenpreise" in treffer[0].details


def test_mit_gesetzter_vertragsart_schweigt_der_checker():
    assert _meldungen(_anlage(mit_sensor=True, vertragsart="dynamisch")) == []


def test_ohne_sensor_schweigt_der_checker():
    """Gegenprobe — ein Festpreis-Anwender geht das nichts an."""
    assert _meldungen(_anlage(mit_sensor=False)) == []
