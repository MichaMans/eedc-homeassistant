"""HA-Export — die Anlagen-Sensoren: `calculate_anlage_sensors` (Energie, Finanzen, CO₂, Prognose, Preis, Grundlast je
Anlage aus den Monats-Fakten, ADR-002/P10) und `grundlast_sensorwert`.
"""
# Reiner Umzug aus `api/routes/ha_export.py` (18.09.2026, Vorlage 8 des Refactorings grosser Dateien):
# Code 1:1 uebernommen, kein Verhaltenswechsel. Die Fassade `__init__.py` haengt den Router ein und exportiert
# die Namen weiter, die Aufrufer und Tests bisher aus dem Modul importierten.

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from collections import defaultdict
from backend.core.investition_kennwerte import (
    get_speicher_kapazitaet_kwh,
    get_speicher_nutzbare_kapazitaet_kwh,
)
from backend.core.berechnungen import (
    DienstlicheLadungZeile,
    FinanzMonatsZeile,
    berechne_dienstliche_ladekosten,
    berechne_finanz_aggregat,
    berechne_wp_alternativkosten_ersparnis,
    berechne_spez_ertrag_annualisiert,
    berechne_verbrauchs_kennzahlen,
    erzeugung_hinter_zaehler_kwh,
    imd_typ_beitrag,
    monatsgewichte_aus_pvgis,
    relevante_kosten_aus_investitionen,
    speicher_wirkungsgrad,
    vollzyklen as berechne_vollzyklen,
)
from backend.services.prognose_auswahl import lade_aktive_prognose
from datetime import date
from backend.services.strompreis_aggregator import (
    lade_preis_aggregate_je_monat,
    aufgeloester_monatspreis,
)
from backend.services.finanz_zeilen import baue_finanz_zeile
from backend.services.monats_fakten import finanz_zeile_eingabe, lade_monats_fakten
from backend.api.routes.strompreise import (
    lade_tarife_fuer_anlage,
    resolve_strompreis_for_komponente,
)
from backend.core.field_definitions import get_emob_pv_netz_kwh
from backend.core.berechnungen.kapitalrechnung import (
    ErsparnisPosten,
    annahme_dauer_text,
    erklaerung_jahres_ersparnis,
    jahres_ersparnis_euro,
    kapitaleinsatz_euro,
)
from backend.services.eauto_wirtschaftlichkeit import (
    berechne_eauto_ersparnis_periode,
    eigener_verbrauch_l_100km,
)
from backend.models.anlage import Anlage
from backend.models.monatsdaten import Monatsdaten
from backend.core.berechnungen.investitions_jahresertrag import (
    BEZEICHNUNG_ERTRAGSFELD,
    jahresertrag_posten,
)
from backend.models.investition import ERTRAGSFELD_TYPEN, Investition, InvestitionMonatsdaten
from backend.utils.investition_filter import aktiv_jetzt
from backend.services.ha_sensors_export import (
    SensorValue,
    ANLAGE_SENSOREN,
    INVESTITION_SENSOREN,
    SPEICHER_SENSOREN,
    LETZTER_IMPORT_SENSOREN,
    PROGNOSE_SENSOREN,
    PREIS_SENSOREN,
)
from backend.services.ha_export_prognose import berechne_prognose_export
from backend.services.ha_export_preis import berechne_preis_export
from backend.core.investition_parameter import PARAM_E_AUTO, PARAM_E_AUTO_DEFAULTS, ist_dienstlich
from backend.core.calculations import berechne_co2_bilanz
from backend.core.berechnungen.ust_eigenverbrauch import (
    UstJahresanteil,
    bemessungsgrundlage_aus_investitionen,
    ust_eigenverbrauch_fuer_anlage,
)
from backend.api.routes.ha_export.emob import _build_emob_pool_ctx, _emob_month_share, _reichere_emob_imd_an


async def grundlast_sensorwert(
    db: AsyncSession, anlage_id: int, heute: date
) -> tuple[Optional[float], Optional[str]]:
    """Die **gemessene** Grundlast des laufenden Monats für den HA-Sensor (#395/5).

    ⛔ **Bewusst NICHT der Live-Wert.** „Grundlast" bezeichnet in eedc zwei
    verschiedene Zahlen (Fund N-332): Cockpit → Live zeigt den Median über das
    Verbrauchs-**Profil** — ohne eigene Historie ist das ein BDEW-H0-Modellwert
    mit 0,3 kW Default. Hier zählt der Median der **gemessenen** Nachtstunden,
    dieselbe Quelle und derselbe Layer-SoT wie die Kachel in Cockpit → Monat.
    Ein Modellwert, der als Sensor in eine Automation läuft, ist die teuerste
    Sorte Zahl: er sieht aus wie eine Messung.

    ⭐ **`heute` ist ein Parameter und kein `date.today()` in dieser Funktion** —
    dasselbe Muster wie `core/hub_leer_grund.py`. Sonst müsste jede Probe die
    Prozessuhr lesen und damit auf die Stunde ihres Laufs wetten (N-167: vier
    von 24 Stunden rot ohne Code-Änderung). Der Aufrufer setzt die Uhr ein, die
    Funktion rechnet.

    Returns:
        ``(kW, Rechenweg)`` — ``(None, None)``, wenn keine Nachtstunde gemessen
        ist. Dann entsteht **kein** Sensor.
    """
    from backend.api.routes.aktueller_monat import _load_grundlast_nacht_kw
    from backend.core.berechnungen import berechne_grundlast

    nacht = await _load_grundlast_nacht_kw(anlage_id, heute.year, heute.month, db)
    if not nacht:
        return None, None
    kennzahlen = berechne_grundlast(
        nacht_verbrauch_kw=nacht, gesamtverbrauch_kwh=None, tage=heute.day,
    )
    if kennzahlen.grundlast_kw is None:
        return None, None
    return kennzahlen.grundlast_kw, (
        f"Median über {len(nacht)} gemessene Nachtstunden (0–5 Uhr) im laufenden Monat"
    )

async def calculate_anlage_sensors(
    db: AsyncSession,
    anlage: Anlage
) -> list[SensorValue]:
    """
    Berechnet alle Sensor-Werte für eine Anlage.

    WICHTIG: PV-Erzeugung kommt aus InvestitionMonatsdaten (pro PV-Modul),
    NICHT aus Monatsdaten.pv_erzeugung_kwh (Legacy-Feld!).
    Einspeisung/Netzbezug kommen aus Monatsdaten (Zählerwerte).
    """
    # Monatsdaten laden (für Zählerwerte: einspeisung, netzbezug)
    result = await db.execute(
        select(Monatsdaten).where(Monatsdaten.anlage_id == anlage.id)
    )
    monatsdaten = result.scalars().all()

    if not monatsdaten:
        return []

    # N-200: Der Tarif kommt aus dem SoT, nicht aus einer Handquery. Die alte
    # Form (`order_by(gueltig_ab.desc()).limit(1)`) verlor ZWEI Filter, die
    # `lade_tarife_fuer_anlage` mitbringt:
    #
    #   * `gueltig_bis` — ein ausgelaufener Tarif galt weiter als „aktuellster";
    #   * `verwendung`  — ist der zuletzt angelegte Tarif ein WP- oder
    #     Wallbox-Spezialtarif, wurde er hier als ALLGEMEINER Netzbezugspreis
    #     gelesen. Genau die Fallunterscheidung, die der SoT trifft.
    #
    # Dazu faellt ein `gueltig_ab` in der Zukunft nicht mehr auf heute durch.
    # Dieselbe Umstellung, die D5 fuer den Daten-Checker gefahren hat.
    # Das Ergebnis wird unten als `_tarife` weiterbenutzt (WP-/Wallbox-Zweig) —
    # der zweite Ladevorgang von damals entfaellt damit.
    _tarife = await lade_tarife_fuer_anlage(db, anlage.id)
    strompreis = _tarife["allgemein"]

    # Investitionen laden für ROI-Berechnung
    result = await db.execute(
        select(Investition)
        .where(Investition.anlage_id == anlage.id)
        .where(aktiv_jetzt())
    )
    investitionen = result.scalars().all()

    # =====================================================================
    # MONATSZEILE AUS DEN MONATS-FAKTEN (ADR-002/P10)
    # =====================================================================
    # Bis 2026-07-31 hat diese Funktion die IMD-Zeilen in VIER getrennten
    # Queries + Schleifen selbst gefaltet (Erzeuger · Speicher · V2H · sonstige
    # Positionen), jede mit ihrer eigenen Filter-Handschrift. Die Rechnung war
    # korrekt — Befund F-5 der Drift-Inventur traf andere Sichten. Umgehängt
    # wird sie trotzdem, weil eine selbst faltende Sicht die nächste Drift-Quelle
    # ist (`docs/KONZEPT-MONATS-FAKTEN.md` §11).
    #
    # Die Schicht lädt Investitionen bewusst OHNE `aktiv`-Vorfilter und
    # entscheidet je Monat über `ist_aktiv_im_monat` (#123: historische
    # Kennzahlen dürfen später stillgelegte Komponenten nicht rückwirkend
    # ausblenden). Der `aktiv_jetzt()`-Vorfilter oben bleibt für alles, was einen
    # HEUTIGEN Zustand beschreibt (kWp, Investitionssummen, Komponenten-Listen);
    # die MENGEN kommen ab jetzt aus der Schicht. Das ist derselbe Schnitt wie
    # im Cockpit — und es ist eine bewegte Zahl für Anlagen mit stillgelegter
    # Komponente (s. Übergabe N-11).
    _tarif_cache: dict[date, dict] = {}
    # Wie der Tarif-Cache: EINE gruppierte Preismessung für Schicht und Finanzzeile.
    _preis_messung = await lade_preis_aggregate_je_monat(db, anlage.id)
    fakten = await lade_monats_fakten(
        db, anlage.id, tarif_cache=_tarif_cache, preis_messung=_preis_messung,
    )

    # PV je Monat über den Read-time-SoT (Messwerte + Aggregat-Lückenfüllung)
    # statt einer rohen IMD-Summe. Die rohe Summe kannte das Anlagen-Aggregat
    # gar nicht: eine Anlage, deren frühe Monate nur als Gesamtwert vorliegen
    # (Umstellung auf Pro-String-Messung mitten in der Historie), verlor diese
    # Monate im PV-Sensor, im spezifischen Ertrag und in den Finanzzeilen.
    #
    # DI-2-B: Erzeuger hinter dem EINEN Hauszähler zählen in die EV-/Autarkie-/
    # CO₂-Bilanz — deckungsgleich mit dem Cockpit (Layer-SoT
    # `erzeugung_hinter_zaehler_kwh`, v3.45.4):
    #   • Balkonkraftwerk zählt als PV (Cockpit-Konvention) → in `pv_erzeugung`.
    #   • Sonstige Erzeuger (Mini-BHKW/KWK) speisen ebenfalls hinter den Zähler →
    #     zählen in EV/Autarkie, bleiben aber aus den PV-eigenen Kennzahlen
    #     (spez. Ertrag/PR) und aus der PV-Erzeugungs-Anzeige draußen.
    # Falle 1 der S1-Übergabe: `erzeugung.pv_kwh` für die PV-Achse und die
    # Finanz-Zeile, `erzeugung.hinter_zaehler_kwh` für die Bilanz.
    pv_erzeugung = sum(f.erzeugung.pv_kwh for f in fakten)
    sonstiges_erzeugung = sum(f.sonstiges.erzeugung_kwh for f in fakten)

    # Fallback: Falls keine InvestitionMonatsdaten vorhanden, berechne aus Einspeisung
    einspeisung = sum(m.einspeisung_kwh or 0 for m in monatsdaten)
    if pv_erzeugung == 0:
        # Schätzung: Erzeugung ≈ Einspeisung + geschätzter Eigenverbrauch
        pv_erzeugung = einspeisung + sum(m.eigenverbrauch_kwh or 0 for m in monatsdaten)

    # #304: netzbezug ist ein Zählerwert aus Monatsdaten (legitim). Eigen-/
    # Direkt-/Gesamtverbrauch NICHT aus den berechneten Legacy-Monatsdaten-
    # Feldern lesen — die bleiben bei IMD-basierten Setups leer (moderne
    # Quellen schreiben in InvestitionMonatsdaten), wodurch die Eigenverbrauchs-
    # quote zusammenbricht (2,2 % statt ~40 %). Sie werden unten zentral aus
    # PV(IMD) + Speicher(IMD) + Zählerwerten über den SoT-Helper berechnet.
    netzbezug = sum(m.netzbezug_kwh or 0 for m in monatsdaten)

    # Speicher-Summen aus den Monats-Fakten (kanonisch über `imd_typ_beitrag`)
    # statt Legacy Monatsdaten. Die frühere Handschrift las die Roh-Schlüssel
    # `ladung_kwh`/`entladung_kwh` direkt (P6-Klasse).
    batterie_ladung = sum(f.speicher.ladung_kwh for f in fakten)
    batterie_entladung = sum(f.speicher.entladung_kwh for f in fakten)

    # Fallback auf Legacy wenn keine InvestitionMonatsdaten
    if batterie_ladung == 0 and batterie_entladung == 0:
        batterie_ladung = sum(m.batterie_ladung_kwh or 0 for m in monatsdaten)
        batterie_entladung = sum(m.batterie_entladung_kwh or 0 for m in monatsdaten)

    # V2H (E-Auto → Haus) wird wie Speicher-Entladung als Eigenverbrauch gezählt.
    # Dienstwagen sind darin nicht mehr enthalten — die Schicht filtert sie,
    # die frühere Schleife hier nicht ([[feedback_dienstwagen_alle_checks]]).
    v2h_entladung = sum(f.emob.v2h_entladung_kwh for f in fakten)

    # #304: Eigenverbrauch/Direktverbrauch/Gesamtverbrauch + Quoten zentral über
    # den SoT-Helper aus IMD-gesourcten Energiemengen (PV + Speicher + V2H) und
    # den Zählerwerten (Einspeisung/Netzbezug) — kanonische Formel, deckungs-
    # gleich mit cockpit/uebersicht.py.
    # DI-2-B: Netzpunkt-Bilanz-Eingang = PV(inkl. BKW) + sonstige Erzeuger,
    # deckungsgleich mit dem Cockpit (`erzeugung_bilanz`, uebersicht.py:416).
    # `pv_erzeugung` selbst (inkl. BKW, ohne BHKW) bleibt für die PV-eigenen
    # Kennzahlen (spez. Ertrag) und die PV-Erzeugungs-Anzeige.
    erzeugung_bilanz = erzeugung_hinter_zaehler_kwh(pv_erzeugung, sonstiges_erzeugung)
    kennzahlen = berechne_verbrauchs_kennzahlen(
        pv_erzeugung_kwh=erzeugung_bilanz,
        einspeisung_kwh=einspeisung,
        netzbezug_kwh=netzbezug,
        speicher_ladung_kwh=batterie_ladung,
        speicher_entladung_kwh=batterie_entladung,
        v2h_entladung_kwh=v2h_entladung,
        abgabe_dritte_kwh=sum(f.sonstiges.abgabe_kwh for f in fakten),
    )
    direktverbrauch = kennzahlen.direktverbrauch_kwh
    eigenverbrauch = kennzahlen.eigenverbrauch_kwh
    gesamtverbrauch = kennzahlen.gesamtverbrauch_kwh
    autarkie = kennzahlen.autarkie_prozent
    ev_quote = kennzahlen.eigenverbrauchsquote_prozent
    # Spezifischer Ertrag — annualisiert über den SoT-Helper, deckungsgleich
    # mit der Cockpit-Kachel (Rainer-PN 2026-06-11: die alte Roh-Division
    # Lebenszeit-kWh ÷ heutiges kWp lieferte einen über die Laufzeit
    # aufkumulierten Wert, ~3× Jahreswert bei 3 Jahren Historie).
    # Aus derselben Quelle wie `pv_erzeugung`: jeder Monat mit aufgelöster PV
    # zählt, gemessen ODER über das Aggregat gefüllt. Der frühere
    # Zwei-Wege-Aufbau (IMD-Monate, sonst Monate mit Legacy-PV>0) ließ bei
    # gemischter Historie die Aggregat-Monate aus und machte den spezifischen
    # Ertrag dadurch zu hoch.
    spez_covered_months = {f.schluessel for f in fakten if f.erzeugung.pv_je_modul}
    spez_gewichte = None
    if spez_covered_months:
        pvgis = await lade_aktive_prognose(db, anlage.id)
        spez_gewichte = monatsgewichte_aus_pvgis(
            pvgis.monatswerte if pvgis else None
        ) or None
    spez_ertrag = berechne_spez_ertrag_annualisiert(
        pv_erzeugung_kwh=pv_erzeugung,
        covered_months=spez_covered_months,
        investitionen=investitionen,
        fallback_kwp=anlage.leistung_kwp or 0.0,
        monatsgewichte=spez_gewichte,
    )

    # Finanzen (#326) — über den SoT-Helper `berechne_finanz_aggregat`, damit
    # HA-Export dieselbe Netto-Ertrag-Zahl liefert wie Cockpit/Jahresbericht.
    # Einspeise-Erlös §51-bereinigt + EV-Ersparnis pro Monat mit dem Monats-
    # Flexpreis (`resolve_netzbezug_preis_cent` → Fallback fixer Tarif). Anwender
    # ohne Strompreis-Sensor (m_neg=None) sehen die alte ungekürzte Berechnung;
    # bei vorhandenem Tages-Aggregat wird die in Negativpreis-Stunden
    # eingespeiste kWh-Menge unvergütet. Sonstige (manuell gepflegt) wie im
    # Cockpit im Netto-Ertrag.
    # Sonstige Positionen: aus den Monats-Fakten — sie falten IMD-Positionen
    # (typ-unabhängig, #310) UND die Basis-Positionen der Monatsdaten-Zeile
    # (G19-1) an einer Stelle, gleiche Netto-Faltung wie Cockpit/Jahresbericht.
    sonstige_netto_gesamt = sum(f.sonstiges.netto_euro for f in fakten)
    # Konzept §9 Weg 2 — eigener Summand neben dem Anlagen-Einspeiseerlös.
    erzeuger_erloes_gesamt = sum(f.sonstiges.einspeise_erloes_euro for f in fakten)
    # F-19: die AUSGABEN gehen in den Kapitaleinsatz. Die dienstlichen
    # Ladekosten weiter unten gehören ausdrücklich nicht hierher — sie sind
    # laufender Aufwand, kein eingesetztes Kapital.
    sonstige_ausgaben_gesamt = sum(f.sonstiges.ausgaben_euro for f in fakten)
    # §8/3: dasselbe für die Ertragsseite — gebraucht, um die Projektion der
    # Jahres-Ersparnis unten von den gepflegten Positionen zu befreien.
    # Bauschritt 7 (2026-08-10): und sie **mindern den Kapitaleinsatz**. Der
    # Sensor `netto_ertrag_euro` behält beide Seiten (Zeitraum-Bilanz).
    sonstige_ertraege_gesamt = sum(f.sonstiges.ertraege_euro for f in fakten)

    # Dienstliche Ladekosten — bis 2026-07-31 hat der HA-Export sie als einzige
    # der drei Sichten **gar nicht** abgezogen (N-13): der Sensor
    # `netto_ertrag_euro` stand bei Dienstwagen-Anlagen über der Cockpit-Kachel,
    # auf die er sich bezieht. Gleiche Formel, gleicher Layer-SoT
    # (ADR-001) wie Cockpit/Übersicht und Aussichten; die Mengen kommen aus den
    # Monats-Fakten (Dienstwagen-Filter + PV/Netz-Split, P10).
    sonstige_netto_gesamt -= berechne_dienstliche_ladekosten(
        DienstlicheLadungZeile(
            ladung_pv_kwh=f.emob.dienstlich_ladung_pv_kwh,
            ladung_netz_kwh=f.emob.dienstlich_ladung_netz_kwh,
            netzbezug_preis_cent=f.tarif.netzbezug_preis_cent,
            wallbox_preis_cent=f.tarif.wallbox_preis_effektiv_cent,
        )
        for f in fakten
    ).gesamt_euro

    einspeise_erloes = 0
    ev_ersparnis = 0
    netto_ertrag = sonstige_netto_gesamt
    if strompreis:
        # #326: FinanzMonatsZeile über den gemeinsamen Builder (einzige erlaubte
        # Konstruktions-Stelle, Wächter) — er löst den Tarif PRO MONAT auf
        # (historische Tarife via gueltig_ab/gueltig_bis), nicht den neuesten
        # Strompreis für alle Jahre. Deckungsgleich mit Cockpit/Jahresbericht
        # (rilmor-mhrs: Jahres-Tarife 23,90→32,80 ct). Die Eingabe entsteht aus
        # dem Monats-Fakt (P10) statt aus fünf site-eigenen Maps; `pv_kwh` darin
        # ist „Module + BKW" (P9) mit `None`-Auflösung als 0 statt als Teilsumme
        # (N42). Nur Monate MIT Zählerzeile — ohne gemessene Einspeisung/Bezug
        # gibt es keine Finanz-Zeile.
        finanz_zeilen: list[FinanzMonatsZeile] = [
            await baue_finanz_zeile(
                db, anlage.id, finanz_zeile_eingabe(f), tarif_cache=_tarif_cache,
                preis_messung=_preis_messung,
            )
            for f in fakten if f.meta.hat_zaehlerzeile
        ]
        _finanz = berechne_finanz_aggregat(
            finanz_zeilen, sonstige_netto_euro=sonstige_netto_gesamt,
            erzeuger_erloes_euro=erzeuger_erloes_gesamt
        )
        einspeise_erloes = _finanz.einspeise_erloes_euro
        ev_ersparnis = _finanz.ev_ersparnis_euro
        netto_ertrag = _finanz.netto_ertrag_euro

    # CO2 (DI-2): der HA-Sensor „CO2 Einsparung" trägt jetzt die volle
    # Cockpit-Bilanz (PV-Eigenverbrauch + WP + E-Mobilität) statt nur
    # `pv_erzeugung × f_strom`. Berechnung weiter unten, nachdem WP- und
    # E-Mob-Aggregate stehen (kanonischer Helper `berechne_co2_bilanz`).

    # Investitions-KPIs berechnen
    investition_gesamt = sum(i.anschaffungskosten_gesamt or 0 for i in investitionen)
    # N-352: über den Layer-SoT, nicht als eigene Form daneben. Hier stand bis
    # 2026-08-30 `Σ gesamt − Σ alternativ` — **ungeklemmt und anlagenweit**,
    # wortgleich die Form, die N-136 im Jahresbericht-PDF behoben hat. Der SoT
    # klemmt **je Position**; trägt eine Position eine teurere Alternative (ein
    # Verbrenner gegen ein E-Auto ist der Regelfall), zog ihr Überschuss die
    # Mehrkosten der **anderen** Positionen herunter.
    # ⚑ DAS TRAF ZWEI AUSGELIEFERTE SENSOREN: `roi_prozent` zu hoch und
    # `amortisation_jahre` zu kurz (an der Probe gemessen: 7,81 statt 10,42
    # Jahre). Die Korrektur ist eine **Wertänderung** und als solche gemeldet.
    relevante_kosten = relevante_kosten_aus_investitionen(investitionen)
    betriebskosten_ges = sum(i.betriebskosten_jahr or 0 for i in investitionen)
    # §8/2: das Gegenstück auf der Ertragsseite. `investitionen` ist bereits
    # `aktiv_jetzt()`-gefiltert (heutiger Zustand), es fehlt nur die Typ-Grenze
    # — nur dort wird das Feld gepflegt und vom ROI-Dashboard gelesen. Ohne
    # diesen Summanden trügen die HA-Sensoren `jahres_ersparnis_euro`,
    # `roi_prozent` und `amortisation_jahre` eine andere Zahl als die
    # Oberfläche, sobald jemand einen Jahres-Ertrag pflegt.
    # §9.2 Geldseite (11c): über denselben SoT wie ROI-Dashboard und
    # Aussichten. `investitionen` ist bereits `aktiv_jetzt()`-gefiltert
    # (`:421`) — ein Sensor ist eine Prognose, er zählt nur, was heute läuft;
    # das ROI-Dashboard filtert bewusst anders (#123).
    #
    # ⛔ **Hier zählt NUR der geschätzte Posten extra — und das ist der
    # Unterschied zu den Aussichten.** Der gemessene Abgabe-Erlös steckt in
    # dieser Sicht bereits in `bilanz_ohne_sonstige` (er ist seit `:602` Teil
    # von `_finanz.netto_ertrag_euro`) und würde als zweiter Summand doppelt
    # zählen. **Gemessen am 06.09.2026:** ohne diese Grenze meldete der Sensor
    # `jahres_ersparnis_euro` **960 € statt 480 €** — die Probe
    # `test_abgabe_geldseite_drei_sichten.py` hat es beim ersten Lauf gefangen.
    # Die Aussichten haben das Problem nicht: dort ist der Erlös in keiner
    # anderen Prognose-Größe enthalten.
    #
    # ⚑ Der SoT wird trotzdem gebraucht, und zwar für den **Vorrang**: Pflegt
    # jemand an einem Abgabe-Gerät BEIDES, liefert `jahresertrag_posten` den
    # gemessenen Posten — dessen Bezeichnung filtern wir hier heraus, und das
    # statische „Ertrag/Jahr" desselben Geräts zählt damit korrekt NICHT mit.
    _ertrag_posten = [
        p
        for i in investitionen
        if i.typ in ERTRAGSFELD_TYPEN
        for p in (jahresertrag_posten(i, fakten),)
        if p is not None and p.bezeichnung == BEZEICHNUNG_ERTRAGSFELD
    ]
    jahres_ertraege_ges = jahres_ersparnis_euro(_ertrag_posten)

    # #326-Inventur Dimension 2: USt auf Eigenverbrauch bei Regelbesteuerung.
    # Cockpit und Aussichten ziehen sie ab, der HA-Export bisher nicht — der
    # Sensor `netto_ertrag_euro` stand damit um den USt-Betrag über der Kachel,
    # auf die er sich bezieht. Vorprüfung im SoT-Helper.
    #
    # N-129 + N-130: Bemessungsgrundlage jetzt Mehrkosten statt Vollkosten, und
    # gerechnet wird je Kalenderjahr. Der Export kennt keinen Jahres-Filter — er
    # liefert IMMER den Gesamtzeitraum und war deshalb von der Zeitraum-Kollaps-
    # Klasse durchgehend betroffen, nicht nur bei gesetztem Filter.
    # Eingänge je Jahr wie die Perioden-Kennzahlen oben, inkl. derselben
    # Legacy-Fallbacks (dort periodenweit, hier je Jahr geprüft).
    monatsdaten_je_jahr: dict[int, list] = defaultdict(list)
    for _md in monatsdaten:
        monatsdaten_je_jahr[_md.jahr].append(_md)
    fakten_je_jahr: dict[int, list] = defaultdict(list)
    for _f in fakten:
        fakten_je_jahr[_f.jahr].append(_f)

    ust_jahresanteile: list[UstJahresanteil] = []
    for _jahr in sorted(set(monatsdaten_je_jahr) | set(fakten_je_jahr)):
        _f_jahr = fakten_je_jahr.get(_jahr, [])
        _md_jahr = monatsdaten_je_jahr.get(_jahr, [])
        _eins_jahr = sum(m.einspeisung_kwh or 0 for m in _md_jahr)
        _pv_jahr = sum(f.erzeugung.pv_kwh for f in _f_jahr)
        if _pv_jahr == 0:
            _pv_jahr = _eins_jahr + sum(m.eigenverbrauch_kwh or 0 for m in _md_jahr)
        _lad_jahr = sum(f.speicher.ladung_kwh for f in _f_jahr)
        _entl_jahr = sum(f.speicher.entladung_kwh for f in _f_jahr)
        if _lad_jahr == 0 and _entl_jahr == 0:
            _lad_jahr = sum(m.batterie_ladung_kwh or 0 for m in _md_jahr)
            _entl_jahr = sum(m.batterie_entladung_kwh or 0 for m in _md_jahr)
        _kz_jahr = berechne_verbrauchs_kennzahlen(
            pv_erzeugung_kwh=erzeugung_hinter_zaehler_kwh(
                _pv_jahr, sum(f.sonstiges.erzeugung_kwh for f in _f_jahr)
            ),
            einspeisung_kwh=_eins_jahr,
            netzbezug_kwh=sum(m.netzbezug_kwh or 0 for m in _md_jahr),
            speicher_ladung_kwh=_lad_jahr,
            speicher_entladung_kwh=_entl_jahr,
            v2h_entladung_kwh=sum(f.emob.v2h_entladung_kwh for f in _f_jahr),
            abgabe_dritte_kwh=sum(f.sonstiges.abgabe_kwh for f in _f_jahr),
        )
        ust_jahresanteile.append(UstJahresanteil(
            jahr=_jahr,
            eigenverbrauch_kwh=_kz_jahr.eigenverbrauch_kwh,
            pv_kwh=_pv_jahr,
            monate=max(len(_f_jahr), len(_md_jahr)),
        ))
    ust_eigenverbrauch = ust_eigenverbrauch_fuer_anlage(
        anlage,
        jahresanteile=ust_jahresanteile,
        bemessungsgrundlage_euro=bemessungsgrundlage_aus_investitionen(investitionen),
        betriebskosten_jahr_euro=betriebskosten_ges,
    )
    netto_ertrag -= ust_eigenverbrauch

    # Alternativkosten-Ersparnisse aus historischen InvestitionMonatsdaten:
    # WP vs. Gas/Öl, E-Auto vs. Benzin, BKW-Eigenverbrauch.
    # Ohne diese Komponenten wäre die Jahresersparnis nur PV-Netto-Ertrag,
    # was bei Anlagen mit WP/E-Auto zu absurd langer Amortisation führt.
    waermepumpen = [i for i in investitionen if i.typ == "waermepumpe"]
    e_autos = [
        i for i in investitionen
        if i.typ == "e-auto" and not ist_dienstlich(i)
    ]
    wallboxen = [
        i for i in investitionen
        if i.typ == "wallbox" and not ist_dienstlich(i)
    ]

    # IMD vor anschaffungsdatum / nach stilllegungsdatum überspringen (#236):
    # Sonst fließen Werte in HA-Sensor-Aggregate ein, obwohl die Komponente
    # in dem Monat noch gar nicht / nicht mehr aktiv war.
    historische_inv_daten: dict[tuple[int, int, int], dict] = {}
    inv_ids = [i.id for i in investitionen]
    inv_by_id_export = {i.id: i for i in investitionen}
    if inv_ids:
        imd_alle = await db.execute(
            select(InvestitionMonatsdaten)
            .where(InvestitionMonatsdaten.investition_id.in_(inv_ids))
        )
        for imd in imd_alle.scalars().all():
            inv = inv_by_id_export.get(imd.investition_id)
            if not inv or not inv.ist_aktiv_im_monat(imd.jahr, imd.monat):
                continue
            historische_inv_daten[(imd.investition_id, imd.jahr, imd.monat)] = (
                imd.verbrauch_daten or {}
            )

    # F-16: die E-Mob-Zeilen bekommen ihren abgeleiteten PV-Anteil, bevor
    # irgendetwas daraus gerechnet wird — der Pool-Kontext unten und die
    # Ersparnis-Schleife weiter unten schöpfen beide aus dieser Map.
    _emob_ids = {
        i.id for i in investitionen
        if i.typ in ("e-auto", "wallbox") and not ist_dienstlich(i)
    }
    if _emob_ids:
        _angereichert = await _reichere_emob_imd_an(
            db,
            anlage.id,
            {
                key: daten
                for key, daten in historische_inv_daten.items()
                if key[0] in _emob_ids
            },
            {w.id for w in wallboxen},
        )
        historische_inv_daten.update(_angereichert)

    # Phase 2a: Emob-Pool-Kontext aus den bereits aktiv-gefilterten IMD bauen.
    # Liegt die Heimladung kanonisch auf der Wallbox (evcc), zieht die
    # E-Auto-Ersparnis unten den km-anteiligen Wallbox-Netz-Anteil statt des
    # (leeren) E-Auto-Netz — sonst würde `bisherige_eauto_ersparnis` keinen
    # Netzstrom abziehen und die Ersparnis überhöhen.
    emob_ctx = _build_emob_pool_ctx(
        historische_inv_daten,
        {e.id for e in e_autos},
        {w.id for w in wallboxen},
    )

    netzbezug_preis_cent = (
        strompreis.netzbezug_arbeitspreis_cent_kwh if strompreis else 30.0
    )

    # Monatsdaten-Dict für Monats-Gaspreis / -Benzinpreis
    md_by_periode = {(md.jahr, md.monat): md for md in monatsdaten}

    # DI-4: WP-Strom mit dem WP-Spezialtarif bewerten (Fallback allgemein), wie
    # in aktueller_monat.py — sonst rechnet der HA-Export die WP-Ersparnis mit
    # dem allgemeinen Netzbezugspreis, obwohl ein günstigerer WP-Tarif gepflegt ist.
    # ADR-002/P8: JE MONAT auflösen — die WP-Ersparnis summiert über die ganze
    # Historie, ein Einheitstarif hätte einen Tarifwechsel rückwirkend über alle
    # Jahre gezogen (dieselbe Klasse wie der Jahresbericht-Drift, #326).
    wp_preis_by_periode: dict[tuple[int, int], float] = {}
    for (_inv_id, _p_jahr, _p_monat) in historische_inv_daten:
        _periode = (_p_jahr, _p_monat)
        if _periode in wp_preis_by_periode:
            continue
        _p_tarife = await lade_tarife_fuer_anlage(
            db, anlage.id, target_date=date(_p_jahr, _p_monat, 1)
        )
        wp_preis_by_periode[_periode] = resolve_strompreis_for_komponente(
            _p_tarife, "waermepumpe", fallback=netzbezug_preis_cent
        )

    # Heutiger WP-Tarif: Fallback für Monate ohne Auflösung und Grundlage der
    # nach vorn gerichteten Sensor-Werte weiter unten. `_tarife` steht seit
    # N-200 schon oben (dieselbe Abfrage, ein Ladevorgang).
    wp_netzbezug_preis_cent = resolve_strompreis_for_komponente(
        _tarife, "waermepumpe", fallback=netzbezug_preis_cent
    )

    # F-18: dasselbe für die WALLBOX. Die WP hatte ihre Monats-Auflösung seit
    # v4.0.5 (#326), die E-Mob-Seite nicht — `bisherige_eauto_ersparnis` unten
    # bewertete die Netzladung der ganzen Historie mit dem HEUTIGEN Tarif und
    # driftete damit gegen den Komponenten-Hub, der längst mittelte. Der Wert
    # steckt über `historischer_netto_ertrag` in vier ausgelieferten Sensoren
    # (`netto_ertrag_euro` · `roi_prozent` · `amortisation_jahre` und dem
    # per-Investition-Sensor `e_auto_ersparnis_vs_benzin_euro`).
    # ⭐ **#412 (11.09.2026): auch dieser Sensor sieht die volle Kaskade.** Bis
    # dahin las die Schleife allein den Wallbox-Tarif — der **abgerechnete**
    # Monats-Ø kam nie an, während Cockpit → Übersicht ihn für dieselbe
    # Ersparnis längst nahm. Zwei Zahlen für eine Größe, je nach Sicht.
    _md_result = await db.execute(
        select(Monatsdaten).where(Monatsdaten.anlage_id == anlage.id)
    )
    _md_je_monat = {(m.jahr, m.monat): m for m in _md_result.scalars().all()}
    _preis_cache_wb: dict = {}
    wallbox_preis_by_periode: dict[tuple[int, int], float] = {}
    for (_inv_id, _p_jahr, _p_monat) in historische_inv_daten:
        _periode = (_p_jahr, _p_monat)
        if _periode in wallbox_preis_by_periode:
            continue
        _p_tarife = await lade_tarife_fuer_anlage(
            db, anlage.id, target_date=date(_p_jahr, _p_monat, 1)
        )
        # Der Wallbox-Tarif bleibt Stufe 4 (`stammpreis_override`) — er geht
        # nicht verloren, nur ein gepflegter oder gemessener Ø schlägt ihn.
        wallbox_preis_by_periode[_periode] = (await aufgeloester_monatspreis(
            db, anlage.id, _p_jahr, _p_monat, _md_je_monat.get(_periode),
            _p_tarife.get("allgemein"),
            stammpreis_override=resolve_strompreis_for_komponente(
                _p_tarife, "wallbox", fallback=netzbezug_preis_cent
            ),
            cache=_preis_cache_wb,
        )).cent
    wallbox_netzbezug_preis_cent = resolve_strompreis_for_komponente(
        _tarife, "wallbox", fallback=netzbezug_preis_cent
    )

    # WP-Alternativkosten (vs. Gas/Öl) über den Berechnungs-Layer (ADR-001):
    # per-WP-Parameter (kein last-write-wins über waermepumpen), per-Monat-
    # Gaspreis aus Monatsdaten mit Fallback auf den WP-Parameter-Default.
    # F-20: **je Wärmepumpe einzeln**, damit jeder Posten seine eigene
    # Monatszahl behält. Ein Sammelaufruf lieferte nur eine Summe — und die
    # wurde anschließend mit der Monatszahl der ANLAGE annualisiert, obwohl
    # eine 2024 nachgerüstete WP nur einen Teil davon lief.
    bisherige_wp_ersparnis = 0.0
    wp_posten: list[ErsparnisPosten] = []
    for _wp in waermepumpen:
        _wp_summe = berechne_wp_alternativkosten_ersparnis(
            [_wp],
            historische_inv_daten,
            {k: md.gaspreis_cent_kwh for k, md in md_by_periode.items()},
            wp_preis_by_periode,
            wp_netzbezug_preis_cent,
        )
        _wp_monate = len({
            (j, m) for (inv_id, j, m) in historische_inv_daten if inv_id == _wp.id
        })
        bisherige_wp_ersparnis += _wp_summe
        wp_posten.append(ErsparnisPosten(
            bezeichnung=f"Wärmepumpe {_wp.bezeichnung}",
            summe_euro=_wp_summe,
            monate=_wp_monate,
        ))

    # Per-E-Auto-Aufschlüsselung der bisherige-Ersparnis. Vorher las eine
    # `for ea`-Schleife `benzinpreis_default` + `vergleich_l_100km` in zwei
    # globale Variablen (last-write-wins). Bei zwei E-Autos mit
    # unterschiedlichen Parametern wurden BEIDE mit den Werten des LETZTEN
    # gerechnet → `jahres_ersparnis_euro`, `roi_prozent` und
    # `amortisation_jahre`-HA-Sensoren waren falsch. Zusätzlich fehlte der
    # `md.kraftstoffpreis_euro`-Monatspreis-Fallback (EU OB) — der Anlage-
    # Sensor driftete deshalb auch gegen den per-Investition-Sensor
    # `e_auto_ersparnis_vs_benzin_euro` (Zeile 583+, der hatte den Fallback).
    bisherige_eauto_ersparnis = 0.0
    # DI-2: E-Mob-CO₂-Aggregate (Dienstwagen bereits über e_autos ausgeschlossen,
    # deckungsgleich mit der Cockpit-Bilanz): gefahrene km, Heim-Netzladung und
    # der Benzin-Vergleichsverbrauch in Litern (je Fahrzeug sein eigener Wert).
    co2_emob_km = 0.0
    co2_emob_netz_kwh = 0.0
    co2_benzin_liter = 0.0
    # #331: die PHEV-Aufteilung wird NACH der Schleife je Fahrzeug **einmal für
    # den ganzen Zeitraum** bestimmt — exakt wie in
    # `berechne_eauto_ersparnis_periode`, die das Cockpit rechnet. Monatsweise
    # aufzuteilen wäre genauer und würde genau deshalb driften.
    #
    # N-181/F-18: die **Rechnung** liegt seit 2026-08-08 im Layer-SoT
    # `berechne_eauto_ersparnis_periode`; diese Schleife sammelt nur noch die
    # Eingänge und die CO₂-Aggregate. Vorher stand hier eine wortgleiche Kopie
    # der Formel — inklusive einer eigenen, abweichenden Preisauflösung, und
    # genau daran ist sie gedriftet.
    _ea_km_pro_monat: dict[int, list[tuple[int, int, float]]] = {}
    _ea_netz_pro_monat: dict[int, list[tuple[int, int, float]]] = {}
    _ea_netz_total: dict[int, float] = {}
    _ea_fahrverbrauch: dict[int, float] = {}
    for ea in e_autos:
        params = ea.parameter or {}
        ea_vergleich_l_100km = params.get(
            PARAM_E_AUTO["VERGLEICH_VERBRAUCH_L_100KM"],
            PARAM_E_AUTO_DEFAULTS["vergleich_verbrauch_l_100km"],
        ) or PARAM_E_AUTO_DEFAULTS["vergleich_verbrauch_l_100km"]
        for (inv_id, jahr, monat), daten in historische_inv_daten.items():
            if inv_id != ea.id:
                continue
            km = daten.get("km_gefahren", 0) or 0
            # #262: SoT-Helper konsolidiert den Netz-Read mit Fallback.
            _, netz = get_emob_pv_netz_kwh(daten)
            # Phase 2a: evcc-Setup → Netz km-anteilig aus dem Wallbox-Pool.
            share = _emob_month_share(emob_ctx, "e-auto", km, jahr, monat)
            if share is not None:
                netz = share.netz_kwh
            # DI-2: CO₂-Aggregate mitziehen (gleicher Netz-/km-/Benzin-Pfad).
            co2_emob_km += km
            co2_emob_netz_kwh += netz
            co2_benzin_liter += km / 100 * ea_vergleich_l_100km
            _ea_netz_total[ea.id] = _ea_netz_total.get(ea.id, 0.0) + netz
            if netz > 0:
                _ea_netz_pro_monat.setdefault(ea.id, []).append((jahr, monat, netz))
            if km > 0:
                _ea_km_pro_monat.setdefault(ea.id, []).append((jahr, monat, km))
                _ea_fahrverbrauch[ea.id] = _ea_fahrverbrauch.get(ea.id, 0.0) + (
                    daten.get("verbrauch_kwh", 0) or 0
                )

    # #331: der fossile Anteil eines Plug-in-Hybrids — als Kosten UND als
    # geminderte CO₂-Vermeidung. Ohne gepflegtes `eigener_verbrauch_l_100km`
    # bleibt beides 0 und der Export trägt exakt dieselben Zahlen wie vorher.
    # Die Kosten-Seite kommt jetzt aus dem SoT (`fossile_kosten_euro`), die
    # CO₂-Seite bleibt hier — sie ist keine Geldgröße.
    _benzinpreis_lookup_export = {
        k: md.kraftstoffpreis_euro for k, md in md_by_periode.items()
    }
    co2_fossil_liter = 0.0
    emob_posten: list[ErsparnisPosten] = []
    for ea in e_autos:
        km_pro_monat = _ea_km_pro_monat.get(ea.id, [])
        if not km_pro_monat:
            continue
        _erg = berechne_eauto_ersparnis_periode(
            km_pro_monat=km_pro_monat,
            ladung_netz_kwh_gesamt=_ea_netz_total.get(ea.id, 0.0),
            # ⚠ Der Anlagen-Sensor kannte externe Ladekosten noch nie — hier
            # bewusst 0.0, damit das Umhängen die Preisachse ändert und sonst
            # nichts. Die Lücke gegenüber dem Cockpit ist notiert, nicht
            # nebenbei gefüllt.
            ladung_extern_euro_gesamt=0.0,
            wallbox_strompreis_cent=wallbox_netzbezug_preis_cent,
            eauto_parameter=ea.parameter,
            monats_benzinpreis_lookup=_benzinpreis_lookup_export,
            fahrverbrauch_kwh_gesamt=_ea_fahrverbrauch.get(ea.id) or None,
            monats_strompreis_lookup=wallbox_preis_by_periode,
            netz_pro_monat=_ea_netz_pro_monat.get(ea.id) or None,
        )
        bisherige_eauto_ersparnis += _erg.ersparnis_euro
        # F-20: die Monatszahl DIESES Fahrzeugs — `km_pro_monat` ist genau die
        # Liste der Monate, aus denen `ersparnis_euro` stammt.
        emob_posten.append(ErsparnisPosten(
            bezeichnung=f"E-Auto {ea.bezeichnung}",
            summe_euro=_erg.ersparnis_euro,
            monate=len({(j, m) for (j, m, _km) in km_pro_monat}),
        ))
        eigener_l = eigener_verbrauch_l_100km(ea.parameter)
        if eigener_l is not None and _erg.km_verbrenner > 0:
            co2_fossil_liter += _erg.km_verbrenner / 100 * eigener_l

    # DI-2: WP-CO₂-Aggregate (gemessene Wärme/Strom) über den kanonischen
    # Zeilen-Helper `imd_typ_beitrag` — dieselbe Wärme-/Strom-Auflösung wie das
    # Cockpit (waerme_kwh-Vorrang, sonst Heizung+Warmwasser; WP-Split-Strom).
    wp_ids = {w.id for w in waermepumpen}
    co2_wp_waerme_kwh = 0.0
    co2_wp_strom_kwh = 0.0
    co2_wp_kuehlen_kwh = 0.0
    for (inv_id, _j, _m), daten in historische_inv_daten.items():
        if inv_id in wp_ids:
            _b = imd_typ_beitrag(inv_by_id_export[inv_id], daten)
            co2_wp_waerme_kwh += _b.wp_waerme
            co2_wp_strom_kwh += _b.wp_strom
            co2_wp_kuehlen_kwh += _b.wp_modus_strom_kuehlen

    # DI-2: Gesamt-CO₂-Bilanz (PV-Eigenverbrauch + WP + E-Mob) über den
    # kanonischen Helper — deckungsgleich mit der Cockpit-Kachel `co2_gesamt_kg`.
    co2_ersparnis = berechne_co2_bilanz(
        eigenverbrauch_kwh=eigenverbrauch,
        wp_waerme_kwh=co2_wp_waerme_kwh,
        wp_strom_kwh=co2_wp_strom_kwh,
        # #263 K-2 (E-B): Kühlen ersetzt keine Heizung.
        wp_strom_kuehlen_kwh=co2_wp_kuehlen_kwh,
        emob_km=co2_emob_km,
        emob_netz_ladung_kwh=co2_emob_netz_kwh,
        benzin_verbrauch_liter=co2_benzin_liter,
        fossil_getankt_liter=co2_fossil_liter,
    ).co2_gesamt_kg

    # BKW: KEIN eigener Posten mehr (ADR-002/P9). Die Ersparnis steckt seit
    # 2026-07-31 in `netto_ertrag` — entweder über die gemeinsame PV-Basis der
    # Finanz-Zeilen (Erzeugung erfasst) oder über deren Rest-Eigenverbrauchs-
    # Term (nicht erfasst), beides mit dem Preis DES MONATS. Der frühere
    # Zuschlag hier rechnete mit einem statischen Netzbezugspreis und tauchte
    # im Sensor `netto_ertrag_euro` gar nicht auf, nur in ROI/Amortisation.
    historischer_netto_ertrag = (
        netto_ertrag
        + bisherige_wp_ersparnis
        + bisherige_eauto_ersparnis
    )

    # F-19: für die KAPITALRECHNUNG ohne die sonstigen AUSGABEN — sie stehen ab
    # hier im Nenner. Ohne diesen Abzug stünde dieselbe Reparatur zweimal in
    # derselben Formel.
    #
    # §8/3 (2026-08-10): und **ohne die sonstigen Erträge**. Eine Position im
    # Monatsabschluss ist per Form einmal geflossen (§2/2) und wird deshalb
    # nicht in die Zukunft verlängert — sonst hätte eine einmalige Förderung
    # jedes künftige Jahr erhöht, spiegelbildlich zu F-19. Wer einen
    # *wiederkehrenden* Ertrag meint, pflegt ihn seit §8/1 als „Ertrag/Jahr" an
    # der Investition; der steht als eigener Summand in `jahres_ertraege_ges`.
    #
    # Der Sensor `netto_ertrag_euro` behält beide Seiten: er ist die
    # Zeitraum-Bilanz („was hat der Zeitraum eingebracht?"), und dort ist eine
    # Reparatur ein Aufwand und eine Förderung ein Ertrag des Zeitraums.
    # Trennlinie: `core/berechnungen/kapitalrechnung.py`.
    bilanz_ohne_sonstige = (
        netto_ertrag + sonstige_ausgaben_gesamt - sonstige_ertraege_gesamt
    )

    # Jahresersparnis — **jeder Posten mit seiner eigenen Monatszahl** (F-20).
    # `netto_ertrag` ist die Anlagenbilanz und gehört zu allen erfassten
    # Monaten; WP und E-Autos bringen ihre eigene Laufzeit mit. Vorher lief
    # alles durch `len(monatsdaten)`, und jede nachgerüstete Komponente wurde
    # dadurch verdünnt — bei vollem Kostenanteil im Nenner.
    anzahl_monate = len(monatsdaten)
    ersparnis_posten = [
        ErsparnisPosten("Anlagenbilanz", bilanz_ohne_sonstige, anzahl_monate),
        *wp_posten,
        *emob_posten,
    ]
    jahres_ersparnis = jahres_ersparnis_euro(
        ersparnis_posten,
        betriebskosten_jahr_euro=betriebskosten_ges,
        jahres_ertraege_euro=jahres_ertraege_ges,
    )

    # ROI und Amortisation. Nenner ist der Kapitaleinsatz (F-19): relevante
    # Kosten + die kumulierten sonstigen AUSGABEN, die bis 2026-08-09 im
    # Zähler standen und dort annualisiert wurden — **minus die kumulierten
    # sonstigen ERTRÄGE** (Bauschritt 7, 2026-08-10). Beide Seiten des
    # Monatsabschlusses stehen damit im Nenner; im Zähler
    # (`bilanz_ohne_sonstige`) sind sie seit §8/3 ohnehin nicht mehr.
    kapitaleinsatz = kapitaleinsatz_euro(
        relevante_kosten_euro=relevante_kosten,
        sonstige_ausgaben_euro=sonstige_ausgaben_gesamt,
        sonstige_ertraege_euro=sonstige_ertraege_gesamt,
    )
    roi_prozent = None
    amortisation_jahre = None
    if kapitaleinsatz > 0 and jahres_ersparnis > 0:
        roi_prozent = (jahres_ersparnis / kapitaleinsatz) * 100
        amortisation_jahre = kapitaleinsatz / jahres_ersparnis

    # Speicher-KPIs berechnen
    speicher_effizienz = None
    speicher_zyklen = None

    # Speicher-Kapazität aus Investitionen ermitteln
    speicher_kapazitaet = 0
    for inv in investitionen:
        if inv.typ == 'speicher' and inv.parameter:
            # Zyklen-Basis ist die BRUTTO-Kapazität — dieselbe Konvention wie in
            # Monatsbericht, Speicher-Dashboard und Jahresbericht
            # (docs/BERECHNUNGEN.md §3.3). Der Kommentar behauptete hier
            # früher, `nutzbare_kapazitaet_kwh` sei ein Override; der Code liest
            # aber bewusst zuerst Brutto. Ein Dreher hätte den HA-Sensor gegen
            # den Monatsbericht laufen lassen (R22-4). `nutzbare_kapazitaet_kwh`
            # ist nur der Fallback, wenn Brutto nicht gepflegt ist; ist beides
            # leer → kein Speicher gepflegt.
            #
            # A31-2: die Lese-REIHENFOLGE bleibt genau so — dies ist der
            # Vollzyklen-Nenner, und der ist brutto (Kanon
            # `core/berechnungen/speicher.py::vollzyklen`, Entscheidung Gernot
            # 2026-07-28). Der Netto-Umstieg von A31-2 gilt für Simulation und
            # Wirtschaftlichkeits-Prognose, NICHT hier. Migriert ist nur der
            # Zugriffsweg: statt zweier Roh-Lesungen die beiden SoT-Helper —
            # `get_speicher_nutzbare_kapazitaet_kwh` greift erst, wenn brutto
            # `None` ist, und liefert dann den Netto-Wert (sein eigener
            # Brutto-Fallback läuft in diesem Fall ins Leere). Identisches
            # Verhalten, nur ohne Literal-Zugriff.
            kap = get_speicher_kapazitaet_kwh(inv) or get_speicher_nutzbare_kapazitaet_kwh(inv)
            if kap:
                speicher_kapazitaet += float(kap)

    # η über den Layer-SoT (N-252) — dieselbe Regel wie Cockpit, Komponenten
    # und Speicher-Dashboard. Der Zeitraum ist hier ein ganzes Jahr, deshalb
    # `fenster_lang`; die Obergrenze gilt trotzdem.
    #
    # ⚠ Bewusste Folge für Home Assistant (Entscheid Gernot 17.08.2026): Wo der
    # Quotient über 100 % liegt, liefert eedc jetzt GAR KEINEN Sensorwert mehr
    # statt einer unmöglichen Zahl (`value` bleibt ungesetzt ⇒ der Sensor
    # meldet `unknown`). Das reißt genau an den Tagen eine Lücke in die
    # HA-Langzeitstatistik, an denen dort bisher ein falscher Wert stand —
    # dieselbe Klasse wie der einmalige LTS-Sprung des CO₂-Sensors unter DI-2.
    _eta_ha = speicher_wirkungsgrad(
        batterie_ladung, batterie_entladung, None, langes_fenster_quelle="fenster_lang"
    )
    speicher_effizienz = _eta_ha.prozent
    # Layer-SoT statt eigener Division — dieselbe Zahl wie Hub/Monat/PDF.
    speicher_zyklen = berechne_vollzyklen(batterie_entladung, speicher_kapazitaet)

    # Sensor-Werte erstellen
    sensor_values = []

    # Energie-Sensoren
    for sensor in ANLAGE_SENSOREN:
        value = None
        berechnung = None

        if sensor.key == "pv_erzeugung_gesamt_kwh":
            value = pv_erzeugung
            berechnung = f"Summe aus {len(monatsdaten)} Monaten"
        elif sensor.key == "direktverbrauch_gesamt_kwh":
            value = direktverbrauch
            berechnung = f"PV direkt verbraucht (ohne Speicher)"
        elif sensor.key == "eigenverbrauch_gesamt_kwh":
            value = eigenverbrauch
        elif sensor.key == "einspeisung_gesamt_kwh":
            value = einspeisung
        elif sensor.key == "netzbezug_gesamt_kwh":
            value = netzbezug
        elif sensor.key == "gesamtverbrauch_kwh":
            value = gesamtverbrauch
            berechnung = f"{eigenverbrauch:.0f} + {netzbezug:.0f}"
        elif sensor.key == "autarkie_prozent":
            value = autarkie
            berechnung = f"{eigenverbrauch:.0f} ÷ {gesamtverbrauch:.0f} × 100"
        elif sensor.key == "eigenverbrauch_quote_prozent":
            value = ev_quote
            # DI-2-B: Nenner = Netzpunkt-Erzeugung (PV inkl. BKW + sonstige
            # Erzeuger), deckungsgleich mit der Cockpit-EV-Quote.
            berechnung = f"{eigenverbrauch:.0f} ÷ {erzeugung_bilanz:.0f} × 100"
        elif sensor.key == "spezifischer_ertrag_kwh_kwp":
            value = spez_ertrag if spez_ertrag else None
            if value is not None:
                berechnung = (
                    f"{pv_erzeugung:.0f} kWh annualisiert "
                    f"(saisonal gewichtet, wie Cockpit)"
                )
        elif sensor.key == "netto_ertrag_euro":
            value = netto_ertrag
            berechnung = f"{einspeise_erloes:.2f} + {ev_ersparnis:.2f} + {sonstige_netto_gesamt:.2f} (sonstige)"
        elif sensor.key == "einspeise_erloes_euro":
            value = einspeise_erloes
            if strompreis and getattr(strompreis, "einspeisung_variabel", False):
                # #392: der €-Wert daneben ist je Monat mit dem gepflegten
                # Monatssatz gerechnet — ein einzelner Satz im Text wäre
                # eine Behauptung, die die Zahl nicht deckt.
                berechnung = (
                    f"{einspeisung:.0f} kWh × Monatssatz "
                    f"(variable Vergütung, je Monat aufgelöst)"
                )
            elif strompreis:
                berechnung = f"{einspeisung:.0f} × {strompreis.einspeiseverguetung_cent_kwh:.2f} ct/kWh"
        elif sensor.key == "eigenverbrauch_ersparnis_euro":
            value = ev_ersparnis
            if strompreis:
                berechnung = f"{eigenverbrauch:.0f} × {strompreis.netzbezug_arbeitspreis_cent_kwh:.2f} ct/kWh"
        elif sensor.key == "co2_ersparnis_kg":
            value = co2_ersparnis
            berechnung = "PV-Eigenverbrauch + Wärmepumpe + E-Mobilität (vermiedenes CO₂)"
        elif sensor.key == "eedc_grundlast_kw":
            value, berechnung = await grundlast_sensorwert(db, anlage.id, date.today())

        if value is not None:
            sensor_values.append(SensorValue(
                definition=sensor,
                value=value,
                berechnung=berechnung
            ))

    # Investitions-Sensoren
    for sensor in INVESTITION_SENSOREN:
        value = None
        berechnung = None

        if sensor.key == "investition_gesamt_euro":
            if investition_gesamt > 0:
                value = investition_gesamt
                berechnung = f"Summe aus {len(investitionen)} Investitionen"
        elif sensor.key == "jahres_ersparnis_euro":
            if jahres_ersparnis > 0:
                value = jahres_ersparnis
                # N-212: der Rechenweg nennt jetzt jeden Posten mit SEINER
                # Monatszahl **und** den Betriebskosten-Abzug. Vorher stand
                # hier `(Σ ÷ Anlagenmonate) × 12` — das ergab einen um exakt
                # die Betriebskosten größeren Wert als der Sensor daneben.
                berechnung = erklaerung_jahres_ersparnis(
                    ersparnis_posten,
                    betriebskosten_jahr_euro=betriebskosten_ges,
                    jahres_ertraege_euro=jahres_ertraege_ges,
                )
        elif sensor.key == "roi_prozent":
            if roi_prozent is not None:
                value = roi_prozent
                berechnung = f"{jahres_ersparnis:.2f} ÷ {kapitaleinsatz:.2f} × 100"
        elif sensor.key == "amortisation_jahre":
            if amortisation_jahre is not None:
                value = amortisation_jahre
                # F-19: der Nenner ist der Kapitaleinsatz. Er wird ausgeschrieben,
                # solange sonstige Positionen ihn von den relevanten Kosten
                # unterscheiden — sonst bliebe die Differenz unerklärt (N-212).
                if sonstige_ausgaben_gesamt or sonstige_ertraege_gesamt:
                    _nenner = f"{relevante_kosten:.2f}"
                    if sonstige_ausgaben_gesamt:
                        _nenner += f" + {sonstige_ausgaben_gesamt:.2f} sonstige Ausgaben"
                    # Bauschritt 7: die Erträge mindern den Nenner und werden
                    # deshalb genauso ausgeschrieben — sonst bliebe die
                    # Differenz zum Ergebnis unerklärt (N-212).
                    if sonstige_ertraege_gesamt:
                        _nenner += f" − {sonstige_ertraege_gesamt:.2f} sonstige Erträge"
                    berechnung = f"({_nenner}) ÷ {jahres_ersparnis:.2f}"
                else:
                    berechnung = f"{kapitaleinsatz:.2f} ÷ {jahres_ersparnis:.2f}"
                # Konzept §5/§8-6: die Annahme steht im selben Attribut wie der
                # Rechenweg. Ein HA-Sensor hat keinen Tooltip — wer die Zahl in
                # ein Dashboard hängt, sieht sonst eine Dauer ohne jede
                # Voraussetzung.
                berechnung += f" — {annahme_dauer_text(betriebskosten_jahr_euro=betriebskosten_ges)}"

        if value is not None:
            sensor_values.append(SensorValue(
                definition=sensor,
                value=value,
                berechnung=berechnung
            ))

    # Speicher-Sensoren (nur wenn Speicher vorhanden)
    if speicher_kapazitaet > 0 or batterie_ladung > 0:
        for sensor in SPEICHER_SENSOREN:
            value = None
            berechnung = None

            if sensor.key == "speicher_zyklen":
                if speicher_zyklen is not None:
                    value = speicher_zyklen
                    berechnung = f"{batterie_entladung:.0f} ÷ {speicher_kapazitaet:.1f}"
            elif sensor.key == "speicher_effizienz_prozent":
                if speicher_effizienz is not None:
                    value = speicher_effizienz
                    berechnung = f"{batterie_entladung:.0f} ÷ {batterie_ladung:.0f} × 100"

            if value is not None:
                sensor_values.append(SensorValue(
                    definition=sensor,
                    value=value,
                    berechnung=berechnung
                ))

    # Letzter Import Sensoren (Status)
    if monatsdaten:
        # Finde den neuesten Monat (sortiert nach Jahr, dann Monat)
        sorted_md = sorted(monatsdaten, key=lambda m: (m.jahr, m.monat), reverse=True)
        letzter = sorted_md[0]

        # Monatsnamen
        monatsnamen = [
            "", "Januar", "Februar", "März", "April", "Mai", "Juni",
            "Juli", "August", "September", "Oktober", "November", "Dezember"
        ]
        monatsname = monatsnamen[letzter.monat] if 1 <= letzter.monat <= 12 else str(letzter.monat)

        for sensor in LETZTER_IMPORT_SENSOREN:
            value = None
            berechnung = None

            if sensor.key == "letzter_import_jahr":
                value = letzter.jahr
                berechnung = f"Neuester Datensatz: {monatsname} {letzter.jahr}"
            elif sensor.key == "letzter_import_monat":
                value = letzter.monat
                berechnung = f"Monat {letzter.monat} ({monatsname})"
            elif sensor.key == "letzter_import_monat_name":
                value = f"{monatsname} {letzter.jahr}"
                berechnung = f"Formatiert aus {letzter.monat}/{letzter.jahr}"
            elif sensor.key == "anzahl_monate_erfasst":
                value = len(monatsdaten)
                berechnung = f"Erfasste Monatsdaten in der Datenbank"

            if value is not None:
                sensor_values.append(SensorValue(
                    definition=sensor,
                    value=value,
                    berechnung=berechnung
                ))

    # #150 A: eedc-eigene PV-Prognose (OpenMeteo × Lernfaktor) — anlage-weit,
    # koordinaten-/PV-gated, netzwerk-tolerant (None → Sensoren entfallen).
    # Stundenprofil reist als Attribut mit (kein eigenes Topic).
    prognose = await berechne_prognose_export(db, anlage)
    if prognose:
        for sensor in PROGNOSE_SENSOREN:
            value = None
            zusatz: dict = {}
            if sensor.key == "eedc_prognose_heute_kwh":
                value = prognose["heute_kwh"]
                if prognose.get("stundenprofil_heute"):
                    zusatz = {"stundenprofil_kwh": prognose["stundenprofil_heute"]}
            elif sensor.key == "eedc_prognose_heute_rollend_kwh":
                value = prognose["heute_rollend_kwh"]
            elif sensor.key == "eedc_prognose_rest_today_kwh":
                value = prognose["rest_today_kwh"]
            elif sensor.key == "eedc_prognose_day_plus_1_kwh":
                value = prognose["day_plus_1_kwh"]
                if prognose.get("stundenprofil_day_plus_1"):
                    zusatz = {"stundenprofil_kwh": prognose["stundenprofil_day_plus_1"]}
            elif sensor.key == "eedc_prognose_day_plus_2_kwh":
                value = prognose["day_plus_2_kwh"]
                if prognose.get("stundenprofil_day_plus_2"):
                    zusatz = {"stundenprofil_kwh": prognose["stundenprofil_day_plus_2"]}
            elif sensor.key == "eedc_prognose_day_plus_3_kwh":
                value = prognose["day_plus_3_kwh"]
                if prognose.get("stundenprofil_day_plus_3"):
                    zusatz = {"stundenprofil_kwh": prognose["stundenprofil_day_plus_3"]}
            elif sensor.key == "eedc_prognose_heute_vormittag_kwh":
                value = prognose.get("heute_vormittag_kwh")
                if prognose.get("solar_noon_heute"):
                    zusatz = {"solar_noon": prognose["solar_noon_heute"]}
            elif sensor.key == "eedc_prognose_heute_nachmittag_kwh":
                value = prognose.get("heute_nachmittag_kwh")
                if prognose.get("solar_noon_heute"):
                    zusatz = {"solar_noon": prognose["solar_noon_heute"]}
            elif sensor.key == "eedc_prognose_morgen_vormittag_kwh":
                value = prognose.get("morgen_vormittag_kwh")
                if prognose.get("solar_noon_morgen"):
                    zusatz = {"solar_noon": prognose["solar_noon_morgen"]}
            elif sensor.key == "eedc_prognose_morgen_nachmittag_kwh":
                value = prognose.get("morgen_nachmittag_kwh")
                if prognose.get("solar_noon_morgen"):
                    zusatz = {"solar_noon": prognose["solar_noon_morgen"]}
            elif sensor.key == "eedc_speicher_voll_um":
                value = prognose["speicher_voll_um"]
                # Die Verbrauchsannahme der Simulation reist mit (N-392) — ein
                # ANDERES Modell als beim Nachbarn darunter (8-Wochen-Profil
                # statt 7-Tage-Profil); das Attribut `profil_typ` sagt es.
                if value is not None and prognose.get("speicher_verbrauch_profil"):
                    zusatz = dict(prognose["speicher_verbrauch_profil"])
            elif sensor.key == "eedc_verbrauchsprognose_heute_kwh":
                value = prognose.get("verbrauch_heute_kwh")
                if value is not None:
                    # Die Grundlage reist mit — dieselben drei Angaben, die der
                    # Tooltip der Kachel nennt (Profiltyp, Tage, belegte Slots).
                    zusatz = {
                        "profil_typ": prognose.get("verbrauch_profil_typ"),
                        "profil_tage": prognose.get("verbrauch_profil_tage"),
                        "profil_slots": prognose.get("verbrauch_profil_slots"),
                    }

            if value is not None:
                sensor_values.append(SensorValue(
                    definition=sensor, value=value, zusatz_attribute=zusatz
                ))

    # #150 B: Börsenpreis-Trigger (Rang je Tag-/Nacht-Fenster) — Rang-Profil als Attribut.
    preis = await berechne_preis_export(db, anlage)
    if preis:
        for sensor in PREIS_SENSOREN:
            value = None
            zusatz = {}
            if sensor.key == "eedc_preis_rang":
                value = preis["preis_rang"]
                if preis.get("rang_profil"):
                    zusatz = {"rang_profil": preis["rang_profil"]}
                if preis.get("guenstig_schwelle_cent") is not None:
                    zusatz["guenstig_schwelle_cent"] = preis["guenstig_schwelle_cent"]
                # Die Bezugsgröße der Schwelle reist mit: ohne sie ist im
                # Rang-Profil keine eigene Schwelle rechenbar (#335/N-105).
                if preis.get("optimierter_durchschnitt_cent") is not None:
                    zusatz["optimierter_durchschnitt_cent"] = preis["optimierter_durchschnitt_cent"]
                # Der Kalendertag des Profils (N-104). Ohne ihn ist ein
                # stehengebliebenes Profil nach Mitternacht nicht von einem
                # aktuellen zu unterscheiden.
                if preis.get("datum"):
                    zusatz["datum"] = preis["datum"]
                # Morgen — sobald die Day-Ahead-Auktion veröffentlicht hat
                # (N-104, Melder rapahl). `morgen_verfuegbar` ist immer gesetzt,
                # damit eine Automation „noch nicht da" von „gibt es nicht"
                # unterscheiden kann, statt auf ein fehlendes Attribut zu prüfen.
                zusatz["morgen_verfuegbar"] = bool(preis.get("morgen_verfuegbar"))
                for schluessel in (
                    "datum_morgen",
                    "rang_profil_morgen",
                    "guenstig_schwelle_cent_morgen",
                    "optimierter_durchschnitt_cent_morgen",
                ):
                    if preis.get(schluessel) is not None:
                        zusatz[schluessel] = preis[schluessel]
            elif sensor.key == "eedc_preis_guenstige_stunden_anzahl":
                value = preis["guenstige_stunden_anzahl"]
            elif sensor.key == "eedc_preis_guenstige_stunden_tag":
                value = preis["guenstige_stunden_tag"]
            elif sensor.key == "eedc_preis_guenstige_stunden_nacht":
                value = preis["guenstige_stunden_nacht"]
            elif sensor.key == "eedc_preis_aktuell_cent":
                value = preis["preis_aktuell_cent"]
            elif sensor.key == "eedc_preis_tages_durchschnitt_cent":
                value = preis["tages_durchschnitt_cent"]
            elif sensor.key == "eedc_preis_optimierter_durchschnitt_cent":
                value = preis["optimierter_durchschnitt_cent"]
            elif sensor.key == "eedc_preis_abstand_prozent":
                value = preis["abstand_prozent"]
            elif sensor.key == "eedc_preis_abstand_cent":
                value = preis["abstand_cent"]

            if value is not None:
                sensor_values.append(SensorValue(
                    definition=sensor, value=value, zusatz_attribute=zusatz
                ))

    return sensor_values
