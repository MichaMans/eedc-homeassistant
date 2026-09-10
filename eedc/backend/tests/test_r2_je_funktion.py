"""R2 je Funktion — die Trennlinie ist die Abgrenzung, nicht die Bauart.

**SOLL Wärme/Klima §3.2b (10.09.2026).** Bis hierher legte
``arbeitszahl_je_funktion`` eine Abgrenzungs-Verletzung unbesehen auf **beide**
Zeilen. Für die Anwender-Angabe (Heizstab, bivalenter Erzeuger) und den
Zeitraum-Versatz ist das richtig; für **gemischte Bauarten** und **Geräte ohne
Wärme** war es eine Übersperre: Die Zahl existierte und wurde unterdrückt.

⭐ **Der Melder-Fall (dietmar1968, Fixture A5):** Wärmepumpe mit getrennter
Strommessung neben einer Split-Klimaanlage. Cockpit zeigte drei Striche, während
der Komponenten-Hub für dieselben Geräte längst 3,0 und 2,5 auswies.

⛔ **Warum die Regel BEIDSEITIG zählt und nicht „jedes Gerät mit Strom liefert
auch Wärme".** Die einseitige Fassung fängt nur den Nenner. Der Zähler kippt
genauso — und in die teurere Richtung, weil dann eine **zu hohe** Kennzahl
erscheint statt gar keiner:

* ``heizenergie_kwh`` trägt nur ``!brauchwasser`` (``field_definitions.py``),
  eine Split-Klimaanlage darf also Heizwärme melden.
* ``strom_warmwasser_kwh`` wird **ungefiltert** gelesen
  (``imd_monatsaggregat.py``, mit ausdrücklicher Begründung), und bis zum
  22.08.2026 wurde das Feld einer Klimaanlage mit getrennter Strommessung
  angeboten — Altbestand steht dort.

Schwesterdatei: ``test_soll_waerme_klima_simulation_anlagen.py`` (A5 selbst).
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.core.berechnungen.waermepumpe_kennzahl import (
    ARBEITSZAHL_FUNKTIONEN,
    GRUND_BAUARTEN_GEMISCHT,
    GRUND_FREMDSTROM,
    GRUND_FUNKTION_NICHT_DECKUNGSGLEICH,
    abgrenzung_je_funktion,
    arbeitszahl_je_funktion,
)
from backend.models import Anlage, Investition  # noqa: F401
from backend.models.investition import InvestitionMonatsdaten

JAHR, MONAT = 2025, 7


async def _anlage(db, name: str) -> Anlage:
    a = Anlage(anlagenname=name, leistung_kwp=10.0, installationsdatum=date(2025, 1, 1))
    db.add(a)
    await db.flush()
    return a


async def _geraet(db, anlage, bezeichnung: str, parameter: dict, daten: dict, monat: int = MONAT):
    inv = Investition(
        anlage_id=anlage.id, typ="waermepumpe", bezeichnung=bezeichnung,
        anschaffungsdatum=date(2025, 1, 1), anschaffungskosten_gesamt=12000.0,
        parameter=parameter,
    )
    db.add(inv)
    await db.flush()
    db.add(InvestitionMonatsdaten(
        investition_id=inv.id, jahr=JAHR, monat=monat, verbrauch_daten=daten,
    ))
    return inv


async def _monat(db, anlage_id, monat: int = MONAT):
    from backend.api.routes.aktueller_monat import get_aktueller_monat
    return await get_aktueller_monat(anlage_id, jahr=JAHR, monat=monat, db=db)


async def _jahr(db, anlage_id):
    from backend.api.routes.cockpit.uebersicht import get_cockpit_uebersicht
    return await get_cockpit_uebersicht(anlage_id, jahr=JAHR, db=db)


_WP = {"wp_art": "luft_wasser", "effizienz_modus": "gesamt_jaz", "getrennte_strommessung": True}
_KLIMA = {"wp_art": "luft_luft", "effizienz_modus": "gesamt_jaz"}


# ═══ Die Layer-Regel für sich ═══════════════════════════════════════════════

_SAUBER = {f: True for f in ARBEITSZAHL_FUNKTIONEN}


def test_anwender_angabe_trifft_weiterhin_alle_funktionen():
    """Ein Heizstab auf dem Zähler sperrt alles — die Angabe trägt keine Funktion."""
    je = abgrenzung_je_funktion(
        abgrenzung_stoerung="fremdstrom", deckung_je_funktion=_SAUBER,
    )
    assert all(je[f] == GRUND_FREMDSTROM for f in ARBEITSZAHL_FUNKTIONEN)


def test_zeitraum_versatz_trifft_weiterhin_alle_funktionen():
    je = abgrenzung_je_funktion(
        bauarten_gemischt=True, zeitraum_versetzt=True, deckung_je_funktion=_SAUBER,
    )
    assert all(je[f] is not None for f in ARBEITSZAHL_FUNKTIONEN)


def test_bauart_trifft_nur_die_vermischten_funktionen():
    je = abgrenzung_je_funktion(
        bauarten_gemischt=True,
        deckung_je_funktion={"heizen": True, "warmwasser": True, "kuehlen": False},
    )
    assert je["heizen"] is None and je["warmwasser"] is None
    assert je["kuehlen"] == GRUND_BAUARTEN_GEMISCHT


def test_ungleiche_geraetezahl_ohne_andere_lage_bekommt_ihren_eigenen_grund():
    """⭐ Die Lage, die es bis zum 10.09. GAR NICHT gab (8ear).

    Keine Bauart-Mischung, keine Anwender-Angabe, alle Geräte melden Wärme —
    und trotzdem stammen Zähler und Nenner dieser Funktion von verschieden
    vielen Geräten. Vorher stand dort eine **falsche Zahl** ohne jeden Hinweis.
    """
    je = abgrenzung_je_funktion(
        deckung_je_funktion={"heizen": True, "warmwasser": False, "kuehlen": None},
    )
    assert je["warmwasser"] == GRUND_FUNKTION_NICHT_DECKUNGSGLEICH
    assert je["heizen"] is None
    # (0, 0) ist KEINE Verletzung — die Funktion gibt es nicht.
    assert je["kuehlen"] is None


def test_fehlender_waermezaehler_bleibt_beim_besseren_satz():
    """``(n, 0)`` ist keine Abgrenzungs-Verletzung — dafür hat `arbeitszahl`
    den genaueren Satz („kein Wärmemengenzähler zugeordnet")."""
    je = abgrenzung_je_funktion(deckung_je_funktion={"heizen": None})
    assert je["heizen"] is None


def test_ohne_angabe_bleibt_der_default_bitgleich():
    """``geraete_je_funktion=None`` sperrt weiter alles — so rufen die Hub-Pfade."""
    je = abgrenzung_je_funktion(bauarten_gemischt=True)
    assert all(je[f] == GRUND_BAUARTEN_GEMISCHT for f in ARBEITSZAHL_FUNKTIONEN)
    je = arbeitszahl_je_funktion(
        heizung_kwh=300.0, strom_heizen_kwh=100.0,
        warmwasser_kwh=100.0, strom_warmwasser_kwh=40.0,
        hat_split=True, abgrenzung_verletzt=GRUND_BAUARTEN_GEMISCHT,
    )
    assert je.heizen.wert is None and je.heizen.grund == GRUND_BAUARTEN_GEMISCHT
    assert je.warmwasser.wert is None


def test_freigegebene_funktion_rechnet_die_gesperrte_nicht():
    je = arbeitszahl_je_funktion(
        heizung_kwh=300.0, strom_heizen_kwh=100.0,
        warmwasser_kwh=100.0, strom_warmwasser_kwh=40.0,
        hat_split=True, abgrenzung_verletzt=GRUND_BAUARTEN_GEMISCHT,
        abgrenzung_je_funktion_grund={"heizen": GRUND_BAUARTEN_GEMISCHT, "warmwasser": None},
    )
    assert je.heizen.wert is None and je.heizen.grund == GRUND_BAUARTEN_GEMISCHT
    assert je.warmwasser.wert == pytest.approx(2.5)


# ═══ Der Melder-Fall, end to end ════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a5_zeigt_je_funktion_in_monat_und_jahr(db):
    """dietmars Bauform: WP + Split-Klima ⇒ Heizen 3,0 · Warmwasser 2,5.

    Die Klimaanlage trägt ihre 200 kWh in ``stromverbrauch_kwh`` — das gehört zu
    **keiner** Funktion und steht in keinem der beiden Quotienten.
    """
    a = await _anlage(db, "A5")
    await _geraet(db, a, "Wärmepumpe", dict(_WP), {
        "strom_heizen_kwh": 800.0, "strom_warmwasser_kwh": 400.0,
        "heizenergie_kwh": 2400.0, "warmwasser_kwh": 1000.0,
    })
    await _geraet(db, a, "Klimaanlage", dict(_KLIMA), {"stromverbrauch_kwh": 200.0})
    await db.commit()

    m = await _monat(db, a.id)
    assert m.wp_jaz_heizen == pytest.approx(3.0) and m.wp_jaz_heizen_grund is None
    assert m.wp_jaz_warmwasser == pytest.approx(2.5) and m.wp_jaz_warmwasser_grund is None
    # ⚠ Die ANLAGENWEITE Zahl bleibt gesperrt, und das ist richtig: sie mischt
    # den Strom beider Geräte mit der Wärme eines.
    assert m.wp_jaz is None and m.wp_jaz_grund == GRUND_BAUARTEN_GEMISCHT

    j = await _jahr(db, a.id)
    assert j.wp_jaz_heizen == pytest.approx(3.0)
    assert j.wp_jaz_warmwasser == pytest.approx(2.5)


# ═══ Die vier Fälle, die WEITERHIN sperren müssen ═══════════════════════════

@pytest.mark.asyncio
async def test_klima_mit_heizwaerme_sperrt_heizen_weiter(db):
    """Zähler zu groß: die Klimaanlage meldet Heizwärme, aber keinen Heizstrom.

    ``heizenergie_kwh`` trägt kein ``!luft_luft``. Eine einseitige Regel
    („jedes Gerät mit Strom liefert auch Wärme") gäbe hier frei und zeigte
    **4,25 statt 3,75** — eine zu hohe Zahl ist teurer als gar keine.
    """
    a = await _anlage(db, "Klima mit Heizwärme")
    await _geraet(db, a, "Wärmepumpe", dict(_WP), {
        "strom_heizen_kwh": 800.0, "heizenergie_kwh": 3000.0,
    })
    await _geraet(db, a, "Klimaanlage", dict(_KLIMA), {
        "stromverbrauch_kwh": 200.0, "heizenergie_kwh": 400.0,
    })
    await db.commit()
    m = await _monat(db, a.id)
    assert m.wp_jaz_heizen is None
    assert m.wp_jaz_heizen_grund == GRUND_BAUARTEN_GEMISCHT


@pytest.mark.asyncio
async def test_klima_mit_altbestand_warmwasserstrom_sperrt_warmwasser_weiter(db):
    """Nenner zu groß: gespeicherter ``strom_warmwasser_kwh`` an der Klimaanlage.

    Das Feld wird ungefiltert gelesen, und bis 22.08.2026 wurde es einer
    Klimaanlage mit getrennter Strommessung angeboten.
    """
    a = await _anlage(db, "Klima mit Altbestand")
    await _geraet(db, a, "Wärmepumpe", dict(_WP), {
        "strom_warmwasser_kwh": 400.0, "warmwasser_kwh": 1000.0,
    })
    await _geraet(db, a, "Klimaanlage",
                  {**_KLIMA, "getrennte_strommessung": True},
                  {"strom_warmwasser_kwh": 100.0})
    await db.commit()
    m = await _monat(db, a.id)
    assert m.wp_jaz_warmwasser is None
    assert m.wp_jaz_warmwasser_grund is not None


@pytest.mark.asyncio
async def test_brauchwasser_wp_sperrt_warmwasser(db):
    """⭐ Der Fall, der bis hierher eine FALSCHE Zahl zeigte (8ear).

    Eine Brauchwasser-Wärmepumpe zählt als Luft-Wasser-Gerät und meldet Wärme —
    damit greift **keine** der bisherigen Sperren: ``bauarten_gemischt`` ist
    False (beide Luft-Wasser) und ``waerme_deckt_nicht_alle_geraete`` ebenfalls
    (beide melden Wärme). Ihre Warmwasser-Wärme landete im Zähler, ihr
    ungeteilter Strom in keinem Nenner ⇒ die Warmwasser-Arbeitszahl war zu hoch,
    **ohne Grund daneben**. Die Mengengleichheit fängt ihn.
    """
    a = await _anlage(db, "WP + Brauchwasser-WP")
    await _geraet(db, a, "Wärmepumpe", dict(_WP), {
        "strom_warmwasser_kwh": 400.0, "warmwasser_kwh": 1000.0,
        "strom_heizen_kwh": 800.0, "heizenergie_kwh": 2400.0,
    })
    await _geraet(db, a, "Brauchwasser-WP",
                  {"wp_art": "brauchwasser", "effizienz_modus": "gesamt_jaz"},
                  {"stromverbrauch_kwh": 300.0, "warmwasser_kwh": 900.0})
    await db.commit()
    m = await _monat(db, a.id)
    # 1900 ÷ 400 = 4,75 wäre die Zahl gewesen, die hier stand.
    assert m.wp_jaz_warmwasser is None, "Warmwasser-Wärme von zwei Geräten, Strom von einem"
    # Heizen bleibt sauber — die Brauchwasser-WP heizt nicht.
    assert m.wp_jaz_heizen == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_heizstab_angabe_sperrt_weiterhin_beide(db):
    """Die Anwender-Angabe trägt keine Funktion ⇒ keine wird freigegeben."""
    a = await _anlage(db, "Heizstab am Zähler")
    await _geraet(db, a, "Wärmepumpe", {**_WP, "abgrenzung": "fremdstrom"}, {
        "strom_heizen_kwh": 800.0, "heizenergie_kwh": 2400.0,
        "strom_warmwasser_kwh": 400.0, "warmwasser_kwh": 1000.0,
    })
    await db.commit()
    m = await _monat(db, a.id)
    assert m.wp_jaz_heizen is None and m.wp_jaz_heizen_grund == GRUND_FREMDSTROM
    assert m.wp_jaz_warmwasser is None and m.wp_jaz_warmwasser_grund == GRUND_FREMDSTROM


@pytest.mark.asyncio
async def test_sommermonat_ohne_heizbetrieb_sperrt_das_jahr_nicht(db):
    """⚠ Die Falle beim Jahres-Pfad.

    Ein Monat ohne Heizbetrieb ist nicht „unsauber abgegrenzt" — er hat die
    Funktion nicht. Wer im Jahr ``all(...)`` über **alle** Monate bildet, sperrt
    die Jahres-Heizzahl an jedem Juli.
    """
    a = await _anlage(db, "Winter + Sommer")
    wp = await _geraet(db, a, "Wärmepumpe", dict(_WP), {
        "strom_heizen_kwh": 800.0, "heizenergie_kwh": 2400.0,
    }, monat=1)
    db.add(InvestitionMonatsdaten(
        investition_id=wp.id, jahr=JAHR, monat=7,
        verbrauch_daten={"strom_warmwasser_kwh": 400.0, "warmwasser_kwh": 1000.0},
    ))
    await _geraet(db, a, "Klimaanlage", dict(_KLIMA), {"stromverbrauch_kwh": 200.0}, monat=7)
    await db.commit()
    j = await _jahr(db, a.id)
    assert j.wp_jaz_heizen == pytest.approx(3.0), "Januar trägt die Heizzahl, Juli hat keine"


@pytest.mark.asyncio
async def test_monat_mit_waerme_ohne_funktionsstrom_sperrt_das_jahr(db):
    """⭐ Den Fall hat der Sprengsatz gefunden, nicht der Entwurf.

    Januar sauber (800 kWh Strom, 2400 kWh Wärme), Februar trägt 600 kWh Wärme
    **ohne** Heizstrom. Die Jahressumme nimmt die Wärme mit und den Strom nicht:
    3000 ÷ 800 = **3,75** statt 3,0.

    ⛔ **Die Gerätezahlen des Jahres sehen das nicht** — in beiden Monaten ist
    genau ein Gerät beteiligt. Deshalb faltet der Jahres-Pfad die **Urteile je
    Monat** und nicht die Zahlen. Ein erster Entwurf verglich Maxima und war
    gegen diesen Fall blind; der Sprengsatz dazu blieb still, und genau das war
    der Befund.
    """
    a = await _anlage(db, "Waerme ohne Funktionsstrom")
    wp = await _geraet(db, a, "WP", dict(_WP), {
        "strom_heizen_kwh": 800.0, "heizenergie_kwh": 2400.0,
    }, monat=1)
    db.add(InvestitionMonatsdaten(
        investition_id=wp.id, jahr=JAHR, monat=2,
        verbrauch_daten={"heizenergie_kwh": 600.0},
    ))
    await db.commit()
    j = await _jahr(db, a.id)
    assert j.wp_jaz_heizen is None, "3,75 waere die Zahl gewesen"
    assert j.wp_jaz_heizen_grund == GRUND_FUNKTION_NICHT_DECKUNGSGLEICH
