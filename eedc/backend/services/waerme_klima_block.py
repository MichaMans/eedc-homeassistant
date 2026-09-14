"""Der Cockpit-Block *Wärme/Klima* — was er **zeigt** und was er **einmal sagt**.

**D-Sicht** (Konzept Wärme/Klima §6, Entscheid Gernot 14.09.2026): Der Block
zeigt Kacheln und Zeilen **nur mit Zahl**. Was die Ausstattung nicht hergibt,
steht **einmal je Sicht** im Kasten *„Was noch möglich wäre"* — mit dem
Handgriff daneben, nicht als sechs Kacheln mit „—".

## Der Anlass, in einem Satz

*„Das release ich so nicht."* (Gernot, 14.09.2026) — Cockpit → Monat zeigte im
Block **vier** Striche mit Grund-Texten, während dietmar1968s selbstgebautes
Dashboard auf **derselben Datenlage** überall Zahlen zeigt und „–" nur dort, wo
wirklich nichts ist. Gemessen stimmte beides; der Unterschied war die Form.

## Zwei Bauteile, und beide entstehen genau einmal

* {@link geraete_zeilen} — die Tabelle *„Zahlen je Gerät"*. Sie liest die
  Kennzahlen aus ``services/waermepumpe_kennzahlen_je_geraet.py``, also aus
  **derselben** Stelle wie der Komponenten-Hub. Der Hub-Link bleibt daneben; er
  führt jetzt zu *mehr* (Verlauf, Saison, Wirtschaftlichkeit) statt zu dem, was
  hier fehlte.
* {@link was_noch_moeglich} — der Kasten. Er fragt für jeden Grund die
  **Klasse** ({@link grund_klasse}): *Ausstattung* ⇒ Kasten mit Handgriff ·
  *Zeitraum* ⇒ „—" ohne Text an der Kachel. Die Klassifizierung steht an der
  Grund-Konstante im Layer, nicht hier und erst recht nicht im Client — sonst
  stünde dieselbe Aussage an zwei Orten (die W-3-Klasse).
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from pydantic import BaseModel

from backend.core.berechnungen.waermepumpe_kennzahl import (
    GRUND_BAUARTEN_GEMISCHT,
    GRUND_FREMDSTROM,
    GRUND_FREMDWAERME,
    GRUND_GERAETE_OHNE_WAERME,
    GRUND_GERAETE_VERSCHIEDEN,
    GRUND_FUNKTION_NICHT_DECKUNGSGLEICH,
    GRUND_KEIN_STROM,
    GRUND_ZEITRAUM,
    HANDGRIFF_JE_GRUND,
    ist_ausstattungs_grund,
)
from backend.core.tageswert_grund import (
    GRUND_KEINE_ZAEHLERSTAENDE,
    GRUND_ZAEHLER_RUECKSPRUNG,
    TAGESWERT_GRUND_KURZ,
)
from backend.services.waermepumpe_kennzahlen_je_geraet import GeraetKennzahlen

#: Wohin der Handgriff führt. **Drei Ziele, nicht mehr** — jeder Grund gehört zu
#: genau einer Fläche:
#:
#: * *Datenquellen* — ein Zähler fehlt oder ein Kennzeichen ist nicht gesetzt.
#: * *Daten* (Daten-Checker · Reparatur-Werkbank) — die Messung ist da, aber
#:   lückenhaft. ⭐ **Der Daten-Checker bleibt der Ort für Reparatur-Hinweise**
#:   (D-Sicht); der Kasten verweist dorthin, statt sie zu wiederholen.
#: * *Komponenten-Hub* — die Zahl gibt es, nur nicht anlagenweit.
#:
#: ⚠ Ein Grund ohne Eintrag bekommt **keinen** Link. Ein Link, der auf eine
#: Sicht führt, die dasselbe sagt, ist schlechter als keiner — dieselbe Regel
#: wie bei ``GRUENDE_HUB_HILFT``.
LINK_DATENQUELLEN = "#/einstellungen/datenquellen"
LINK_DATEN = "#/einstellungen/daten"
LINK_HUB = "#/komponenten/waermepumpe"

LINK_JE_GRUND: dict[str, str] = {
    GRUND_BAUARTEN_GEMISCHT: LINK_HUB,
    GRUND_GERAETE_OHNE_WAERME: LINK_HUB,
    GRUND_GERAETE_VERSCHIEDEN: LINK_HUB,
    GRUND_FUNKTION_NICHT_DECKUNGSGLEICH: LINK_DATENQUELLEN,
    GRUND_FREMDSTROM: LINK_DATENQUELLEN,
    GRUND_FREMDWAERME: LINK_DATENQUELLEN,
    GRUND_KEIN_STROM: LINK_DATENQUELLEN,
    GRUND_ZEITRAUM: LINK_DATEN,
    TAGESWERT_GRUND_KURZ[GRUND_KEINE_ZAEHLERSTAENDE]: LINK_DATEN,
    TAGESWERT_GRUND_KURZ[GRUND_ZAEHLER_RUECKSPRUNG]: LINK_DATEN,
}


def _link(grund: str) -> Optional[str]:
    """Das Ziel des Handgriffs — Default *Datenquellen*, wo einer existiert.

    ⚠ **Kein Link ohne Handgriff.** Ein Grund, für den es nichts zu tun gibt
    (*„… aus verschiedenen Monaten"* hat einen Handgriff, *„Nutzenergie und
    Strom … aus verschiedenen Monaten"* ebenfalls), bekommt keine Schaltfläche,
    die ins Leere führt.
    """
    if grund in LINK_JE_GRUND:
        return LINK_JE_GRUND[grund]
    return LINK_DATENQUELLEN if grund in HANDGRIFF_JE_GRUND else None


class WpGeraetZeile(BaseModel):
    """Eine Zeile der Tabelle *„Zahlen je Gerät"* im Block.

    ⛔ **Der Client rechnet hier nichts** (ADR-002/**P12**, ``check:cop-roh``):
    Jede Arbeitszahl steht fertig mit ihrem Grund daneben; „—" entsteht aus
    ``None``, nicht aus einer Division im Browser.
    """

    investition_id: int
    name: str
    strom_kwh: Optional[float] = None
    waerme_kwh: Optional[float] = None
    jaz: Optional[float] = None
    jaz_grund: Optional[str] = None
    jaz_heizen: Optional[float] = None
    jaz_heizen_grund: Optional[str] = None
    jaz_warmwasser: Optional[float] = None
    jaz_warmwasser_grund: Optional[str] = None
    jaz_kuehlen: Optional[float] = None
    jaz_kuehlen_grund: Optional[str] = None


#: Die Namen der Größen, die im Kasten stehen können. **Sie sind Bezeichner,
#: keine Sätze** — der Client fragt mit ihnen ab, ob eine Kachel in den Kasten
#: gewandert ist, statt Grund-**Texte** zu vergleichen.
#:
#: ⭐ **Warum nicht über den Grund-Text** (gemessen 14.09.2026): Die Tages-Route
#: reicht an derselben Kachel die **Kurzform** des W-18-Grundes herein, während
#: unter der Wärme-Kachel die **Langform** steht. Ein Text-Vergleich im Client
#: hätte dort nie getroffen — und zwar still. Ein Name ist ein Schlüssel, ein
#: Satz ist eine Formulierung.
GROESSE_ARBEITSZAHL = "Arbeitszahl"
GROESSE_ARBEITSZAHL_HEIZEN = "Arbeitszahl Heizen"
GROESSE_ARBEITSZAHL_WARMWASSER = "Arbeitszahl Warmwasser"
GROESSE_ARBEITSZAHL_KUEHLEN = "Arbeitszahl Kühlen"
GROESSE_WAERME = "Wärme erzeugt"

#: Der Wortschatz als Menge — der Client-Spiegel wird daran geprüft.
GROESSEN_IM_KASTEN: frozenset[str] = frozenset({
    GROESSE_ARBEITSZAHL,
    GROESSE_ARBEITSZAHL_HEIZEN,
    GROESSE_ARBEITSZAHL_WARMWASSER,
    GROESSE_ARBEITSZAHL_KUEHLEN,
    GROESSE_WAERME,
})


class WpMoeglichZeile(BaseModel):
    """Eine Zeile des Kastens *„Was noch möglich wäre"*.

    Eine Zeile **je Grund**, nicht je Kachel: Derselbe fehlende Zähler sperrt
    regelmäßig zwei Kennzahlen, und zweimal denselben Satz zu lesen ist genau
    die Wiederholung, die der Kasten abschaffen soll.
    """

    #: Die betroffenen Größen als **Bezeichner** — womit der Client abfragt.
    groessen: list[str]
    #: Dieselben Größen als **Anzeigetext** („A · B"). Die Trennung ist
    #: Absicht: Wer eine Beschriftung ändert, soll keine Abfrage brechen.
    groesse: str
    grund: str
    handgriff: Optional[str] = None
    link: Optional[str] = None


def _r(wert: Optional[float], stellen: int = 2) -> Optional[float]:
    return round(wert, stellen) if wert is not None else None


def geraete_zeilen(
    kennzahlen: Sequence[GeraetKennzahlen],
) -> list[WpGeraetZeile]:
    """Die Tabelle *„Zahlen je Gerät"* — aus der einen Rechenstelle.

    ⚠ **Geräte ohne jede Menge fallen heraus.** Ein im Zeitraum stillstehendes
    (oder stillgelegtes) Gerät als Zeile aus Strichen zu zeigen wäre genau die
    Strich-Flut, gegen die die D-Sicht gebaut ist. Ein Gerät mit Strom **oder**
    Wärme bleibt — auch wenn seine Arbeitszahl gesperrt ist: Dann trägt die
    Zeile die Mengen und den Grund, und das ist eine Auskunft.
    """
    zeilen: list[WpGeraetZeile] = []
    for k in kennzahlen:
        m = k.mengen
        if m.strom_kwh <= 0 and m.waerme_kwh <= 0:
            continue
        zeilen.append(WpGeraetZeile(
            investition_id=m.inv_id,
            name=m.name,
            strom_kwh=_r(m.strom_kwh, 1),
            # P4: eine fehlende Wärme ist keine 0 — sie ist keine Zahl.
            waerme_kwh=_r(m.waerme_kwh, 1) if m.waerme_kwh > 0 else None,
            jaz=_r(k.gesamt.wert),
            jaz_grund=k.gesamt.grund,
            jaz_heizen=_r(k.je_funktion.heizen.wert),
            jaz_heizen_grund=k.je_funktion.heizen.grund,
            jaz_warmwasser=_r(k.je_funktion.warmwasser.wert),
            jaz_warmwasser_grund=k.je_funktion.warmwasser.grund,
            jaz_kuehlen=_r(k.kuehlen.wert),
            jaz_kuehlen_grund=k.kuehlen.grund,
        ))
    return zeilen


def was_noch_moeglich(
    gruende: Iterable[tuple[str, Optional[str]]],
) -> list[WpMoeglichZeile]:
    """Der Kasten — **jeder Ausstattungs-Grund genau einmal**.

    Args:
        gruende: Paare ``(Größen-Name, Grund)``. Ein Grund, der die Klasse
            *Zeitraum* trägt, erscheint **nicht** — dort steht an der Kachel ein
            „—" ohne Text, und der Kasten bliebe sonst im Juni voll mit Sätzen,
            zu denen es nichts zu tun gibt.

    ⭐ **Die Reihenfolge ist die der Aufrufer-Liste**, nicht alphabetisch: Die
    Gesamtzahl steht oben, die Funktionen darunter — dieselbe Reihenfolge, in
    der der Anwender sie im Block gesucht hat.
    """
    reihenfolge: list[str] = []
    groessen: dict[str, list[str]] = {}
    for groesse, grund in gruende:
        if not grund or not ist_ausstattungs_grund(grund):
            continue
        if grund not in groessen:
            groessen[grund] = []
            reihenfolge.append(grund)
        if groesse not in groessen[grund]:
            groessen[grund].append(groesse)
    return [
        WpMoeglichZeile(
            groessen=list(groessen[g]),
            groesse=" · ".join(groessen[g]),
            grund=g,
            handgriff=HANDGRIFF_JE_GRUND.get(g),
            link=_link(g),
        )
        for g in reihenfolge
    ]


def schranken_eingang(
    kennzahlen: Sequence[GeraetKennzahlen],
) -> tuple[float, list[str]]:
    """Wieviel Strom steht im Nenner **ohne** gemessene Wärme — und von wem?

    Der Eingang der Schranke (**E1b**). ``(0.0, [])`` heißt: Jedes Gerät, das
    Strom beisteuert, steuert auch gemessene Wärme bei — dann ist die
    anlagenweite Zahl die gewohnte Arbeitszahl und trägt kein „≥".

    ⚠ **Gezählt wird der BEITRAG, nicht der Bestand** — ein Gerät ohne Strom in
    diesem Zeitraum macht keine Schranke auf. Dieselbe Regel wie in
    ``deckung_aus_geraeten`` (N-441).
    """
    menge = 0.0
    namen: list[str] = []
    for k in kennzahlen:
        m = k.mengen
        if m.strom_kwh <= 0 or m.hat_waermemessung:
            continue
        menge += m.strom_kwh
        if m.name and m.name not in namen:
            namen.append(m.name)
    return menge, namen
