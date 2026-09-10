"""Tageswerte kumulativer Zähler über einen **Bereich** — eine Reihe statt N Abfragen.

**Warum es diesen Leser gibt** (10.09.2026, Konzept Wärme/Klima §8,
Bauschnitt 4). Der Wärme/Klima-Verlauf in *Cockpit → Monat* braucht die
gemessene Wärme **je Tag** über einen ganzen Monat. Der bestehende Weg dorthin
ist ``get_tagesdetail_kwh`` — und der ist pro Tag teuer:

* zwei ``get_snapshot``-Abfragen je Feld (die beiden Tagesränder),
* dazu die Standreihe für die Monotonie-Prüfung,

also **drei** Datenbankabfragen je Feld und Tag. Für einen Monat mit einer
Wärmepumpe und zwei Wärmefeldern sind das ~186 Abfragen für **eine** Linie in
einem Diagramm. Dieser Leser lädt stattdessen **eine** Standreihe je Feld über
das ganze Fenster und wertet lokal aus.

⭐ **Die Regel selbst wird geteilt, nicht kopiert.** Die Tagesreset-Behandlung
steht in ``reader.tageswert_aus_reihe``, die Feldmenge in
``aggregator.TAGESDETAIL_AUSGABE``. Beides ist **wegen dieses Lesers** dorthin
gezogen worden: Ohne das hätte er die Regel ein drittes Mal und die Feldliste
ein zweites Mal getragen — und **Bauschnitt 6** (Kälte je Tag) müsste danach an
zwei Stellen gebaut werden.

⚠ **Die Ränder überlappen, und das ist der eigentliche Gewinn:** Das Ende von
Tag N ist der Anfang von Tag N+1. Für 31 Tage braucht es 32 Randstände je Feld,
nicht 62.

## Self-Healing — heilen ja, aber nur die Lücken

``get_snapshot`` hat eine dreistufige Kaskade: lokale Snapshot-Tabelle → HA
Langzeitstatistik → MQTT-Backup; was sie findet, **schreibt sie zurück**. Dieser
Leser deckt Stufe 1 selbst ab und ruft die Kaskade nur für die Ränder, die lokal
fehlen.

⛔ **Warum nicht ganz darauf verzichten** — das war der erste Entwurf und er war
falsch begründet („bis zu 62 HA-Abrufe je Aufruf"): Der geheilte Wert wird
**upsertet**, es ist also ein Einmal-Preis, kein Dauerzustand. Und es gibt
Lagen, in denen lokale Stände systematisch fehlen — allen voran der **Monat, in
dem der Anwender den Zähler zugeordnet hat**: Snapshots entstehen erst ab der
Zuordnung, die Monatswerte davor kommen aus der HA-Langzeitstatistik. Ohne
Heilung bräche die Linie ausgerechnet dort, wo der Verlauf zum ersten Mal
geöffnet wird — und Cockpit → Tag zeigte daneben einen Wert. Das ist die
S1-Verletzung (*dieselbe Größe, überall derselbe Wert*), also die v4.0.1-Klasse.

⚠ **Und die Heilung läuft über ``asyncio.to_thread``.** ``get_value_at`` ist
synchrones SQLAlchemy gegen die Recorder-Datenbank; direkt im ``async def``
gerufen hält es den Event-Loop an. Der Snapshot-**Writer** hat diese Lehre seit
Langem und schreibt sie in seinen eigenen Kommentar (*„hielt den Event-Loop von
eedc an — je Zähler, je Lauf"*); der Reader hat sie nicht, und über einen ganzen
Monat wöge das schwerer als über einen Tag.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.sensor_snapshot import SensorSnapshot
from backend.services.snapshot.keys import (
    extract_quellen_energy,
    feld_hat_zaehler,
)
from backend.services.snapshot.reader import (
    get_snapshot,
    mqtt_zaehler_keys,
    tageswert_aus_reihe,
)

logger = logging.getLogger(__name__)

#: Toleranz beim Zuordnen eines Snapshots zu einer Tagesgrenze (Minuten) —
#: dieselbe wie in ``get_snapshot``. Stundenzeilen liegen exakt auf :00, die
#: Toleranz deckt Latenz-Jitter ab.
RAND_TOLERANZ_MINUTEN = 5


def _tagesgrenzen(von: date, bis: date) -> list[datetime]:
    """Die Randzeitpunkte für ``[von, bis]`` — ein Rand mehr als Tage.

    ``bis`` ist **einschließlich**; der letzte Rand ist deshalb der Beginn des
    Folgetags.
    """
    tage = (bis - von).days + 1
    return [
        datetime.combine(von, time.min) + timedelta(days=i) for i in range(tage + 1)
    ]


def _naechster_stand(
    reihe: list[tuple[datetime, float]], ziel: datetime,
) -> Optional[float]:
    """Der Stand, der ``ziel`` am nächsten liegt — innerhalb der Toleranz.

    Dieselbe Auswahl, die ``get_snapshot`` in SQL trifft (``ORDER BY
    abs(zeitpunkt − ziel) LIMIT 1``), nur lokal auf der geladenen Reihe.
    """
    grenze = timedelta(minutes=RAND_TOLERANZ_MINUTEN)
    bester: Optional[tuple[timedelta, float]] = None
    for ts, wert in reihe:
        abstand = abs(ts - ziel)
        if abstand > grenze:
            continue
        if bester is None or abstand < bester[0]:
            bester = (abstand, wert)
    return bester[1] if bester else None


async def lade_tageswerte_je_feld(
    db: AsyncSession,
    anlage,
    investitionen_by_id: dict,
    von: date,
    bis: date,
    felder: dict[tuple[str, str], str],
) -> dict[date, dict[str, float]]:
    """Tages-kWh je Ausgabe-Key über ``[von, bis]`` — eine Reihe je Feld.

    Args:
        felder: Ausschnitt aus ``aggregator.TAGESDETAIL_AUSGABE``,
            ``(typ, mapping-feld) → ausgabe_key``. Der Aufrufer wählt die
            Teilmenge, die er braucht; die **Namen** kommen aus der einen
            Tabelle.

    Returns:
        ``{datum: {ausgabe_key: kwh}}``. Ein Tag ohne verwertbaren Wert fehlt
        — ebenso ein Tag, dessen Zähler zurückgesprungen ist. **Keine 0 als
        Platzhalter** (ADR-002/P4: keine Aussage statt einer Zahl, die wie eine
        Messung aussieht).
    """
    sensor_mapping = anlage.sensor_mapping or {}
    investitionen_map = sensor_mapping.get("investitionen", {}) or {}
    quellen_energy = extract_quellen_energy(anlage)
    mqtt_keys = await mqtt_zaehler_keys(db, anlage.id)
    grenzen = _tagesgrenzen(von, bis)

    ergebnis: dict[date, dict[str, float]] = {}

    for inv_id_str, inv in investitionen_by_id.items():
        if inv is None:
            continue
        typ = getattr(inv, "typ", None)
        # E-Auto mit parent (die Wallbox misst die Ladung) → sonst Doppelzählung;
        # dieselbe Regel wie in `get_tagesdetail_kwh`.
        if typ == "e-auto" and getattr(inv, "parent_investition_id", None) is not None:
            continue
        inv_data = investitionen_map.get(str(inv_id_str))
        geraet_felder = (inv_data or {}).get("felder", {}) or {}

        for (t, feld), ausgabe_key in felder.items():
            if t != typ:
                continue
            cfg = geraet_felder.get(feld)
            sensor_key = f"inv:{inv_id_str}:{feld}"
            if not feld_hat_zaehler(cfg, sensor_key, quellen_energy, mqtt_keys):
                continue
            sensor_id = cfg.get("sensor_id") if isinstance(cfg, dict) else None

            reihe = await _reihe_ueber_bereich(
                db, anlage.id, sensor_key, grenzen[0], grenzen[-1],
            )
            staende = await _raender_mit_heilung(
                db, anlage, sensor_key, sensor_id, quellen_energy, grenzen, reihe,
            )

            for i, tages_start in enumerate(grenzen[:-1]):
                s0, s1 = staende[i], staende[i + 1]
                if s0 is None or s1 is None:
                    continue
                tages_ende = grenzen[i + 1]
                zwischen = [
                    w for ts, w in reihe if tages_start < ts < tages_ende
                ]
                wert = tageswert_aus_reihe(s0, s1, zwischen)
                if wert is None:
                    logger.debug(
                        "Zähler-Rücksprung im Tagesfenster für anlage=%s key=%s "
                        "(%s) — keine Tagesaussage",
                        anlage.id, sensor_key, tages_start.date(),
                    )
                    continue
                tag = tages_start.date()
                je_tag = ergebnis.setdefault(tag, {})
                je_tag[ausgabe_key] = je_tag.get(ausgabe_key, 0.0) + wert

    return ergebnis


async def _reihe_ueber_bereich(
    db: AsyncSession,
    anlage_id: int,
    sensor_key: str,
    von: datetime,
    bis: datetime,
) -> list[tuple[datetime, float]]:
    """**Die eine Abfrage** — alle Stände des Fensters, zeitlich sortiert.

    Die Ränder sind mit erfasst (im Unterschied zu ``reader.reihe_im_fenster``,
    das sie exklusiv hält): Hier **sind** sie Teil derselben Reihe, weil jeder
    innere Rand zugleich Ende des einen und Anfang des nächsten Tages ist.
    """
    rand = timedelta(minutes=RAND_TOLERANZ_MINUTEN)
    result = await db.execute(
        select(SensorSnapshot.zeitpunkt, SensorSnapshot.wert_kwh)
        .where(and_(
            SensorSnapshot.anlage_id == anlage_id,
            SensorSnapshot.sensor_key == sensor_key,
            SensorSnapshot.zeitpunkt >= von - rand,
            SensorSnapshot.zeitpunkt <= bis + rand,
        ))
        .order_by(SensorSnapshot.zeitpunkt)
    )
    return [(ts, wert) for ts, wert in result.all() if wert is not None]


async def _raender_mit_heilung(
    db: AsyncSession,
    anlage,
    sensor_key: str,
    sensor_id: Optional[str],
    quellen_energy: dict,
    grenzen: list[datetime],
    reihe: list[tuple[datetime, float]],
) -> list[Optional[float]]:
    """Je Tagesgrenze ein Stand — lokal, sonst über die Heilungs-Kaskade.

    ⚠ **Die Kaskade wird nur für die LÜCKEN gerufen.** Ein vollständig
    mitgeschriebener Monat kostet damit genau eine Abfrage (die Reihe oben);
    ein Monat aus der Zeit vor der Zuordnung kostet so viel wie früher, heilt
    sich dabei aber und ist beim nächsten Mal lokal da.
    """
    staende: list[Optional[float]] = [
        _naechster_stand(reihe, ziel) for ziel in grenzen
    ]
    for i, wert in enumerate(staende):
        if wert is not None:
            continue
        # `get_snapshot` fragt zuerst dieselbe Tabelle noch einmal — das ist
        # eine Abfrage zu viel, aber die Kaskade dahinter (HA-Langzeitstatistik,
        # MQTT-Backup, Rückschreiben) gibt es nur hier, und sie an dieser Stelle
        # nachzubauen wäre die F-56-Klasse.
        staende[i] = await get_snapshot(
            db, anlage.id, sensor_key, sensor_id, grenzen[i],
            quellen_energy=quellen_energy,
        )
    return staende
