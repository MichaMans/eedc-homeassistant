"""
Typed Range über Snapshot-Boundaries (Etappe 3c P2, KONZEPT-ENERGIEPROFIL-3C.md).

Kapselt die Backward-Konvention nach Issue #144 für Hourly-Slots
und das HA-konforme Tagesfenster für Boundary-Diff-Tagesgesamt.
Konsumenten dürfen Slot-Indices nicht selbst rechnen — sie iterieren über
`boundary_offsets` und `slot_pairs`, lesen Snapshots an `boundary_at(offset)`
und nehmen für jeden Slot das Tupel `(slot_idx, prev_offset, curr_offset)`.

Backward-Konvention #144 (Slot 0..23):
    Snapshots @ Vortag 23:00, Heute 00:00, ..., Heute 23:00 → 25 Boundaries
    Slot h = snap[curr=h] − snap[prev=h-1]                  → 24 Slots
    Slot 0  = Energie [Vortag 23:00, Heute 00:00)
    Slot 23 = Energie [Heute 22:00, Heute 23:00)

HA-Tagesgesamt (Boundary-Diff über [Heute 00:00, Folgetag 00:00)):
    Snapshots @ Heute 00:00, Folgetag 00:00 → 2 Boundaries
    Tagesgesamt = snap[24] − snap[0]

Beide Fenster sind 24 Stunden lang, aber semantisch verschieden — Konsumenten
dürfen nicht erwarten, dass `Σ slot[0..23] == Tagesgesamt` ist (Slot-Σ deckt
[Vortag-23, Heute-23) ab, nicht [Heute-00, Folgetag-00)).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

#: Die Herkunfts-Kennung einer Tageszeile, deren Werte aus den HA-LTS-Stunden
#: stammen (Σ der 24 Rückwärts-Slots). **Eine Konstante für Schreiber und
#: Leser** (N-434): Der Energieprofil-Aggregator schreibt sie, die Tagessichten
#: lesen an ihr ab, in welchem Fenster `komponenten_kwh` steht. Zwei Literale
#: wären die F-56-Klasse — benennt jemand die Kennung um, läse der Leser still
#: das falsche Fenster.
TZ_QUELLE_LTS: str = "external:ha_statistics:daily"

#: Präfix der Wärmepumpen-Schlüssel in `komponenten_kwh` bzw. ihrer Provenance.
_WP_PROVENANCE_PRAEFIX = "komponenten_kwh.waermepumpe_"


def tageszeile_ist_rueckwaerts(source_provenance: Optional[dict[str, Any]]) -> bool:
    """Steht der Wärmepumpen-Tagesstrom dieser Tageszeile im Rückwärtsfenster?

    ``True``, wenn ein ``komponenten_kwh.waermepumpe_*``-Eintrag die Quelle
    {@link TZ_QUELLE_LTS} trägt — dann ist der Wert Σ der LTS-Slots
    [Vortag 23:00, Heute 23:00) und jede Teilmenge desselben Geräts muss über
    {@link BoundaryRange.for_day_backward} gelesen werden.

    ``False`` sonst — auch ohne Provenance (Altbestand, Snapshot-Pfad, Fixture):
    dann gilt das bisherige HA-Tagesfenster [00:00, 24:00). Das ist die
    Voreinstellung, die bis N-434 überall galt; eine Zeile ohne Angabe wird
    nicht umgedeutet.
    """
    if not isinstance(source_provenance, dict):
        return False
    for key, eintrag in source_provenance.items():
        if not str(key).startswith(_WP_PROVENANCE_PRAEFIX):
            continue
        if isinstance(eintrag, dict) and eintrag.get("source") == TZ_QUELLE_LTS:
            return True
    return False


@dataclass(frozen=True)
class BoundaryRange:
    """Typed Range über Snapshot-Zeitstempel für eine bestimmte Aggregat-Variante."""

    datum: date
    boundary_offsets: tuple[int, ...]
    """Stunden-Offsets relativ zu `datum` 00:00, an denen Snapshots gelesen werden."""

    slot_pairs: tuple[tuple[int, int, int], ...]
    """Liste von `(slot_idx, prev_offset, curr_offset)` für die Slot-Aggregation.

    Leer für `for_day_total` — dort `boundary_offsets` direkt nutzen.
    """

    @classmethod
    def for_hourly_slots(cls, datum: date) -> "BoundaryRange":
        """Backward-Hourly nach Issue #144.

        25 Boundaries (offsets `-1..23`), 24 Slots (`0..23`).
        Slot h = `snap[curr=h] − snap[prev=h-1]`.
        Slot 0 = Energie [Vortag 23:00, Heute 00:00).
        """
        return cls(
            datum=datum,
            boundary_offsets=tuple(range(-1, 24)),
            slot_pairs=tuple((h, h - 1, h) for h in range(24)),
        )

    @classmethod
    def for_day_backward(cls, datum: date) -> "BoundaryRange":
        """Tagesgesamt über das **Rückwärtsfenster** — dasselbe wie Σ der Slots.

        2 Boundaries (offsets `-1` und `23`).
        Tagesgesamt = `snap[23] − snap[-1]` = Energie [Vortag 23:00, Heute 23:00).

        ⭐ **Warum es dieses Fenster als Tageswert gibt (N-434, 11.09.2026).** Im
        HA-Add-on entsteht `TagesZusammenfassung.komponenten_kwh` als Σ der 24
        LTS-Slots (`get_komponenten_tageskwh_lts`) — also in DIESEM Fenster, nicht
        in [00:00, 24:00). Wer einen zweiten Zähler desselben Geräts als Teilmenge
        dagegenstellt (die gemessenen Betriebsart-Zähler), muss ihn im selben
        Fenster lesen. Sonst erscheint die Differenz zweier Randstunden als
        Messung: 1,8 von 7,0 kWh „nicht aufgeteilt" bei einem Gerät, das nur
        heizt — oder die Aufteilung verschwindet, weil die Teilmenge größer
        wirkt als ihr Ganzes. Welches Fenster eine Tageszeile trägt, sagt
        {@link tageszeile_ist_rueckwaerts}.
        """
        return cls(
            datum=datum,
            boundary_offsets=(-1, 23),
            slot_pairs=(),
        )

    @classmethod
    def for_day_total(cls, datum: date) -> "BoundaryRange":
        """HA-konformer Tagesgesamt-Range.

        2 Boundaries (offsets `0` und `24`).
        Tagesgesamt = `snap[24] − snap[0]` = Energie [Heute 00:00, Folgetag 00:00).
        Wird in Päckchen 3 (E2) primärer Truth-Pfad für TagesZusammenfassung-Felder.
        """
        return cls(
            datum=datum,
            boundary_offsets=(0, 24),
            slot_pairs=(),
        )

    def boundary_at(self, offset: int) -> datetime:
        """Zeitstempel für einen Boundary-Offset (Stunden seit `datum` 00:00)."""
        return datetime.combine(self.datum, datetime.min.time()) + timedelta(hours=offset)
