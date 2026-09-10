"""Der Betriebsart-Stapel **eines Tages** — die Zusammenführung beider Zweige.

**Warum es dieses Modul gibt** (10.09.2026, Konzept Wärme/Klima §8,
Bauschnitt 4). Ein Tag kann seine Stromaufteilung aus **zwei** Quellen haben:

* **Zweig 1 — gemessene Betriebsart-Zähler** (F4): eigene kWh-Zähler je
  Betriebsart, ggf. je Innengerät.
* **Zweig 2 — aus dem Betriebsmodus abgeleitet**: die Stundenzeilen tragen den
  Modus, die Menge kommt aus dem Leistungspfad und wird auf die Tages-Zählersumme
  normiert (``falte_modus_split_tag``).

Die Weiche zwischen ihnen ist **SOLL §6.1/F4**, Invariante K2: *„Ein einziger
zugeordneter Zähler schaltet das Gerät ganz auf den gemessenen Weg und
**verdrängt** die abgeleitete Aufteilung."* Sie gilt **je Gerät**, nicht je
Anlage — eine Klimaanlage mit Betriebsart-Zählern und eine Wärmepumpe ohne
dürfen nebeneinander stehen.

⛔ **Bis hierher stand diese Zusammenführung ausgeschrieben in
``api/routes/energie_profil/views.py::get_tag_detail``** — also in einer Route,
und damit für jeden anderen Leser unerreichbar. Beim Bau des Monats-Verlaufs
(x = Tage) hätte sie ein zweites Mal entstehen müssen: **F-56.** Die Probe
``test_263_t3_gemessene_betriebsart_tag.py`` sagt im Kopf, warum das teuer
gewesen wäre — *„die beiden Zweige treffen sich in ``get_tag_detail``, die
Vorrang-Regel ist nur im Paar prüfbar"*.

⚠ **Und der Fehler, den es ohne diese Datei gegeben hätte, ist gemessen:** Der
erste Bauplan für den Monats-Verlauf kannte nur Zweig 2. An **dietmars** Anlage
(drei Innengeräte mit Riemann-Zählern je Betriebsart seit 24.08.2026) hätte der
Monat damit einen anderen Stapel gezeigt als Tag und Jahr daneben — die
S1-Verletzung (*„dieselbe Größe trägt überall denselben Wert"*), also genau die
v4.0.1-Klasse.

**Rein und ohne Datenbank** (ADR-001): Die Eingänge lädt der Aufrufer — die
Route für einen Tag, der Bereichs-Leser für einen ganzen Monat. Gefaltet wird
hier, und zwar nur einmal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from backend.core.berechnungen.modus_split import (
    ModusSplit,
    abdeckung_ueber_geraete,
    teilmengen_passen,
)
from backend.core.berechnungen.betriebsart_gemessen import modus_strom_zeile
from backend.core.betriebsmodus import HEIZEN, KUEHLEN, WARMWASSER

#: Toleranz der Teilmengen-Invariante (kWh) — ``Σ Teilmengen ≤ Gesamt + x``.
#: ⚠ Passt ein Gerät nicht, wird es **ganz** ausgelassen statt gekappt: eine
#: stille Kappung machte aus einem Widerspruch eine plausible Zahl (SOLL §6.1,
#: Invariante am Gesamt-Stromzähler).
TEILMENGEN_TOLERANZ_KWH = 0.5


@dataclass(frozen=True)
class TagesStapel:
    """Die Betriebsart-Aufteilung eines Tages über **alle** Geräte der Anlage."""

    heizen_kwh: float = 0.0
    warmwasser_kwh: float = 0.0
    kuehlen_kwh: float = 0.0
    lueften_kwh: float = 0.0
    entfeuchten_kwh: float = 0.0
    #: Was von der Bezugsmenge nach Abzug aller Teilmengen übrig bleibt.
    nicht_aufgeteilt_kwh: float = 0.0
    #: ⚠ **W-17b: die Grundmenge, auf die sich der Stapel bezieht** — die Σ der
    #: Bezugsmengen der Geräte, die eine Aufteilung beigesteuert haben. Sie ist
    #: bewusst **nicht** der gesamte Wärmepumpen-Strom: dort steckt auch der
    #: Strom von Geräten ohne Aufteilung, der sonst als „nicht aufgeteilt" beim
    #: falschen Gerät erschiene (an einer Instanz gemessen: 96,4 statt 6,4 kWh).
    #: dietmar1968 sah 30 kWh Balken unter einer 284-kWh-Kachel; die Differenz
    #: muss benannt werden, statt stumm zu bleiben.
    bezug_kwh: float = 0.0
    #: Stunden mit Modus-Erkenntnis, **über Geräte gedeckelt** — zwei Geräte mit
    #: je 18 Stunden ergeben nicht 36 Stunden Erkenntnis (W-17, dietmar1968).
    abdeckung_h: float = 0.0
    #: Hat überhaupt ein Gerät beigetragen?
    hat_split: bool = False
    #: Kam mindestens ein Beitrag aus **gemessenen** Betriebsart-Zählern?
    hat_gemessen: bool = False

    @property
    def ist_leer(self) -> bool:
        return not self.hat_split


def falte_tages_stapel(
    gemessen_je_inv: dict[str, dict[str, float]],
    zaehler_strom_je_inv: dict[str, float],
    splits_je_inv: dict[str, ModusSplit],
    investitionen_by_id: dict,
    datum: date,
) -> TagesStapel:
    """Faltet beide Zweige eines Tages zu **einem** Stapel.

    Args:
        gemessen_je_inv: ``{inv_id: {feldname: kwh}}`` aus
            ``get_betriebsart_strom_tageswerte`` — **mit unveränderten
            Feldnamen** samt Innengerät-Suffix. ⚠ Die Regel *Gerätefeld gewinnt,
            sonst Σ Innengeräte* löst ``modus_strom_zeile`` auf; sie hier vorab
            zu summieren wäre die Doppelzählungs-Klasse.
        zaehler_strom_je_inv: der **zählerbasierte** Tagesstrom je Gerät
            (``TagesZusammenfassung.komponenten_kwh``). ⛔ **Nicht** die Summe
            aus dem Leistungspfad — die weicht ab, und genau daran hängt W-17b.
        splits_je_inv: ``{inv_id: ModusSplit}`` aus ``lade_modus_split_tag``
            bzw. dem Bereichs-Leser (Zweig 2).
        investitionen_by_id: für die Zeitfilterung (``ist_aktiv_an``).
        datum: der Tag — entscheidet, welches Gerät überhaupt zählt.

    Returns:
        Den ``TagesStapel``. Trägt **kein** Gerät bei, ist er leer statt eine
        Reihe von Nullen (ADR-002/P4: keine Aussage statt einer 0).
    """
    heizen = warmwasser = kuehlen = lueften = entfeuchten = 0.0
    rest = bezug = abdeckung = 0.0
    hat_split = hat_gemessen = False
    gemessene_geraete: set[str] = set()

    # ── Zweig 1: gemessene Betriebsart-Zähler (Vorrang, SOLL §6.1/F4) ──────
    for inv_id_str, felder in gemessen_je_inv.items():
        inv = investitionen_by_id.get(inv_id_str)
        if inv is None or not inv.ist_aktiv_an(datum):
            continue
        zeile = modus_strom_zeile(felder)
        if not zeile.gemessen:
            continue
        # Ohne Tages-Bezug gibt es nichts, wovon die Teilmenge eine wäre.
        geraet_bezug = zaehler_strom_je_inv.get(inv_id_str)
        if geraet_bezug is None:
            continue
        if (
            zeile.heizen_kwh + zeile.kuehlen_kwh
            > float(geraet_bezug) + TEILMENGEN_TOLERANZ_KWH
        ):
            continue
        hat_split = True
        hat_gemessen = True
        gemessene_geraete.add(inv_id_str)
        bezug += float(geraet_bezug)
        heizen += zeile.heizen_kwh
        kuehlen += zeile.kuehlen_kwh
        # E4 (Konzept §2.3): Lüften und Entfeuchten sind erfassbar und
        # erscheinen in der Aufteilung — sie bekommen nur keine Kennzahl.
        lueften += zeile.lueften_kwh
        entfeuchten += zeile.entfeuchten_kwh
        rest += max(
            0.0,
            float(geraet_bezug) - zeile.heizen_kwh - zeile.kuehlen_kwh
            - zeile.lueften_kwh - zeile.entfeuchten_kwh,
        )

    # ── Zweig 2: aus dem Betriebsmodus abgeleitet ──────────────────────────
    for inv_id_str, split in splits_je_inv.items():
        # K2: ein Gerät, das oben schon gezählt hat, trägt hier nicht noch
        # einmal bei — der gemessene Weg verdrängt den abgeleiteten **ganz**.
        if inv_id_str in gemessene_geraete:
            continue
        inv = investitionen_by_id.get(inv_id_str)
        if inv is None or not inv.ist_aktiv_an(datum):
            continue
        if not teilmengen_passen(split, split.bezug_kwh):
            continue
        hat_split = True
        geraet_bezug = float(split.bezug_kwh or 0.0)
        bezug += geraet_bezug
        heizen += split.teilmenge_kwh(HEIZEN)
        kuehlen += split.teilmenge_kwh(KUEHLEN)
        # N-336: nur der abgeleitete Zweig füllt Warmwasser — s. `ModusStromZeile`.
        warmwasser += split.teilmenge_kwh(WARMWASSER)
        rest += max(
            0.0,
            geraet_bezug
            - split.teilmenge_kwh(HEIZEN) - split.teilmenge_kwh(KUEHLEN)
            - split.teilmenge_kwh(WARMWASSER),
        )
        # W-17: Die Schleife läuft über die GERÄTE des Tages. Zwei Wärmepumpen
        # mit je 18 erfassten Stunden ergeben nicht 36 Stunden Erkenntnis,
        # sondern höchstens 18 — ein Tag hat 24. Genau diese Zahl hat
        # dietmar1968 gemeldet (T89667 #210). Die Regel steht im Layer-SoT.
        abdeckung = abdeckung_ueber_geraete(abdeckung, split.abdeckung_h)

    return TagesStapel(
        heizen_kwh=heizen,
        warmwasser_kwh=warmwasser,
        kuehlen_kwh=kuehlen,
        lueften_kwh=lueften,
        entfeuchten_kwh=entfeuchten,
        nicht_aufgeteilt_kwh=rest,
        bezug_kwh=bezug,
        abdeckung_h=abdeckung,
        hat_split=hat_split,
        hat_gemessen=hat_gemessen,
    )
