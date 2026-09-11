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

---

## Die Stunden (11.09.2026, Bauschnitt 5)

⭐ **Die Stunde verteilt den Tag — sie rechnet ihn nicht neu.** Welches Gerät
mit welchem Zweig und welcher Menge beiträgt, entscheidet **dieselbe** Auswahl
wie für den Tag ({@link beitraege_des_tages}). Die Stunde bekommt je Gerät und
Segment die Tagesmenge × ihren Anteil an der gemessenen Stundenform. Damit ist
Σ Stunden = Tagesbalken je Segment **per Konstruktion** — das ist S1 innerhalb
des Blocks, und genau daran hing W-17b (dietmar1968: 30 kWh Balken unter einer
284-kWh-Kachel).

* **Zweig 2:** Anteil an der Modus-Form × Tagesmenge ist algebraisch derselbe
  Faktor wie in ``falte_modus_split_tag`` (Tagesmenge ÷ Stundensumme) — auch
  ohne Normierung und für Stunden ohne Modus (die gehen in den Rest).
* **Zweig 1:** je Feld verteilt, dann ``modus_strom_zeile`` **je Stunde** auf
  dieselbe Schlüsselmenge (explizite 0,0) — die Innengeräte-Regel bleibt an
  ihrer einen Stelle. Ohne feste Schlüsselmenge wäre sie nicht linear: fehlt
  das Gerätefeld in einer Stunde, summierte sie die Innengeräte (Probe der
  Gegenprüfung: 5,5 statt 4,0).
* **Keine Form, aber eine Menge** (z. B. ein Zähler, dessen ganzer Tagesverbrauch
  in der Stunde lag, die das Rückwärts-Raster dem Folgetag gibt): **nicht
  verteilen** — die Menge fehlt in den Stunden und wird als
  ``ohne_stundenform_kwh`` genannt (ADR-002/P4: keine erfundene Form).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from backend.core.berechnungen.modus_split import (
    ModusSplit,
    ModusStunde,
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

#: Anzahl der Stunden-Slots eines Tages (Rückwärts-Raster, Slot h = [h−1, h)).
STUNDEN = 24

#: Die Modi, die Zweig 2 als eigene Teilmenge zählt — alles andere ist Rest.
#: Dieselbe Menge wie in {@link beitraege_des_tages} (N-336: Warmwasser nur hier).
_ZWEIG2_MODI = (HEIZEN, KUEHLEN, WARMWASSER)


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


@dataclass(frozen=True)
class GeraeteBeitrag:
    """Was **ein** Gerät zum Tagesstapel beiträgt — die Entscheidung des Tages.

    Sie wird für den Tag gefaltet und für die Stunden verteilt; beide lesen
    dieselbe Liste, damit die Stunde nichts entscheidet, was der Tag nicht
    schon entschieden hat.
    """

    inv_id: str
    gemessen: bool
    bezug_kwh: float
    heizen_kwh: float = 0.0
    warmwasser_kwh: float = 0.0
    kuehlen_kwh: float = 0.0
    lueften_kwh: float = 0.0
    entfeuchten_kwh: float = 0.0
    nicht_aufgeteilt_kwh: float = 0.0
    abdeckung_h: float = 0.0
    #: Zweig 1: die Tageswerte je Feld, **unverändert samt Innengerät-Suffix** —
    #: die Stunde verteilt sie einzeln und löst danach je Stunde auf.
    felder: dict[str, float] = field(default_factory=dict)


def beitraege_des_tages(
    gemessen_je_inv: dict[str, dict[str, float]],
    zaehler_strom_je_inv: dict[str, float],
    splits_je_inv: dict[str, ModusSplit],
    investitionen_by_id: dict,
    datum: date,
) -> list[GeraeteBeitrag]:
    """Welches Gerät mit welchem Zweig beiträgt — die eine Auswahl (K2, Zeitfilter, Invariante).

    Args:
        gemessen_je_inv: ``{inv_id: {feldname: kwh}}`` aus
            ``get_betriebsart_strom_tageswerte`` — **mit unveränderten
            Feldnamen** samt Innengerät-Suffix. ⚠ Die Regel *Gerätefeld gewinnt,
            sonst Σ Innengeräte* löst ``modus_strom_zeile`` auf; sie hier vorab
            zu summieren wäre die Doppelzählungs-Klasse.
        zaehler_strom_je_inv: der **zählerbasierte** Tagesstrom je Gerät
            (``TagesZusammenfassung.komponenten_kwh``). ⛔ **Nicht** die Summe
            aus dem Leistungspfad — die weicht ab, und genau daran hängt W-17b.
            ⚠ Seit N-434 steht er im selben Fenster wie ``gemessen_je_inv``.
        splits_je_inv: ``{inv_id: ModusSplit}`` aus ``lade_modus_split_tag``
            bzw. dem Bereichs-Leser (Zweig 2).
        investitionen_by_id: für die Zeitfilterung (``ist_aktiv_an``).
        datum: der Tag — entscheidet, welches Gerät überhaupt zählt.

    Returns:
        Die Beiträge, Zweig 1 vor Zweig 2 — dieselbe Reihenfolge, in der die
        Tagesfaltung sie addiert.
    """
    beitraege: list[GeraeteBeitrag] = []
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
        gemessene_geraete.add(inv_id_str)
        beitraege.append(GeraeteBeitrag(
            inv_id=inv_id_str,
            gemessen=True,
            bezug_kwh=float(geraet_bezug),
            heizen_kwh=zeile.heizen_kwh,
            kuehlen_kwh=zeile.kuehlen_kwh,
            # E4 (Konzept §2.3): Lüften und Entfeuchten sind erfassbar und
            # erscheinen in der Aufteilung — sie bekommen nur keine Kennzahl.
            lueften_kwh=zeile.lueften_kwh,
            entfeuchten_kwh=zeile.entfeuchten_kwh,
            nicht_aufgeteilt_kwh=max(
                0.0,
                float(geraet_bezug) - zeile.heizen_kwh - zeile.kuehlen_kwh
                - zeile.lueften_kwh - zeile.entfeuchten_kwh,
            ),
            felder=dict(felder),
        ))

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
        geraet_bezug = float(split.bezug_kwh or 0.0)
        beitraege.append(GeraeteBeitrag(
            inv_id=inv_id_str,
            gemessen=False,
            bezug_kwh=geraet_bezug,
            heizen_kwh=split.teilmenge_kwh(HEIZEN),
            kuehlen_kwh=split.teilmenge_kwh(KUEHLEN),
            # N-336: nur der abgeleitete Zweig füllt Warmwasser — s. `ModusStromZeile`.
            warmwasser_kwh=split.teilmenge_kwh(WARMWASSER),
            nicht_aufgeteilt_kwh=max(
                0.0,
                geraet_bezug
                - split.teilmenge_kwh(HEIZEN) - split.teilmenge_kwh(KUEHLEN)
                - split.teilmenge_kwh(WARMWASSER),
            ),
            abdeckung_h=split.abdeckung_h,
        ))
    return beitraege


def _falte(beitraege: Sequence[GeraeteBeitrag]) -> TagesStapel:
    """Summiert die Beiträge — in ihrer Reihenfolge, damit die Zahlen bitgleich bleiben."""
    heizen = warmwasser = kuehlen = lueften = entfeuchten = 0.0
    rest = bezug = abdeckung = 0.0
    for b in beitraege:
        bezug += b.bezug_kwh
        heizen += b.heizen_kwh
        kuehlen += b.kuehlen_kwh
        warmwasser += b.warmwasser_kwh
        lueften += b.lueften_kwh
        entfeuchten += b.entfeuchten_kwh
        rest += b.nicht_aufgeteilt_kwh
        if not b.gemessen:
            # W-17: Die Schleife läuft über die GERÄTE des Tages. Zwei Wärmepumpen
            # mit je 18 erfassten Stunden ergeben nicht 36 Stunden Erkenntnis,
            # sondern höchstens 18 — ein Tag hat 24. Genau diese Zahl hat
            # dietmar1968 gemeldet (T89667 #210). Die Regel steht im Layer-SoT.
            abdeckung = abdeckung_ueber_geraete(abdeckung, b.abdeckung_h)
    return TagesStapel(
        heizen_kwh=heizen,
        warmwasser_kwh=warmwasser,
        kuehlen_kwh=kuehlen,
        lueften_kwh=lueften,
        entfeuchten_kwh=entfeuchten,
        nicht_aufgeteilt_kwh=rest,
        bezug_kwh=bezug,
        abdeckung_h=abdeckung,
        hat_split=bool(beitraege),
        hat_gemessen=any(b.gemessen for b in beitraege),
    )


def falte_tages_stapel(
    gemessen_je_inv: dict[str, dict[str, float]],
    zaehler_strom_je_inv: dict[str, float],
    splits_je_inv: dict[str, ModusSplit],
    investitionen_by_id: dict,
    datum: date,
) -> TagesStapel:
    """Faltet beide Zweige eines Tages zu **einem** Stapel.

    Args wie {@link beitraege_des_tages}.

    Returns:
        Den ``TagesStapel``. Trägt **kein** Gerät bei, ist er leer statt eine
        Reihe von Nullen (ADR-002/P4: keine Aussage statt einer 0).
    """
    return _falte(beitraege_des_tages(
        gemessen_je_inv, zaehler_strom_je_inv, splits_je_inv,
        investitionen_by_id, datum,
    ))


# ── Die Stunden ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StundenFormen:
    """Die gemessene **Form** der Stunden je Gerät — sie verteilt, sie entscheidet nichts.

    Jede Liste hat 24 Einträge (Slot h = [h−1, h)); ``None`` heißt „in diesem
    Slot kein Wert" und trägt keinen Anteil.
    """

    #: Zweig 1: je Gerät je Feld (samt Suffix) die Slot-Mengen des Zählers.
    felder_je_inv: dict[str, dict[str, list[Optional[float]]]] = field(default_factory=dict)
    #: Zweig 1, Rest-Form: der Gesamtstrom des Geräts je Slot aus dem Zählerpfad.
    gesamt_je_inv: dict[str, list[Optional[float]]] = field(default_factory=dict)
    #: Rückfall für den Gesamtstrom und Zweig 2: die Stundenzeilen (Leistungspfad)
    #: mit Modus und Slot.
    modus_stunden_je_inv: dict[str, list[ModusStunde]] = field(default_factory=dict)


@dataclass(frozen=True)
class StundenVerteilung:
    """Der Tagesstapel auf 24 Stunden verteilt."""

    #: 24 Einträge. Jeder trägt das **Tor des Tages** (``hat_split``,
    #: ``hat_gemessen``, ``abdeckung_h``) — hat der Tag eine Aufteilung, gilt sie
    #: für jede seiner Stunden; auch eine Stunde, die nur Rest trägt, bleibt
    #: sichtbar, sonst verlöre die Zeichnung ihre Summe.
    stunden: list[TagesStapel]
    #: Menge, für die es keine Stundenform gab — sie fehlt in den Stunden (P4).
    ohne_stundenform_kwh: float = 0.0


def verteile_menge(
    menge: float, form: Sequence[Optional[float]],
) -> tuple[list[float], float]:
    """Eine Menge nach einer Form auf 24 Slots — oder gar nicht.

    Returns:
        ``(24 Werte, nicht_verteilt)``. Ohne positive Form bleibt die ganze Menge
        unverteilt; sie wird nie gleichmäßig verteilt (das wäre eine erfundene
        Form).
    """
    if menge <= 0:
        return [0.0] * STUNDEN, 0.0
    gewichte = [
        max(0.0, float(form[h])) if h < len(form) and form[h] is not None else 0.0
        for h in range(STUNDEN)
    ]
    summe = sum(gewichte)
    if summe <= 0:
        return [0.0] * STUNDEN, float(menge)
    return [menge * g / summe for g in gewichte], 0.0


def _modus_form(stunden: Sequence[ModusStunde], gehoert_dazu) -> list[float]:
    form = [0.0] * STUNDEN
    for s in stunden:
        if s.stunde is None or not 0 <= s.stunde < STUNDEN:
            continue
        if gehoert_dazu(s.modus):
            form[s.stunde] += abs(float(s.kwh or 0.0))
    return form


def _gesamt_form(inv_id: str, formen: StundenFormen) -> list[Optional[float]]:
    """Gesamtstrom je Slot: Zählerpfad, sonst Leistungspfad der Stundenzeilen."""
    gesamt = formen.gesamt_je_inv.get(inv_id)
    if gesamt and any(v is not None for v in gesamt):
        return list(gesamt)
    return _modus_form(formen.modus_stunden_je_inv.get(inv_id, ()), lambda _m: True)


def verteile_tages_stapel_auf_stunden(
    beitraege: Sequence[GeraeteBeitrag],
    formen: StundenFormen,
) -> StundenVerteilung:
    """Verteilt die Beiträge des Tages auf 24 Stunden (s. Modul-Kopf, *Die Stunden*)."""
    tag = _falte(beitraege)
    seg = ("heizen", "warmwasser", "kuehlen", "lueften", "entfeuchten", "rest")
    je_stunde = {k: [0.0] * STUNDEN for k in seg}
    ohne = 0.0

    for b in beitraege:
        geraet = {k: [0.0] * STUNDEN for k in seg}
        if b.gemessen:
            # Je Feld verteilen, dann je Stunde auflösen — auf DIESELBE
            # Schlüsselmenge, damit „Gerätefeld gewinnt" in jeder Stunde gilt.
            formen_felder = formen.felder_je_inv.get(b.inv_id, {})
            verteilt: dict[str, list[float]] = {}
            roh: dict[str, list[float]] = {}
            for feld, tageswert in b.felder.items():
                form = formen_felder.get(feld, [None] * STUNDEN)
                verteilt[feld], _ = verteile_menge(float(tageswert or 0.0), form)
                roh[feld] = [float(v) if v is not None else 0.0 for v in
                             (list(form) + [None] * STUNDEN)[:STUNDEN]]
            rest_form: list[float] = []
            gesamt = _gesamt_form(b.inv_id, formen)
            for h in range(STUNDEN):
                z = modus_strom_zeile({f: w[h] for f, w in verteilt.items()})
                geraet["heizen"][h] = z.heizen_kwh
                geraet["kuehlen"][h] = z.kuehlen_kwh
                geraet["lueften"][h] = z.lueften_kwh
                geraet["entfeuchten"][h] = z.entfeuchten_kwh
                z_roh = modus_strom_zeile({f: w[h] for f, w in roh.items()})
                g = gesamt[h] if h < len(gesamt) and gesamt[h] is not None else 0.0
                rest_form.append(max(
                    0.0,
                    g - z_roh.heizen_kwh - z_roh.kuehlen_kwh
                    - z_roh.lueften_kwh - z_roh.entfeuchten_kwh,
                ))
            geraet["rest"], _ = verteile_menge(b.nicht_aufgeteilt_kwh, rest_form)
        else:
            stunden = formen.modus_stunden_je_inv.get(b.inv_id, ())
            for key, modus, menge in (
                ("heizen", HEIZEN, b.heizen_kwh),
                ("kuehlen", KUEHLEN, b.kuehlen_kwh),
                ("warmwasser", WARMWASSER, b.warmwasser_kwh),
            ):
                geraet[key], _ = verteile_menge(
                    menge, _modus_form(stunden, lambda m, _mo=modus: m == _mo),
                )
            geraet["rest"], _ = verteile_menge(
                b.nicht_aufgeteilt_kwh,
                _modus_form(stunden, lambda m: m not in _ZWEIG2_MODI),
            )

        # Was die Stunden dieses Geräts NICHT tragen, je Segment gegen den Tag
        # gemessen — so zählen verdrängte Innengeräte-Felder nie mit.
        for key, tageswert in (
            ("heizen", b.heizen_kwh), ("warmwasser", b.warmwasser_kwh),
            ("kuehlen", b.kuehlen_kwh), ("lueften", b.lueften_kwh),
            ("entfeuchten", b.entfeuchten_kwh), ("rest", b.nicht_aufgeteilt_kwh),
        ):
            ohne += max(0.0, tageswert - sum(geraet[key]))
            for h in range(STUNDEN):
                je_stunde[key][h] += geraet[key][h]

    stunden_stapel = [
        TagesStapel(
            heizen_kwh=je_stunde["heizen"][h],
            warmwasser_kwh=je_stunde["warmwasser"][h],
            kuehlen_kwh=je_stunde["kuehlen"][h],
            lueften_kwh=je_stunde["lueften"][h],
            entfeuchten_kwh=je_stunde["entfeuchten"][h],
            nicht_aufgeteilt_kwh=je_stunde["rest"][h],
            bezug_kwh=sum(je_stunde[k][h] for k in seg),
            abdeckung_h=tag.abdeckung_h,
            hat_split=tag.hat_split,
            hat_gemessen=tag.hat_gemessen,
        )
        for h in range(STUNDEN)
    ]
    # Rundungsreste der Division sind keine fehlende Form.
    return StundenVerteilung(
        stunden=stunden_stapel,
        ohne_stundenform_kwh=ohne if ohne > 1e-6 else 0.0,
    )
