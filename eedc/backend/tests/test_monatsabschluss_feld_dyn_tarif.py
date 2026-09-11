"""Das Feld „Ø Strompreis" richtet sich nach dem Tarif DES MONATS.

`netzbezug_durchschnittspreis_cent` trägt den abgerechneten Monats-Ø eines
dynamischen Tarifs und schlägt später den Stammdaten-Arbeitspreis
(`resolve_netzbezug_preis_cent`). Angeboten wird das Feld nur bei
`vertragsart == "dynamisch"` (`BEDINGTE_BASIS_FELDER`, Bedingung
`dynamischer_tarif`).

Bis 2026-07-30 entschied darüber der HEUTE gültige Tarif: Wer von dynamisch auf
Festpreis wechselte, kam an den Ø eines Altmonats nicht mehr heran — und
umgekehrt erschien das Feld für alte Festpreis-Monate (Forum simon42
#89667/60).

⭐ **Seit 11.09.2026 (#412, OB73-gif) schaltet auch ein ZUGEORDNETER
STROMPREIS-SENSOR das Feld frei.** Die Vertragsart ist ein **optionales**
Dropdown ohne Vorbelegung; wer seinen Tibber-Sensor zuordnete, sie aber nie
umstellte, sah das Feld **nie** — und konnte seinen abgerechneten Ø auch
nachträglich nicht eintragen, während eedc die Stundenpreise längst
mitschrieb. Die Zuordnung ist das ehrlichere Signal: sie ist eine Handlung,
die Vertragsart eine Angabe, die man vergessen kann.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.api.routes.monatsabschluss.views import get_monatsabschluss
from backend.models import Anlage, Strompreis

FELD = "netzbezug_durchschnittspreis_cent"


async def _anlage_mit_tarifwechsel(db) -> int:
    """Dynamisch bis Ende 2025, danach Festpreis."""
    anlage = Anlage(anlagenname="Tarifwechsel", leistung_kwp=10.0)
    db.add(anlage)
    await db.flush()
    db.add(Strompreis(
        anlage_id=anlage.id,
        gueltig_ab=date(2024, 1, 1), gueltig_bis=date(2025, 12, 31),
        netzbezug_arbeitspreis_cent_kwh=25.0, einspeiseverguetung_cent_kwh=8.0,
        vertragsart="dynamisch",
    ))
    db.add(Strompreis(
        anlage_id=anlage.id, gueltig_ab=date(2026, 1, 1),
        netzbezug_arbeitspreis_cent_kwh=30.0, einspeiseverguetung_cent_kwh=8.0,
        vertragsart="sondervertrag",
    ))
    await db.flush()
    return anlage.id


def _feld_namen(antwort) -> set[str]:
    return {f.feld for f in antwort.basis_felder}


@pytest.mark.asyncio
async def test_alter_dyn_monat_bietet_das_feld_weiterhin_an(db):
    anlage_id = await _anlage_mit_tarifwechsel(db)

    antwort = await get_monatsabschluss(anlage_id=anlage_id, jahr=2025, monat=6, db=db)

    assert FELD in _feld_namen(antwort)


@pytest.mark.asyncio
async def test_festpreis_monat_bietet_das_feld_nicht_an(db):
    """Gegenprobe — sonst stünde in jedem Festpreis-Monat ein Feld, das dort
    nichts bewirken soll."""
    anlage_id = await _anlage_mit_tarifwechsel(db)

    antwort = await get_monatsabschluss(anlage_id=anlage_id, jahr=2026, monat=3, db=db)

    assert FELD not in _feld_namen(antwort)


async def _anlage_mit_sensor_ohne_vertragsart(db) -> int:
    """Der Fall aus #412: Preissensor zugeordnet, Vertragsart nie gesetzt.

    ⚠ **Das ist der Normalfall, nicht die Ausnahme** — `vertragsart` ist
    `nullable=True` ohne Default, und in der mitgelieferten Demo-Datenbank ist
    sie bei allen drei Tarifen leer (gemessen 11.09.2026).
    """
    anlage = Anlage(anlagenname="Tibber ohne Vertragsart", leistung_kwp=10.0)
    anlage.sensor_mapping = {
        "basis": {"strompreis": {"sensor_id": "sensor.tibber_preis"}},
    }
    db.add(anlage)
    await db.flush()
    db.add(Strompreis(
        anlage_id=anlage.id, gueltig_ab=date(2025, 1, 1),
        netzbezug_arbeitspreis_cent_kwh=30.0, einspeiseverguetung_cent_kwh=8.0,
        # vertragsart bewusst NICHT gesetzt
    ))
    await db.flush()
    return anlage.id


@pytest.mark.asyncio
async def test_zugeordneter_preissensor_schaltet_das_feld_frei(db):
    """#412: **ohne diesen Weg gibt es für den Anwender gar keinen.**"""
    anlage_id = await _anlage_mit_sensor_ohne_vertragsart(db)

    antwort = await get_monatsabschluss(anlage_id=anlage_id, jahr=2025, monat=6, db=db)

    assert FELD in _feld_namen(antwort)


@pytest.mark.asyncio
async def test_ohne_sensor_und_ohne_vertragsart_bleibt_das_feld_weg(db):
    """Gegenprobe — das Tor ist weiter geschlossen, wo es geschlossen gehört.

    Ein Festpreis-Anwender ohne Preissensor soll kein Feld sehen, das bei ihm
    nichts bewirkt (die #392-Lehre: ein Feld, das zum Falschausfüllen einlädt,
    ist schlechter als kein Feld).
    """
    anlage = Anlage(anlagenname="Nur Festpreis", leistung_kwp=10.0)
    db.add(anlage)
    await db.flush()
    db.add(Strompreis(
        anlage_id=anlage.id, gueltig_ab=date(2025, 1, 1),
        netzbezug_arbeitspreis_cent_kwh=30.0, einspeiseverguetung_cent_kwh=8.0,
    ))
    await db.flush()

    antwort = await get_monatsabschluss(anlage_id=anlage.id, jahr=2025, monat=6, db=db)

    assert FELD not in _feld_namen(antwort)
