"""Der Wärme/Klima-Verlauf je Tag — die Reihe hinter *Cockpit → Monat*.

**Was hier entsteht** (Konzept Wärme/Klima §8, Bauschnitt 4): je Tag eines
Zeitraums die drei Größen, die der Verlauf zeigt —

* **Strom, nach Betriebsart gestapelt** (beide Zweige, ``falte_tages_stapel``),
* **gemessene Wärme** als Linie,
* das **Tagesmittel der Außentemperatur** als zweite Achse.

⭐ **Es ist derselbe Stapel wie in Cockpit → Tag, nicht ein zweiter.** Die
Zusammenführung der beiden Zweige (gemessene Betriebsart-Zähler schlagen den aus
dem Betriebsmodus abgeleiteten Split, je Gerät — SOLL §6.1/F4, Invariante K2)
liegt seit dem 10.09.2026 im Layer und wird hier **gerufen**, nicht nachgebaut.
Ohne das hätte eine Anlage mit Betriebsart-Zählern im Monat einen anderen Stapel
gezeigt als in Tag und Jahr daneben — die S1-Verletzung (*dieselbe Größe, überall
derselbe Wert*).

⚠ **Die Grundmenge des Stapels ist nicht der Wärmepumpen-Strom.** Sie ist die Σ
der Bezugsmengen der Geräte **mit** Aufteilung (``bezug_kwh``), und sie kommt aus
dem **Zählerpfad** (``TagesZusammenfassung.komponenten_kwh``) — nicht aus der
Stundensumme des Leistungspfads. Beide weichen ab, und genau daran hängt
**W-17b**: dietmar1968 sah 30 kWh Balken unter einer 284-kWh-Kachel. Der Verlauf
nennt die Differenz mit derselben Zeile wie der Balken darunter, statt eine
zweite Antwort zu erfinden.

⛔ **Gezeichnet wird nur GEMESSENE Wärme** (SOLL §3.3/S4). Auf Tagesebene ist das
ohnehin die einzige, die es gibt: Die abgeleitete Wärme entsteht aus
``Strom × Arbeitszahl`` an den **Monatszeilen** (``imd_monatsaggregat``), und die
kennt der Tag nicht. Ein Tag ohne Wärmemengenzähler trägt hier deshalb **nichts**
— eine Lücke in der Linie, keine Null.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.berechnungen import waermepumpe_kwh_je_investition
from backend.core.berechnungen.tages_stapel import TagesStapel, falte_tages_stapel
from backend.models.tages_energie_profil import TagesZusammenfassung
from backend.services.energie_profil.modus_split_monat import lade_modus_split_je_tag
from backend.services.mitteltemperatur import lade_tagesmittel_temperatur
from backend.services.snapshot.aggregator import (
    TAGESDETAIL_AUSGABE,
    WAERME_AUSGABE_KEYS,
    get_betriebsart_strom_tageswerte,
)
from backend.services.snapshot.bereichs_leser import lade_tageswerte_je_feld

#: Die Wärme-Felder, die der Verlauf als Linie zeichnet — ein **Ausschnitt** aus
#: der einen Feldtabelle, keine zweite Liste.
#:
#: ⛔ Hier stand bis 11.09.2026 „Bauschnitt 6 erweitert … diese Menge". Das war
#: falsch: Kälte ist eine **eigene Rolle** (Konzept §8) und bekommt ihren
#: eigenen Ausschnitt darunter — in dieser Menge flösse sie in die Wärme-Linie.
_WAERME_FELDER = {
    schluessel: key
    for schluessel, key in TAGESDETAIL_AUSGABE.items()
    if key in WAERME_AUSGABE_KEYS
}
#: Die Kälte (Bauschnitt 6b) — Gerätefeld oder Σ Innengeräte, dieselbe Regel
#: wie im Tagespfad (der Bereichs-Leser löst den Suffix seit 6a auf).
_KAELTE_KEY = "wp_kaelte_kwh"
_KAELTE_FELDER = {
    schluessel: key
    for schluessel, key in TAGESDETAIL_AUSGABE.items()
    if key == _KAELTE_KEY
}


@dataclass(frozen=True)
class WaermeVerlaufTag:
    """Eine Tageszeile des Verlaufs."""

    datum: date
    stapel: TagesStapel
    #: Σ der **gemessenen** Wärme des Tages (Heizung + Warmwasser), oder
    #: ``None``: keine Aussage, keine 0.
    waerme_kwh: Optional[float]
    #: Die **gemessene Kälte** des Tages, oder ``None``. Nie Teil der Wärme.
    kaelte_kwh: Optional[float]
    #: Tagesmittel der Außentemperatur (°C), oder ``None``.
    temperatur_c: Optional[float]
    #: Der gesamte Wärmepumpen-Strom des Tages aus dem **Zählerpfad** — der
    #: Bezug für die Zeile „Aufgeteilte Menge X von Y kWh".
    strom_kwh: Optional[float]


async def lade_waerme_verlauf(
    db: AsyncSession,
    anlage,
    investitionen_by_id: dict,
    von: date,
    bis: date,
) -> list[WaermeVerlaufTag]:
    """Die Tagesreihe für ``[von, bis]`` (einschließlich), aufsteigend.

    **Vier Bereichs-Abfragen statt einer Schleife über Tage:** Modus-Split
    (zwei Queries), Zählerstrom je Tag, Temperatur, und je Wärmefeld eine
    Standreihe. Ein Tag, der nichts beiträgt, fehlt in der Liste — der Aufrufer
    entscheidet, ob er ihn als Lücke zeichnet.
    """
    splits_je_tag = await lade_modus_split_je_tag(db, anlage.id, von=von, bis=bis)
    zaehler_je_tag, rueckwaerts_tage = await _zaehlerstrom_je_tag(
        db, anlage.id, von, bis,
    )
    # N-435: die Wärme je Tag im Fenster derselben Tageszeile — sonst nennte die
    # Monatssäule eine andere Wärme als *Cockpit → Tag* für denselben Tag.
    # Bauschnitt 6b: Wärme UND Kälte in EINEM Satz Bereichsabfragen — dasselbe
    # Fenster je Tag. Getrennt wird danach nach Key, nie über die Summe.
    nutzenergie_je_tag = await lade_tageswerte_je_feld(
        db, anlage, investitionen_by_id, von, bis,
        {**_WAERME_FELDER, **_KAELTE_FELDER},
        rueckwaerts_tage=rueckwaerts_tage,
    )
    temperatur_je_tag = await lade_tagesmittel_temperatur(db, anlage.id, von, bis)

    # ⚠ Zweig 1 (gemessene Betriebsart-Zähler) ist selbst snapshot-basiert und
    # hat heute nur einen Tages-Einstieg. Er wird deshalb je Tag gerufen — aber
    # **nur für Tage, die überhaupt eine Zeile haben**, statt für jeden
    # Kalendertag des Monats.
    tage = sorted(
        set(splits_je_tag) | set(zaehler_je_tag) | set(nutzenergie_je_tag)
    )

    zeilen: list[WaermeVerlaufTag] = []
    for tag in tage:
        if not (von <= tag <= bis):
            continue
        # N-434: dasselbe Fenster wie der Bezug dieses Tages — die Herkunft der
        # Tageszeile entscheidet, nicht eine Voreinstellung.
        gemessen_je_inv = await get_betriebsart_strom_tageswerte(
            db, anlage, investitionen_by_id, tag,
            rueckwaerts=tag in rueckwaerts_tage,
        )
        zaehler = zaehler_je_tag.get(tag, {})
        stapel = falte_tages_stapel(
            gemessen_je_inv,
            zaehler,
            splits_je_tag.get(tag, {}),
            investitionen_by_id,
            tag,
        )
        werte = nutzenergie_je_tag.get(tag, {})
        # ⛔ Nie `sum(werte.values())` — seit 6b steht dort auch die Kälte.
        waerme_teile = [v for k, v in werte.items() if k in WAERME_AUSGABE_KEYS]
        waerme = sum(waerme_teile) if waerme_teile else None
        kaelte = werte.get(_KAELTE_KEY)
        strom = sum(zaehler.values()) if zaehler else None
        if stapel.ist_leer and waerme is None and kaelte is None:
            # Ein Tag ohne Aufteilung und ohne gemessene Wärme hat für den
            # Verlauf nichts zu sagen (ADR-002/P4) — die Temperatur allein
            # macht keine Zeile.
            continue
        zeilen.append(WaermeVerlaufTag(
            datum=tag,
            stapel=stapel,
            waerme_kwh=round(waerme, 2) if waerme else None,
            kaelte_kwh=round(kaelte, 2) if kaelte else None,
            temperatur_c=temperatur_je_tag.get(tag),
            strom_kwh=round(strom, 2) if strom else None,
        ))
    return zeilen


async def _zaehlerstrom_je_tag(
    db: AsyncSession, anlage_id: int, von: date, bis: date,
) -> tuple[dict[date, dict[str, float]], set[date]]:
    """Der Wärmepumpen-Tagesstrom je Gerät — und in welchem Fenster er steht.

    ⚠ **Hier stand bis 11.09.2026 „nicht die Stundensumme des Leistungspfads
    (``TagesBilanz.wp_strom_kwh``)"** — das war falsch benannt: ``waermepumpe_kw``
    kommt aus dem **Zählerpfad** im Rückwärts-Raster (``get_hourly_kwh_by_category``
    bzw. die LTS-Variante), nicht aus dem Leistungspfad. Der Unterschied zu
    ``komponenten_kwh`` ist das **Fenster** (und im Snapshot-Rückfall Lückenfüllung
    und Reset-Regel), nicht die Quelle. Die Wahl dieser Quelle bleibt richtig:
    Kachel und Aufteilung darunter rechnen mit ihr.

    ⛔ **N-434:** Im HA-Add-on ist ``komponenten_kwh`` Σ der 24 LTS-Slots, also
    [Vortag 23:00, 23:00). Deshalb meldet diese Funktion zusätzlich die Tage,
    deren Zeile dieses Fenster trägt — die Betriebsart-Zähler desselben Tages
    werden im selben Fenster gelesen.

    Returns:
        ``(je_tag, rueckwaerts_tage)``.
    """
    from backend.services.snapshot.boundary_range import tageszeile_ist_rueckwaerts

    result = await db.execute(
        select(
            TagesZusammenfassung.datum,
            TagesZusammenfassung.komponenten_kwh,
            TagesZusammenfassung.source_provenance,
        )
        .where(and_(
            TagesZusammenfassung.anlage_id == anlage_id,
            TagesZusammenfassung.datum >= von,
            TagesZusammenfassung.datum <= bis,
        ))
    )
    je_tag: dict[date, dict[str, float]] = {}
    rueckwaerts_tage: set[date] = set()
    for datum, komponenten, provenance in result.all():
        if tageszeile_ist_rueckwaerts(provenance):
            rueckwaerts_tage.add(datum)
        if komponenten:
            werte = waermepumpe_kwh_je_investition(komponenten)
            if werte:
                je_tag[datum] = werte
    return je_tag, rueckwaerts_tage
