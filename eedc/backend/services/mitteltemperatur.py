"""Die Außentemperatur als Mittelwert je Tag und je Monat — mit Vorrangkette.

**Warum es diesen Dienst gibt.** Der Wärme/Klima-Verlauf (Konzept §8) zeigt die
Außentemperatur als zweite Linie: Ein kalter Monat braucht mehr Strom, ohne dass
die Anlage schlechter arbeitet. Der naheliegende Ort dafür — das Feld
``Monatsdaten.durchschnittstemperatur`` — ist seit dem IA-V4-Flip **leer**: Sein
Auto-Fill lag in der gelöschten V3-Seite (**N-426**). Die Größe selbst ist
trotzdem da, nur an einer anderen Stelle.

⭐ **Und die überlebt sogar länger als gedacht.** Das Retention-Cleanup löscht
ausschließlich ``TagesEnergieProfil`` (die Stundenzeilen, `scheduler_jobs.py`) —
``TagesZusammenfassung`` mit ihrem Tages-Min/Max wird **nie** beschnitten.

**Die Vorrangkette, von genau nach ungenau:**

1. **Stundenwerte** (``TagesEnergieProfil.temperatur_c``) — das echte Mittel über
   die gemessenen Stunden. Reicht zwei Jahre zurück und deckt damit genau die
   Zeiträume ab, die ein Jahres-Verlauf zeigt.
2. **Tages-Min/Max** (``TagesZusammenfassung``) — ``(min + max) / 2``, die
   klimatologische Näherung. Sie greift für ältere Monate und für Tage, deren
   Stundenzeilen fehlen.
3. **Gepflegter Monatswert** (``Monatsdaten.durchschnittstemperatur``) — wer ihn
   von Hand einträgt, hat Vorrang vor **nichts**: Er kommt zuletzt, weil die
   gemessenen Reihen der Anlage näher sind als ein Wert aus einem Archiv.

⛔ **Kein Netzabruf.** Die Wetter-Route könnte jeden vergangenen Monat liefern,
aber zwölf Abrufe für eine Hilfslinie sind unverhältnismäßig — und sie träfen
das Wetter am Anlagenstandort nicht besser als die eigene Messreihe.

⚠ **Was der Wert NICHT ist:** eine Größe, aus der etwas gerechnet wird. Er
ordnet ein und wird angezeigt. Für eine Wetternormierung (Heizgradtage) braucht
es eine eigene, dokumentierte Definition — die Frage ist offen (Konzept §4).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.tages_energie_profil import TagesEnergieProfil, TagesZusammenfassung


async def lade_tagesmittel_temperatur(
    db: AsyncSession,
    anlage_id: int,
    von: "object" = None,
    bis: "object" = None,
) -> dict[object, float]:
    """Tagesmittel der Außentemperatur je Datum (Vorrang 1 vor 2).

    ``von``/``bis`` sind ``date``-Grenzen (einschließlich); ``None`` heißt
    „unbegrenzt". Rückgabe: ``{datum: °C}`` — Tage ohne jede Temperaturspur
    fehlen, statt mit 0 dazustehen.
    """
    # 1 — echtes Mittel über die Stundenzeilen des Tages.
    q = select(TagesEnergieProfil.datum, TagesEnergieProfil.temperatur_c).where(
        TagesEnergieProfil.anlage_id == anlage_id,
        TagesEnergieProfil.temperatur_c.is_not(None),
    )
    if von is not None:
        q = q.where(TagesEnergieProfil.datum >= von)
    if bis is not None:
        q = q.where(TagesEnergieProfil.datum <= bis)
    summe: dict[object, float] = defaultdict(float)
    anzahl: dict[object, int] = defaultdict(int)
    for datum, temp in (await db.execute(q)).all():
        summe[datum] += float(temp)
        anzahl[datum] += 1
    je_tag = {d: summe[d] / anzahl[d] for d in summe if anzahl[d] > 0}

    # 2 — Näherung aus Min/Max, nur wo Stufe 1 nichts hat.
    q2 = select(
        TagesZusammenfassung.datum,
        TagesZusammenfassung.temperatur_min_c,
        TagesZusammenfassung.temperatur_max_c,
    ).where(TagesZusammenfassung.anlage_id == anlage_id)
    if von is not None:
        q2 = q2.where(TagesZusammenfassung.datum >= von)
    if bis is not None:
        q2 = q2.where(TagesZusammenfassung.datum <= bis)
    for datum, tmin, tmax in (await db.execute(q2)).all():
        if datum in je_tag or tmin is None or tmax is None:
            continue
        je_tag[datum] = (float(tmin) + float(tmax)) / 2.0
    return je_tag


async def lade_monatsmittel_temperatur(
    db: AsyncSession,
    anlage_id: int,
    gepflegt_je_monat: Optional[dict[tuple[int, int], Optional[float]]] = None,
) -> dict[tuple[int, int], float]:
    """Monatsmittel je ``(jahr, monat)`` über die ganze Historie der Anlage.

    ⚠ **Gemittelt wird über die TAGE, nicht über die Stunden des Monats.** Ein
    Monat, von dem nur wenige Tage Stundenwerte tragen, bekäme sonst das Gewicht
    dieser Tage — das Mittel kippte in Richtung der besser erfassten Zeit. Über
    Tagesmittel gemittelt zählt jeder erfasste Tag gleich viel.

    Args:
        gepflegt_je_monat: von Hand gepflegte Monatswerte
            (``Monatsdaten.durchschnittstemperatur``). Sie füllen **nur Lücken** —
            Stufe 3 der Vorrangkette.
    """
    je_tag = await lade_tagesmittel_temperatur(db, anlage_id)
    summe: dict[tuple[int, int], float] = defaultdict(float)
    tage: dict[tuple[int, int], int] = defaultdict(int)
    for datum, temp in je_tag.items():
        schluessel = (datum.year, datum.month)
        summe[schluessel] += temp
        tage[schluessel] += 1
    ergebnis = {k: round(summe[k] / tage[k], 1) for k in summe if tage[k] > 0}

    for schluessel, wert in (gepflegt_je_monat or {}).items():
        if wert is not None and schluessel not in ergebnis:
            ergebnis[schluessel] = round(float(wert), 1)
    return ergebnis
