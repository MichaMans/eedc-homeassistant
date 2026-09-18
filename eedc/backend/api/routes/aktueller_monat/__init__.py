"""
Aktueller Monat API Route — Paket-Fassade.

Kombiniert Daten aus HA-Sensoren, HA-Statistics, Connectors und gespeicherten
Monatsdaten zu einer Echtzeit-Übersicht des laufenden Monats.

Seit 18.09.2026 ein Paket (Vorlage 1 des Refactorings grosser Dateien, reiner Umzug):
- ``schemas.py``   — Antwortmodelle und Konstanten
- ``vergleich.py`` — Vorjahr, PVGIS-SOLL, Nachtsockel, Tarifaufloesung
- ``tkonto.py``    — die T-Konto-Zeile je Investition
- hier            — Router, die fuenf Quellen-Sammler und der Endpunkt

⚠ Endpunkt und Sammler bleiben BEWUSST in dieser Datei: 57 Testpatches setzen Attribute auf
diesem Modulobjekt (``monkeypatch.setattr(am, "datetime", …)`` und die drei Sammler). Laege der
Endpunkt in einem Untermodul, traefen die Patches ein Modul, das die Uhr nicht mehr liest —
die Tests blieben gruen und pruefen nichts. Alle Namen, die Tests und Aufrufer bisher aus dem
Modul importierten, werden hier weiter exportiert (``__all__``).
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from backend.core.exceptions import not_found
from backend.api.deps import get_db
from backend.services.waermepumpe_kennzahlen_je_geraet import lade_kennzahlen_je_geraet
from backend.services.waerme_klima_block import (
    achsen_der_anlage,
    funktions_eingaenge_der_anlage,
    geraete_zeilen,
    schranken_eingang,
    traegt_menge,
    was_noch_moeglich,
)
from backend.models.anlage import Anlage
from backend.models.investition import Investition, InvestitionMonatsdaten
from backend.models.monatsdaten import Monatsdaten
from backend.core.berechnungen.zeittarif import hat_zeitfenster
from backend.services.strompreis_aggregator import aufgeloester_monatspreis
from backend.api.routes.strompreise import lade_tarife_fuer_anlage, resolve_einspeise_preis_cent
from backend.api.routes.connector import _calc_month_delta
from backend.core.berechnungen.anlagen_kwp import anlagen_kwp
from backend.core.berechnungen.waermepumpe_kennzahl import (
    ARBEITSZAHL_FUNKTIONEN,
    abgrenzung_je_funktion,
    als_arbeitszahl,
    hub_hilft,
    abgrenzungs_grund,
    arbeitszahl_je_funktion,
    arbeitszahl_kuehlen,
    heizwaerme_kwh,
    systemarbeitszahl,
    waerme_gesamt_kwh,
)
from backend.core.berechnungen import (
    sonstiges_richtung,
    auslastung_prozent,
    auslastungs_basis_kwh,
    autarkie_prozent,
    berechne_grundlast,
    monatsfenster,
    berechne_netzbezug_kosten,
    berechne_netzladung_kosten,
    eauto_effizienz_100km,
    eigenverbrauchsquote_prozent,
    einspeise_erloes_euro,
    erzeugung_hinter_zaehler_kwh,
    merge_datenquellen,
    spezifischer_ertrag_kwh_kwp,
    speicher_wirkungsgrad as berechne_speicher_wirkungsgrad,
    teilzeitraum_felder,
    vollzyklen as berechne_vollzyklen,
)
from backend.core.monatswert_grund import monatswert_grund, monatswert_grund_text
from backend.services.einspeise_erloes_service import get_neg_preis_einspeisung_monat
from backend.services.wp_wirtschaftlichkeit import berechne_wp_ersparnis, wp_ersparnis_berechnung
from backend.services.eauto_wirtschaftlichkeit import compute_emob_pool_attribution
from backend.services.emob_ladeanteil import reichere_monatszeilen_an
from backend.services.monats_fakten import (
    MonatsFakt,
    SonstigesFakten,
    lade_monats_fakten,
    pv_unvollstaendig_hinweis,
)
from backend.core.wirtschaftlichkeit_defaults import (
    EINSPEISEVERGUETUNG_DEFAULT_CENT,
    NETZBEZUG_DEFAULT_CENT,
)
from backend.core.betriebsmodus import (
    BETRIEBSART_NUTZENERGIE_FELD,
    BETRIEBSART_STROM_FELD,
    HEIZEN as BM_HEIZEN,
    MESSBARE_MODI,
)
from backend.core.field_definitions import (
    FEINE_STROM_FELDER,
    basis_feld_key,
    get_wp_strom_kwh,
    wp_strom_aufteilung,
)
from backend.core.investition_kennwerte import get_speicher_kapazitaet_kwh
from backend.core.investition_parameter import ist_dienstlich
from backend.api.routes.aktueller_monat.schemas import (  # noqa: F401 — Re-Export fuer Tests und Aufrufer
    AktuellerMonatResponse,
    DatenquelleInfo,
    ERLOES_LABEL_EINSPEISUNG,
    InvestitionFinancialDetail,
    MONAT_NAMEN,
    SollPv,
    SonstigesGeraet,
)
from backend.api.routes.aktueller_monat.tkonto import _baue_investition_financial  # noqa: F401
from backend.api.routes.aktueller_monat.vergleich import (  # noqa: F401
    _load_grundlast_nacht_kw,
    _load_soll_pv,
    _load_vorjahr,
    _zeittarif_preis,
)

logger = logging.getLogger(__name__)

__all__ = [
    'AktuellerMonatResponse',
    'DatenquelleInfo',
    'ERLOES_LABEL_EINSPEISUNG',
    'InvestitionFinancialDetail',
    'MONAT_NAMEN',
    'SollPv',
    'SonstigesGeraet',
    '_baue_investition_financial',
    '_collect_connector_data',
    '_collect_ha_statistics_data',
    '_collect_mqtt_inbound_data',
    '_collect_saved_data',
    '_collect_tagesebene_data',
    '_load_grundlast_nacht_kw',
    '_load_soll_pv',
    '_load_vorjahr',
    '_zeittarif_preis',
    'datetime',
    'get_aktueller_monat',
    'router',
]

router = APIRouter()

from backend.api.routes.aktueller_monat.aggregation import _WP_WAERME_D1_SUFFIX  # noqa: F401 — Konstante zog nach aggregation.py (Vorlage 2)

from backend.api.routes.aktueller_monat.aggregation import _WP_STROM_K3_SUFFIX  # noqa: F401 — Konstante zog nach aggregation.py (Vorlage 2)
from backend.api.routes.aktueller_monat.aggregation import (  # Vorlage 2
    _WP_STROM_K3_SUFFIX,
    _WP_WAERME_D1_SUFFIX,
    aggregiere_typen,
    berechne_bilanzwerte,
    emob_max_pool,
    extrahiere_werte,
)
from backend.api.routes.aktueller_monat.finanzen import (  # Vorlage 2
    betriebskosten_und_sonstige_positionen,
    emob_aggregat_und_kennzahlen,
    finanzen_des_monats,
    komponenten_ersparnis,
    t_konto_je_investition,
)

# =============================================================================
# Datensammlung
# =============================================================================

async def _collect_ha_statistics_data(anlage: Anlage, jahr: int, monat: int) -> dict[str, tuple[float, DatenquelleInfo]]:
    """Sammelt Daten aus der HA Recorder-Statistik-DB (Konfidenz 92%).

    Liest MAX(state) - MIN(state) pro Sensor aus der HA statistics-Tabelle.
    Funktioniert für total_increasing UND measurement Sensoren (Fallback).
    """
    # N-156/F-26: das frühere Gate auf `HA_INTEGRATION_AVAILABLE`
    # (= SUPERVISOR_TOKEN) stand unmittelbar vor der Frage, die es beantworten
    # sollte — `ha_stats.is_available` prüft die Erreichbarkeit selbst, und zwar
    # per Recorder-DB **oder** WebSocket. Im Docker-Betrieb mit Long-Lived-Token
    # sperrte es damit die Langzeitstatistik aus, obwohl sie erreichbar war.
    #
    # ⚠ Die Erreichbarkeitsfrage steht bewusst **hinter** der Sensor-Liste:
    # `is_available` baut im Zweifel eine Verbindung auf und zahlt bei nicht
    # erreichbarer HA einen vollen Timeout. Eine Anlage ohne einen einzigen
    # Sensor-Feld-Eintrag hat hier nichts zu holen — die darf das nicht kosten.
    mapping = anlage.sensor_mapping or {}
    basis = mapping.get("basis", {})
    inv_mapping = mapping.get("investitionen", {})

    # Sensor-IDs sammeln und Rückmapping erstellen: sensor_id → feld_name
    sensor_to_feld: dict[str, str] = {}

    basis_feld_map = {
        "einspeisung": "einspeisung_kwh",
        "netzbezug": "netzbezug_kwh",
        "pv_gesamt": "pv_erzeugung_kwh",
    }
    for mapping_key, feld_name in basis_feld_map.items():
        feld_mapping = basis.get(mapping_key)
        if feld_mapping and feld_mapping.get("strategie") == "sensor" and feld_mapping.get("sensor_id"):
            sensor_to_feld[feld_mapping["sensor_id"]] = feld_name

    for inv_id_str, inv_data in inv_mapping.items():
        felder = inv_data.get("felder", {})
        for feld_key, feld_config in felder.items():
            if feld_config and feld_config.get("strategie") == "sensor" and feld_config.get("sensor_id"):
                sensor_to_feld[feld_config["sensor_id"]] = f"inv_{inv_id_str}_{feld_key}"

    if not sensor_to_feld:
        return {}

    from backend.services.ha_statistics_service import get_ha_statistics_service
    ha_stats = get_ha_statistics_service()
    if not ha_stats.is_available:
        return {}

    # Synchronen SQLite-Zugriff in Thread auslagern
    try:
        sensor_ids = list(sensor_to_feld.keys())
        result = await asyncio.to_thread(ha_stats.get_monatswerte, sensor_ids, jahr, monat)
    except Exception:
        logger.warning("HA Statistics DB nicht erreichbar")
        return {}

    resolved: dict[str, tuple[float, DatenquelleInfo]] = {}
    now_str = datetime.now().isoformat()
    quelle = DatenquelleInfo(quelle="ha_statistics", konfidenz=92, zeitpunkt=now_str)

    for sensor_wert in result.sensoren:
        feld_name = sensor_to_feld.get(sensor_wert.sensor_id)
        if feld_name and sensor_wert.differenz is not None and sensor_wert.differenz > 0:
            resolved[feld_name] = (sensor_wert.differenz, quelle)

    return resolved


async def _collect_connector_data(anlage: Anlage, jahr: int, monat: int) -> dict[str, tuple[float, DatenquelleInfo]]:
    """Sammelt Daten aus Connector-Snapshots (Konfidenz 90%)."""
    config = anlage.connector_config
    if not config:
        return {}

    snapshots = config.get("meter_snapshots", {})
    if not snapshots:
        return {}

    delta = _calc_month_delta(snapshots, jahr, monat)
    if not delta:
        return {}

    resolved: dict[str, tuple[float, DatenquelleInfo]] = {}
    last_fetch = config.get("last_fetch")
    # Die Abdeckung wandert mit: das Delta misst `(abdeckung_von, abdeckung_bis]`,
    # nicht zwangsläufig den ganzen Monat. `merge_datenquellen` entscheidet
    # damit, ob der Connector gespeicherte Monatswerte überschreiben darf.
    quelle = DatenquelleInfo(
        quelle="local_connector",
        konfidenz=90,
        zeitpunkt=last_fetch,
        abdeckung_von=delta.abdeckung_von,
        abdeckung_bis=delta.abdeckung_bis,
    )

    feld_map = {
        "pv_erzeugung_kwh": "pv_erzeugung_kwh",
        "einspeisung_kwh": "einspeisung_kwh",
        "netzbezug_kwh": "netzbezug_kwh",
        "batterie_ladung_kwh": "speicher_ladung_kwh",
        "batterie_entladung_kwh": "speicher_entladung_kwh",
    }
    for delta_key, feld_name in feld_map.items():
        val = delta.werte.get(delta_key)
        if val is not None:
            resolved[feld_name] = (val, quelle)

    return resolved


def _collect_saved_data(
    fakt: Optional[MonatsFakt],
) -> dict[str, tuple[float, DatenquelleInfo]]:
    """Der **gespeicherte** Zweig der Quellen-Kaskade (Konfidenz 85 %).

    Faltet nichts mehr selbst: die Mengen kommen aus den Monats-Fakten
    (ADR-002/**P10**), wo Zeitfilter (``aktiv`` · Anschaffung · Stilllegung),
    Dienstwagen-Filter, P7-Auflösung der PV und der Monatstarif genau einmal
    gelten. Übrig bleibt hier die **Merge-Semantik** — und die ist bewusst
    unverändert:

    - Die ``> 0``-Gates sind keine Rechenregel, sondern die Präzedenz-Regel der
      Kaskade: ein Feld, das ``saved`` nicht setzt, darf von einer stärkeren
      Quelle (Connector · MQTT · HA-Statistics) kommen. ``is not None`` daraus
      zu machen wäre eine andere Route, nicht dieselbe mit anderer Herkunft.
    - Einspeisung/Netzbezug kommen weiter direkt von der ``Monatsdaten``-Zeile
      und **nur, wenn sie dort gesetzt sind**: ``ZaehlerFakten`` macht aus einer
      fehlenden Zahl eine 0,0, und eine 0,0 aus der schwächsten Quelle würde
      eine echte Lücke füllen, die eine stärkere Quelle noch schließen könnte.

    Zwei Zahlen ändern sich dadurch sichtbar (beide gewollt, ``C1c``):
    die PV ist **P7-aufgelöst** (das Anlagen-Aggregat füllt Modul-Lücken, statt
    dass der Monatsbericht bei reiner Aggregat-Pflege gar keine PV zeigt), und
    der BKW-Eigenverbrauch fällt ohne Messwert **nicht** mehr auf die volle
    Erzeugung zurück (der alte D5-Quirk, divergent zu Komponenten/Übersicht).
    """
    resolved: dict[str, tuple[float, DatenquelleInfo]] = {}
    if fakt is None:
        return resolved

    md = fakt.meta.monatsdaten
    quelle = DatenquelleInfo(
        quelle="gespeichert", konfidenz=85,
        zeitpunkt=(
            md.updated_at.isoformat()
            if md is not None and getattr(md, "updated_at", None) else None
        ),
    )

    if md is not None:
        for attr, feld in [
            ("einspeisung_kwh", "einspeisung_kwh"),
            ("netzbezug_kwh", "netzbezug_kwh"),
        ]:
            val = getattr(md, attr, None)
            if val is not None:
                resolved[feld] = (val, quelle)

    for feld, wert in (
        # Module (P7-aufgelöst) + BKW — die PV-Achse der Anlage.
        ("pv_erzeugung_kwh", fakt.erzeugung.pv_kwh),
        ("speicher_ladung_kwh", fakt.speicher.ladung_kwh),
        ("speicher_entladung_kwh", fakt.speicher.entladung_kwh),
        # #183: bei getrennter Strommessung ist der Gesamt-Strom bereits im
        # Resolver summiert; `waerme` = waerme_kwh oder Heiz+WW.
        ("wp_strom_kwh", fakt.wp.strom_kwh),
        ("wp_waerme_kwh", fakt.wp.waerme_kwh),
        # #263 K-2 (E-B): der Kühlanteil — Teilmenge von `wp_strom_kwh`, hier
        # nur mitgeführt, damit die Ersparnis-Rechnung ihn herausnehmen kann.
        ("wp_modus_kuehlen_kwh", fakt.wp.modus_strom_kuehlen_kwh),
        # Heimladungs-Trias aus EINER Quelle (#262) — Dienstwagen sind in der
        # Schicht bereits heraus ([[feedback_dienstwagen_alle_checks]]).
        ("emob_ladung_kwh", fakt.emob.ladung_kwh),
        ("emob_km", fakt.emob.km),
        ("emob_verbrauch_kwh", fakt.emob.fahrverbrauch_kwh),
        ("emob_pv_ladung_kwh", fakt.emob.ladung_pv_kwh),
        ("emob_ladung_extern_euro", fakt.emob.extern_euro),
        ("bkw_erzeugung_kwh", fakt.bkw.erzeugung_kwh),
        ("bkw_eigenverbrauch_kwh", fakt.bkw.eigenverbrauch_gemessen_kwh),
        # Sonstiger Erzeuger (BHKW) speist hinter den Hauszähler.
        ("sonstiges_erzeugung_kwh", fakt.sonstiges.erzeugung_kwh),
        # §9.2: Abgabe an Dritte — wird in der Bilanz unten vom Eigenverbrauch abgezogen.
        ("sonstiges_abgabe_kwh", fakt.sonstiges.abgabe_kwh),
    ):
        if wert > 0:
            resolved[feld] = (wert, quelle)

    return resolved


async def _collect_mqtt_inbound_data(
    db: AsyncSession,
    anlage: Anlage,
    investitionen: list[Investition],
    jahr: int,
    monat: int,
    bis: Optional[datetime] = None,
) -> dict[str, tuple[float, DatenquelleInfo]]:
    """Sammelt Monatsmengen aus den MQTT-Zählerständen (Konfidenz 91%).

    ⛔ **Hier stand bis zum 2026-08-27: „Liest kumulierte Monatswerte aus dem
    MQTT-Cache."** Der Docstring war falsch, und weil `merge_datenquellen` das
    Ergebnis per ``resolved.update(mqtt_energy)`` anwendet, war es kein
    Anzeige-, sondern ein **Zahlenfehler**: Im laufenden Monat überschrieb der
    Lebenszählerstand den gespeicherten Monatswert. Bei einer Anlage ohne HA —
    also genau der Standalone-MQTT-Aufstellung, für die dieser Pfad gebaut ist
    — gewann er gegen alles außer den HA-Statistiken, die es dort nicht gibt.

    Der Cache trägt den zuletzt empfangenen **Stand**; so verlangt es die
    Topic-Registry (*„Kumulierter kWh-Zählerstand"*). Gemeldet von **gruaGit**
    (Discussion #396) an der Schwesterstelle im Monatsabschluss (F-66).

    Die Menge kommt jetzt aus der mitgeschriebenen Standreihe. Fehlt ein Rand
    oder sprang der Zähler zurück, fehlt das Feld im Ergebnis — dann bleibt der
    **gespeicherte** Wert stehen, statt von einem Stand verdrängt zu werden.

    ⭐ **Seit N-472 mit Rückfall auf den ersten Stand des Monats.** Fehlt der
    Stand am Monatsersten — die Lage jeder Anlage, die mitten im Monat
    eingerichtet wurde —, misst die Menge ab dem ersten mitgeschriebenen Stand,
    und die ``DatenquelleInfo`` dieses Feldes trägt dann ``abdeckung_von``/
    ``abdeckung_bis``. Der Slot ist derselbe, den der Connector seit #361 für
    genau diese Aussage benutzt; die Provenanz-Zeile beschriftet ihn bereits
    (*„MQTT (ab 14.09.)"*). Ein Feld **ohne** ``abdeckung_von`` hat den
    Monatsersten als linken Rand — daran erkennt der Aufrufer die Teilzeiträume,
    ohne dass diese Funktion eine zweite Liste zurückgeben muss.
    """
    from backend.services.mqtt_energy_history_service import mqtt_monats_mengen
    from backend.services.mqtt_inbound_service import get_mqtt_inbound_service
    from backend.services.snapshot.keys import extract_quellen_energy

    svc = get_mqtt_inbound_service()
    if not svc:
        return {}

    energy = svc.cache.get_energy_data(anlage.id)
    if not energy:
        return {}

    # Der Aufrufer ruft diesen Pfad nur für den laufenden Monat — die obere
    # Grenze ist deshalb das Jetzt und nicht das Monatsende (ein Stand in der
    # Zukunft existiert nicht).
    #
    # ⚠ `bis` ist ein PARAMETER, kein `datetime.now()` mitten im Rumpf: Eine
    # Probe, die die Prozessuhr liest, wettet auf die Stunde ihres Laufs (N-167)
    # — und die Suite fährt in drei Zeitzonen. Der Wächter
    # `test_konformitaet_echte_uhr_in_tests.py` hat genau das beim Bau dieser
    # Zeile gemeldet; die Naht ist die Antwort darauf und gehört ohnehin hierher.
    mengen = await mqtt_monats_mengen(
        db, anlage.id, jahr, monat, list(energy.keys()),
        quellen_energy=extract_quellen_energy(anlage),
        bis=bis if bis is not None else datetime.now(),
        rueckfall_erster_stand=True,
    )
    if not mengen:
        return {}

    resolved: dict[str, tuple[float, DatenquelleInfo]] = {}
    now_str = datetime.now().isoformat()
    quelle = DatenquelleInfo(quelle="mqtt_inbound", konfidenz=91, zeitpunkt=now_str)

    def _quelle(menge) -> DatenquelleInfo:
        """Die gemeinsame Quelle — oder eine eigene, wenn der Monatsanfang fehlt.

        Ein Feld ab Monatsbeginn bekommt die geteilte Instanz **ohne**
        Abdeckung (bitgleich zu vor N-472). Nur das Rückfall-Feld trägt seinen
        gemessenen Zeitraum, damit Provenanz-Zeile und Kachel-Hinweis ihn
        nennen können — und nur seinen eigenen, nicht den eines Nachbarn.
        """
        if menge.ab_fenster_beginn:
            return quelle
        return DatenquelleInfo(
            quelle="mqtt_inbound", konfidenz=91, zeitpunkt=now_str,
            abdeckung_von=menge.seit, abdeckung_bis=menge.bis,
        )

    # Basis-Felder
    basis_map = {
        "pv_gesamt_kwh": "pv_erzeugung_kwh",
        "einspeisung_kwh": "einspeisung_kwh",
        "netzbezug_kwh": "netzbezug_kwh",
    }
    for mqtt_key, feld_name in basis_map.items():
        menge = mengen.get(mqtt_key)
        if menge is not None and menge.menge_kwh > 0:
            resolved[feld_name] = (menge.menge_kwh, _quelle(menge))

    # Investitions-Felder: inv/{inv_id}/{key} → inv_{inv_id}_{key}
    # (passt zum Aggregations-Pattern in der Prioritätskette)
    inv_ids = {str(i.id) for i in investitionen}
    for mqtt_key, menge in mengen.items():
        if not mqtt_key.startswith("inv/") or menge.menge_kwh <= 0:
            continue
        parts = mqtt_key.split("/", 2)  # ["inv", "3", "ladung_kwh"]
        if len(parts) == 3 and parts[1] in inv_ids:
            resolved[f"inv_{parts[1]}_{parts[2]}"] = (menge.menge_kwh, _quelle(menge))

    return resolved


async def _collect_tagesebene_data(
    db: AsyncSession,
    anlage_id: int,
    jahr: int,
    monat: int,
    wp_mengen: Optional[dict] = None,
    wp_von: Optional[date] = None,
    wp_bis: Optional[date] = None,
) -> dict[str, tuple[float, DatenquelleInfo]]:
    """Die **fünfte** Quelle: die lokale Tagesebene (Konfidenz 80 %, N-472).

    ⛔ **Der Anlass.** Eine Anlage mit vollständig aggregierter Tagesebene sah
    in *Cockpit → Monat* leere Kacheln, solange keine der vier direkten Quellen
    antwortete: einen automatischen Monatsabschluss gibt es nicht, der laufende
    Monat hat also nie eine ``Monatsdaten``-Zeile, und wer weder HA-Statistik
    noch Connector noch MQTT-Zählerreihe hat, bekam gar nichts. **Der Verlauf
    daneben zeigte dieselben Tage vollständig** (gemessen an der
    Prüfstand-Anlage der Demo-DB r28: 13 September-Tage, PV 265,3 kWh,
    Einspeisung 191,9, Netzbezug 101,3 — und drei leere Kacheln darüber).

    Die Quelle ist dieselbe, die N-121 für die **Zeitreihen** geöffnet hat
    (``services/energie_profil/monats_aus_tagen.py``); hier wird sie direkt
    gelesen statt über ``lade_monats_fakten(inkl_nur_tageswerte=True)``, und
    zwar aus einem Grund: Über die Fakten-Schicht käme sie als ``gespeichert``
    heraus und behauptete eine Herkunft, die sie nicht hat. Die Marke
    ``quellen.tagesebene`` und die eigene ``DatenquelleInfo`` sind der Punkt.

    ⭐ **Zwei Leser, eine Quelle.** Die anlagenweite Strom-Bilanz kommt aus
    ``monats_aus_tagen`` (Zähler · PV · BKW · Speicher). Die **Wärme/Klima**-
    Größen reicht der Aufrufer als ``wp_mengen`` herein — aus
    ``waerme_verlauf.lade_waerme_monatsmengen_je_geraet``, **demselben Leser,
    den der Verlauf daneben für seine Tage benutzt**. Das ist keine
    Bequemlichkeit: Genau dieses Nebeneinander war der Anlass (*„der Verlauf
    zeigt dieselben Tage, die Kacheln nicht"*), und eine zweite Quelle hätte
    zwei Zahlen erzeugt, wo eine gefragt war (S1).

    ⚠ **Sie werden hereingereicht statt hier geholt**, weil der Aufrufer sie ein
    zweites Mal braucht: für die Tabelle *Zahlen je Gerät*, deren Monatszeilen
    es im laufenden Monat noch nicht gibt. Zweimal zu lesen wäre dieselbe
    Abfrage zweimal.

    ⚠ **Die WP-Größen kommen als Rohfelder je Gerät** (``inv_<id>_…``), nicht
    aufgelöst — K3 und D1 fallen anschließend in ``_wp_strom_k3`` bzw.
    ``_wp_waerme_d1`` wie bei jeder anderen Nicht-DB-Quelle. Eine Quelle, die
    ihre Größen vorab auflöst, stünde als einzige neben der Kette.

    ⚠ **Kein HA-Zugriff.** Die Tagesebene liegt lokal; das war die Auflage, unter
    der N-121 entschieden wurde, und sie gilt hier genauso.

    Returns:
        ``{feld: (menge, DatenquelleInfo)}`` — nur Größen mit ``> 0``, wie in
        allen vier Collectoren. Keine Tagesspur ⇒ leeres Dict.
    """
    from backend.services.energie_profil.monats_aus_tagen import (
        lade_monats_summen_aus_tagen,
    )

    summen = await lade_monats_summen_aus_tagen(
        db, anlage_id, von=(jahr, monat), bis=(jahr, monat)
    )
    summe = summen.get((jahr, monat))
    wp_mengen = wp_mengen or {}

    if (summe is None or summe.tage <= 0) and not wp_mengen:
        return {}

    # Die Abdeckung gehört dazu (P4): Beginnt die Tagesspur erst mitten im
    # Monat — später eingerichtetes Add-on, Vollbackfill, der nicht zurückreicht
    # —, sagt die Provenanz-Zeile es (*„Tageswerte (ab 05.09.)"*). Derselbe
    # Slot, dieselbe Beschriftung wie beim Connector seit #361; beginnt sie am
    # Monatsersten, schweigt sie von selbst (`connector_deckt_monatsanfang`).
    # ⚠ Die Ränder der **beiden** Leser zusammen — die Wärme-Spur kann früher
    # beginnen als die Bilanz-Spur und umgekehrt.
    _erste = [t for t in (getattr(summe, "erster_tag", None), wp_von) if t]
    _letzte = [t for t in (getattr(summe, "letzter_tag", None), wp_bis) if t]
    quelle = DatenquelleInfo(
        quelle="tagesebene", konfidenz=80, zeitpunkt=datetime.now().isoformat(),
        abdeckung_von=datetime.combine(min(_erste), time.min) if _erste else None,
        abdeckung_bis=(
            datetime.combine(max(_letzte), time.min) + timedelta(days=1)
            if _letzte else None
        ),
    )
    resolved: dict[str, tuple[float, DatenquelleInfo]] = {}
    for feld, wert in (
        ("einspeisung_kwh", getattr(summe, "einspeisung_kwh", 0.0)),
        ("netzbezug_kwh", getattr(summe, "netzbezug_kwh", 0.0)),
        # `pv_kwh` ist Module + BKW — dieselbe PV-Achse wie im DB-Zweig.
        ("pv_erzeugung_kwh", summe.pv_kwh if summe is not None else 0.0),
        ("bkw_erzeugung_kwh", getattr(summe, "bkw_kwh", 0.0)),
        ("speicher_ladung_kwh", getattr(summe, "speicher_ladung_kwh", 0.0)),
        ("speicher_entladung_kwh", getattr(summe, "speicher_entladung_kwh", 0.0)),
    ):
        if wert > 0:
            resolved[feld] = (wert, quelle)

    # ── Wärme/Klima je Gerät (A-5) ──
    # Die Feldnamen sind die der **Registry**, nicht die der Tagesebene — genau
    # die Keys, die `_wp_strom_k3` und `_wp_waerme_d1` unten lesen. Damit läuft
    # die bestehende Kette (K3 · D1 · `typ_aggregation` · Systemarbeitszahl),
    # statt daneben eine zweite zu entstehen.
    for inv_id, m in wp_mengen.items():
        for feld, wert in (
            ("stromverbrauch_kwh", m.strom_kwh),
            ("waerme_kwh", m.waerme_kwh),
            ("heizenergie_kwh", m.heizung_kwh),
            ("warmwasser_kwh", m.warmwasser_kwh),
            ("strom_heizen_kwh", m.strom_heizen_kwh),
            ("strom_warmwasser_kwh", m.strom_warmwasser_kwh),
            ("kaelte_kwh", m.kaelte_kwh),
        ):
            if wert > 0:
                resolved[f"inv_{inv_id}_{feld}"] = (wert, quelle)
    # Der Kühlanteil als Anlagensumme — Eingang der Ersparnis-Rechnung, wie im
    # DB-Zweig (`fakt.wp.modus_strom_kuehlen_kwh`). Er ist eine **Teilmenge**
    # des WP-Stroms, keine eigene Achse, und läuft deshalb nicht durch K3.
    _kuehl = sum(m.modus_strom_kuehlen_kwh for m in wp_mengen.values())
    if _kuehl > 0:
        resolved["wp_modus_kuehlen_kwh"] = (_kuehl, quelle)
    return resolved

# =============================================================================
# Endpoint
# =============================================================================

@router.get("/{anlage_id}", response_model=AktuellerMonatResponse)
async def get_aktueller_monat(
    anlage_id: int,
    jahr: Optional[int] = None,
    monat: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Übersicht eines Monats mit Daten aus allen verfügbaren Quellen.

    Reihenfolge der vier Quellen (spätere überschreiben frühere):
    1. Gespeicherte Monatsdaten (85%) — DB
    2. Connector (90%) — Geräte-Snapshot-Delta
    3. MQTT-Inbound (91%) — Energy-Topics aus Smarthome (nur aktueller Monat)
    4. HA Statistics (92%) — Recorder-DB

    **Die Konfidenz allein entscheidet nicht** — überschreiben darf eine
    frischere Quelle nur im **laufenden** Monat (Live-Vorschau). Im
    abgeschlossenen Monat sind die gespeicherten Werte authoritativ: Connector
    (#325 detlefh68) und HA-Statistics (#118 Safi105) füllen dort nur
    **fehlende** Felder (`setdefault`), MQTT wird gar nicht erst gesammelt.
    Ein importierter Monatswert (Cloud-/Portal-/CSV-Import) wird von einem
    zugeordneten HA-Sensor hier also **nicht** verdrängt — die Zuordnung
    stehen zu lassen kostet ihn nichts.

    Was „fehlend" heißt, entscheiden die `> 0`-Gates in `_collect_saved_data`:
    eine gespeicherte 0,0 gilt der Kaskade nicht als Wert und darf gefüllt
    werden. Begründung im Docstring dort.

    **Nur diese Route.** Auf der **Schreib**-Seite gilt die Aussage nicht:
    `external:portal_import` (Cloud-/Portal-Import) und `external:ha_statistics`
    stehen auf derselben Hierarchie-Stufe (`core/source_priority.py`), gleiche
    Stufe ist Last-Writer-Wins. Ein HA-Statistik-Import mit gesetztem
    `ueberschreiben` überschreibt einen Cloud-Wert dauerhaft in der DB. Wer
    den Wert wirklich festnageln will, pflegt ihn im Formular (`manual:form`,
    Stufe 1 — die einzige, die beide schlägt).

    Der Connector überschreibt zusätzlich nur mit belegter Monatsabdeckung
    (#361), sonst ist sein Delta ein Teilzeitraum.

    Die Regeln selbst stehen in `core/berechnungen/datenquellen.py`
    (ADR-001) — hier steht nur, was diese Route hineinreicht.
    """
    # N-267: Zeittarif-Cache dieser Anfrage (SIEBEN Bildungsstellen in dieser
    # Route lesen ihn) — Begruendung im Block ueber `_zeittarif_preis`.
    _zt_cache: dict = {}
    # Anlage mit Investitionen laden
    result = await db.execute(
        select(Anlage)
        .options(selectinload(Anlage.investitionen))
        .where(Anlage.id == anlage_id)
    )
    anlage = result.scalar_one_or_none()
    if not anlage:
        raise not_found("Anlage")

    now = datetime.now()
    if jahr is None:
        jahr = now.year
    if monat is None:
        monat = now.month
    ist_aktueller_monat = (jahr == now.year and monat == now.month)
    # Das gemessene Fenster des Monats — EIN Anker für alle Größen, die im
    # laufenden Monat mit den abgelaufenen Tagen wachsen (SOLL · Grundlast ·
    # Speicher-Auslastung). Vorher zählte jede dieser drei Stellen ihre Tage
    # selbst, mit drei Kopien derselben `min(heute.day, tage_im_monat)`-Zeile.
    fenster = monatsfenster(jahr, monat, heute=now.date())
    investitionen = [i for i in anlage.investitionen if i.aktiv]

    # ── Daten sammeln (I/O) — Zusammenführung nach Präzedenz im SoT-Helper ──
    # Sammeln bleibt hier (DB/HA-Zugriff); die Merge-/Override-Regeln leben in
    # core/berechnungen/datenquellen.merge_datenquellen (ADR-001) — eine Stelle,
    # symmetrie-getestet, ohne Drift zwischen den Quellen-Zweigen.
    # MQTT wird für abgeschlossene Monate gar nicht erst gesammelt.
    #
    # ADR-002/**P10**: die Monatszeile wird genau einmal aufbereitet. Diese
    # Route mischt vier Quellen, von denen die Schicht ausdrücklich nur EINE
    # kennt (die DB — Live/Connector sind Nicht-Ziel, KONZEPT-MONATS-FAKTEN §4).
    # Also kommt der DB-Zweig aus den Fakten, die Präzedenz bleibt hier.
    monats_fakten = await lade_monats_fakten(
        db, anlage_id, von=(jahr, monat), bis=(jahr, monat)
    )
    monats_fakt = monats_fakten[0] if monats_fakten else None

    saved = _collect_saved_data(monats_fakt)
    connector = await _collect_connector_data(anlage, jahr, monat)
    mqtt_energy = (
        await _collect_mqtt_inbound_data(db, anlage, investitionen, jahr, monat)
        if ist_aktueller_monat else {}
    )
    ha_stats = await _collect_ha_statistics_data(anlage, jahr, monat)
    # Fünfte Quelle (N-472) — nur im laufenden Monat, und das ist eine Aussage
    # über die Kategorie, nicht über den Aufwand: Im laufenden Monat IST eine
    # Teilmenge der Tage die vollständige Auskunft über das bisher Geschehene,
    # und alle vier Quellen darüber messen dort ebenfalls nur bis jetzt. In
    # einem abgeschlossenen Monat wäre dieselbe Teilmenge eine stille
    # Untertreibung eines Monatswertes — dort ist die Antwort der
    # Monatsabschluss, auf den der Daten-Checker ohnehin zeigt
    # (`daten_checker/monatsdaten.py`, MONATSDATEN_VOLLSTAENDIGKEIT).
    # Die Wärme/Klima-Mengen der Tagesebene — EINMAL gelesen, zweimal gebraucht:
    # für die Kacheln (über den Collector) und für die Tabelle *Zahlen je Gerät*
    # weiter unten, deren Monatszeilen es im laufenden Monat noch nicht gibt.
    _tages_wp_mengen: dict = {}
    _tages_wp_von = _tages_wp_bis = None
    if ist_aktueller_monat and investitionen:
        from calendar import monthrange

        from backend.services.energie_profil.waerme_verlauf import (
            lade_waerme_monatsmengen_je_geraet,
        )
        _tages_wp_mengen, _tages_wp_von, _tages_wp_bis = (
            await lade_waerme_monatsmengen_je_geraet(
                db, anlage, {str(i.id): i for i in investitionen},
                date(jahr, monat, 1), date(jahr, monat, monthrange(jahr, monat)[1]),
            )
        )
    tagesebene = (
        await _collect_tagesebene_data(
            db, anlage_id, jahr, monat,
            wp_mengen=_tages_wp_mengen, wp_von=_tages_wp_von, wp_bis=_tages_wp_bis,
        )
        if ist_aktueller_monat else {}
    )

    # Abdeckung des Connector-Deltas — sie steht in jedem seiner
    # DatenquelleInfo (eine Instanz für alle Felder), der erste Eintrag genügt.
    # Naive Zeitstempel wie in `_calc_month_delta`, daher direkt mit dem
    # naiven Monatsanfang vergleichbar.
    connector_abdeckung_von = next(
        (info.abdeckung_von for _, info in connector.values() if info.abdeckung_von),
        None,
    )

    quellen_args = dict(
        saved=saved,
        connector=connector,
        mqtt_energy=mqtt_energy,
        ha_stats=ha_stats,
        ist_aktueller_monat=ist_aktueller_monat,
        connector_abdeckung_von=connector_abdeckung_von,
        monat_start=datetime(jahr, monat, 1),
        tagesebene=tagesebene,
    )
    resolved: dict[str, tuple[float, DatenquelleInfo]] = merge_datenquellen(**quellen_args)

    # Felder, die nur einen Teilzeitraum messen — sie dürfen die Aggregation der
    # Komponenten-Werte nicht unterdrücken, siehe `direct_fields` unten (#361).
    # Drei Herkünfte: Connector-Delta ohne Abdeckung des Monatsanfangs, MQTT mit
    # Rückfall auf den ersten Stand (N-472) und die Tagesebene. Woran ein
    # MQTT-Feld als Rückfall erkennbar ist, steht in `_collect_mqtt_inbound_data`:
    # an der gesetzten `abdeckung_von` seiner eigenen `DatenquelleInfo`.
    teilzeitraum = teilzeitraum_felder(
        **quellen_args,
        mqtt_ab_monatsbeginn={
            k for k, (_, info) in mqtt_energy.items() if info.abdeckung_von is None
        },
    )

    # ── aggregiere_typen (Vorlage 2: Abschnitt in aggregation.py, Schnittstelle 5 ein / 1 aus) ──
    _out = aggregiere_typen(investitionen=investitionen, jahr=jahr, monat=monat, resolved=resolved, teilzeitraum=teilzeitraum)
    if "direct_fields" in _out: direct_fields = _out["direct_fields"]
    # ── emob_max_pool (Vorlage 2: Abschnitt in aggregation.py, Schnittstelle 5 ein / 0 aus) ──
    _out = emob_max_pool(direct_fields=direct_fields, investitionen=investitionen, jahr=jahr, monat=monat, resolved=resolved)
    # ── extrahiere_werte (Vorlage 2: Abschnitt in aggregation.py, Schnittstelle 2 ein / 10 aus) ──
    _out = extrahiere_werte(monats_fakt=monats_fakt, resolved=resolved)
    if "abgabe_dritte" in _out: abgabe_dritte = _out["abgabe_dritte"]
    if "einspeisung" in _out: einspeisung = _out["einspeisung"]
    if "erzeugung_bilanz" in _out: erzeugung_bilanz = _out["erzeugung_bilanz"]
    if "get_val" in _out: get_val = _out["get_val"]
    if "hinweise" in _out: hinweise = _out["hinweise"]
    if "netzbezug" in _out: netzbezug = _out["netzbezug"]
    if "pv" in _out: pv = _out["pv"]
    if "sonstiges_erz_bilanz" in _out: sonstiges_erz_bilanz = _out["sonstiges_erz_bilanz"]
    if "speicher_entladung" in _out: speicher_entladung = _out["speicher_entladung"]
    if "speicher_ladung" in _out: speicher_ladung = _out["speicher_ladung"]
    # ── berechne_bilanzwerte (Vorlage 2: Abschnitt in aggregation.py, Schnittstelle 8 ein / 5 aus) ──
    _out = berechne_bilanzwerte(abgabe_dritte=abgabe_dritte, einspeisung=einspeisung, erzeugung_bilanz=erzeugung_bilanz, netzbezug=netzbezug, pv=pv, sonstiges_erz_bilanz=sonstiges_erz_bilanz, speicher_entladung=speicher_entladung, speicher_ladung=speicher_ladung)
    if "autarkie" in _out: autarkie = _out["autarkie"]
    if "direktverbrauch" in _out: direktverbrauch = _out["direktverbrauch"]
    if "eigenverbrauch" in _out: eigenverbrauch = _out["eigenverbrauch"]
    if "ev_quote" in _out: ev_quote = _out["ev_quote"]
    if "gesamtverbrauch" in _out: gesamtverbrauch = _out["gesamtverbrauch"]
    # ── finanzen_des_monats (Vorlage 2: Abschnitt in finanzen.py, Schnittstelle 8 ein / 19 aus) ──
    _out = await finanzen_des_monats(_zt_cache=_zt_cache, anlage_id=anlage_id, db=db, eigenverbrauch=eigenverbrauch, einspeisung=einspeisung, jahr=jahr, monat=monat, netzbezug=netzbezug)
    if "allgemein_tarif" in _out: allgemein_tarif = _out["allgemein_tarif"]
    if "einspeise_cent" in _out: einspeise_cent = _out["einspeise_cent"]
    if "einspeise_erloes" in _out: einspeise_erloes = _out["einspeise_erloes"]
    if "einspeisung_neg_preis" in _out: einspeisung_neg_preis = _out["einspeisung_neg_preis"]
    if "ev_ersparnis" in _out: ev_ersparnis = _out["ev_ersparnis"]
    if "grundgebuehr" in _out: grundgebuehr = _out["grundgebuehr"]
    if "monats_benzinpreis" in _out: monats_benzinpreis = _out["monats_benzinpreis"]
    if "monats_gaspreis" in _out: monats_gaspreis = _out["monats_gaspreis"]
    if "netto_ertrag" in _out: netto_ertrag = _out["netto_ertrag"]
    if "netzbezug_arbeitspreis_kosten" in _out: netzbezug_arbeitspreis_kosten = _out["netzbezug_arbeitspreis_kosten"]
    if "netzbezug_durchschnittspreis" in _out: netzbezug_durchschnittspreis = _out["netzbezug_durchschnittspreis"]
    if "netzbezug_kosten" in _out: netzbezug_kosten = _out["netzbezug_kosten"]
    if "netzbezug_preis_abdeckung" in _out: netzbezug_preis_abdeckung = _out["netzbezug_preis_abdeckung"]
    if "netzbezug_preis_cent" in _out: netzbezug_preis_cent = _out["netzbezug_preis_cent"]
    if "netzbezug_preis_effektiv_cent" in _out: netzbezug_preis_effektiv_cent = _out["netzbezug_preis_effektiv_cent"]
    if "netzbezug_preis_herkunft" in _out: netzbezug_preis_herkunft = _out["netzbezug_preis_herkunft"]
    if "nicht_vergueteter_erloes" in _out: nicht_vergueteter_erloes = _out["nicht_vergueteter_erloes"]
    if "tarife" in _out: tarife = _out["tarife"]
    if "zaehlergebuehr_jahr" in _out: zaehlergebuehr_jahr = _out["zaehlergebuehr_jahr"]
    # ── komponenten_ersparnis (Vorlage 2: Abschnitt in finanzen.py, Schnittstelle 1 ein / 5 aus) ──
    _out = komponenten_ersparnis(get_val=get_val)
    if "emob_ersparnis" in _out: emob_ersparnis = _out["emob_ersparnis"]
    if "wp_ersparnis" in _out: wp_ersparnis = _out["wp_ersparnis"]
    if "wp_ersparnis_berechnung_text" in _out: wp_ersparnis_berechnung_text = _out["wp_ersparnis_berechnung_text"]
    if "wp_strom" in _out: wp_strom = _out["wp_strom"]
    if "wp_waerme" in _out: wp_waerme = _out["wp_waerme"]
    # ── Arbeitszahl aus dem Layer, nicht aus dem Client (R2/W-3) ────────────
    #
    # ⚠ **Der abgeleitete Anteil kommt IMMER aus den Monats-Fakten**, auch wenn
    # die Mengen oben aus einer anderen Quelle gewonnen wurden. Er beschreibt
    # die **Herkunft der IMD-Zeilen** dieses Monats, und die ändert sich nicht
    # dadurch, dass eine Menge über HA-Statistik statt aus der Datenbank kam.
    # Im Zweifel sperrt er — „unbekannt" ist besser als „falsch" (P4).
    wp_waerme_abgeleitet_kwh = (
        monats_fakt.wp.waerme_abgeleitet_kwh if monats_fakt is not None else 0.0
    )
    # ── R2: alle drei erkennbaren Lagen, über die eine Layer-Stelle ─────────
    #
    # **Gerät:** Trägt der Block Strom von Geräten, deren Wärme fehlt? Der Block
    # *Wärme/Klima* aggregiert alle Wärmepumpen der Anlage — bei dietmar1968
    # „Wärmepumpe · Klimaanlage" — und nur eines meldete Wärme.
    #
    # **Anwender-Angabe:** Heizstab-Strom auf dem WP-Zähler (Fall H-C) oder ein
    # bivalenter Zweiterzeuger am selben Kreis. Beides ist aus keiner Messreihe
    # ableitbar; die Reihenfolge (Angabe schlägt Erkennung) steht im Layer.
    #
    # **Zeitraum (SOLL §4.2 Fall 3):** ⭐ Diese Sicht ist die **einzige** mit
    # Vier-Quellen-Auflösung — und `teilzeitraum` weist genau die Felder aus,
    # deren Endwert nur einen Ausschnitt des Monats misst (#361, coolxmad #353:
    # ein frisch eingerichteter Connector, der erst mitten im Monat zu zählen
    # begann). Steht **genau eine** der beiden Seiten darin, tragen Q und E
    # verschieden lange Zeiträume und der Quotient wäre einer aus zwei
    # Wirklichkeiten.
    #
    # ⛔ **Bewusst kein Zählen von Tagen mit Wert.** Der naheliegende Weg —
    # „an wie vielen Tagen des Monats gab es überhaupt eine Zahl?" — kann
    # *„kein Wert, weil das Gerät stand"* nicht von *„kein Wert, weil der Sensor
    # fehlte"* unterscheiden. Bei einer Wärmepumpe im Juli ist Ersteres der
    # Normalfall; ein Wächter darauf meldete jeden Sommer bei jeder Anlage.
    # Genau die Fehlalarm-Klasse, die §2i-6 schon einmal eingefangen hat.
    _wp_seiten_teilzeitraum = sum(
        1 for f in ("wp_waerme_kwh", "wp_strom_kwh") if f in teilzeitraum
    )
    wp_abgrenzung_verletzt = abgrenzungs_grund(
        abgrenzung_stoerung=(
            monats_fakt.wp.abgrenzung_stoerung if monats_fakt is not None else None
        ),
        # R2/Bauart (SOLL §5): Wärmepumpe und Split-Klimaanlage in einer Zahl.
        bauarten_gemischt=(
            monats_fakt is not None and monats_fakt.wp.bauarten_gemischt
        ),
        geraete_ohne_waerme=(
            monats_fakt is not None
            and monats_fakt.wp.waerme_deckt_nicht_alle_geraete
        ),
        zeitraum_versetzt=_wp_seiten_teilzeitraum == 1,
        # N-441: die Gegenrichtung. ⚠ Sie haengt **hinter** `zeitraum_versetzt`
        # in der Kette (`abgrenzungs_grund`), weil hier — und nur hier — beide
        # zugleich wahr sein koennen: Der Zeitraum-Versatz entsteht aus der
        # Vier-Quellen-Aufloesung dieses Monats. Vorn eingehaengt haette das
        # neue Glied dort einen heute gezeigten Grund samt Hub-Link getauscht.
        geraete_verschieden=(
            monats_fakt is not None and monats_fakt.wp.geraete_verschieden
        ),
    )
    # ⭐ **Die Frage „welche Funktion trifft die Verletzung?" steht weiter
    # unten** (SOLL §3.2b; seit WK-16i hinter den Geräte-Kennzahlen, denn aus
    # ihnen beantwortet der laufende Monat sie).
    # W-14 + E4: Der funktionsfremde Strom (Kühlen · Lüften · Entfeuchten) kommt
    # — wie der abgeleitete Anteil darüber — IMMER aus den Monats-Fakten. Er
    # beschreibt die Aufteilung der IMD-Zeilen dieses Monats, und die ändert sich
    # nicht dadurch, dass eine Menge über HA-Statistik statt aus der Datenbank
    # kam. Eine Größe statt drei Summanden: die Aufzählung an vier Aufrufern war
    # die Bauform, an der W-14 entstanden ist.
    #
    # ⭐ **SOLL-§9-E7/Option A (12.09.2026): der ABZUG, nicht die Menge.** Hier
    # stand bis dahin `modus_strom_funktionsfremd_kwh`. Bei getrennter
    # Strommessung mit nur **abgeleiteter** Aufteilung kürzte das den Nenner um
    # eine Menge, die er nie enthielt — der Split verteilt
    # `strom_heizen + strom_warmwasser`, er stellt nichts daneben. Die
    # Mengen-Größe bleibt daneben stehen und trägt weiter die Aufteilung (K1).
    wp_strom_funktionsfremd_kwh = (
        monats_fakt.wp.modus_strom_funktionsfremd_abzug_kwh
        if monats_fakt is not None else 0.0
    )
    # ── E1b: die ANLAGENWEITE Zahl, als Schranke statt als Strich ──────────
    #
    # ⭐ **Entscheid Gernot, 14.09.2026.** Hier stand `arbeitszahl(...)` mit
    # `wp_abgrenzung_verletzt` — und damit sperrten **zwei** Lagen die Zahl, die
    # sie gar nicht falsch machen, sondern nur zu **klein**: gemischte Bauarten
    # (`GRUND_BAUARTEN_GEMISCHT`) und Geräte ohne Wärmemeldung
    # (`GRUND_GERAETE_OHNE_WAERME`). In beiden steht Strom im Nenner, dem keine
    # gemessene Wärme gegenübersteht; der Quotient ist dann eine **untere
    # Schranke** — und die ist eine wahre Aussage.
    #
    # ⚠ **Die übrigen Gründe sperren weiter, und das ist der Kern der
    # Unterscheidung:** Fremdanteil-Angabe, Zeitraum-Versatz, „Wärme und Strom
    # von verschiedenen Geräten" und „… aus verschiedenen Monaten" kippen die
    # Zahl nach **oben** oder in unbekannte Richtung. Eine untere Schranke wäre
    # dort eine Falschaussage. Deshalb dieselbe Kette ein zweites Mal — ohne die
    # beiden Glieder, die zur Schranke werden.
    _wp_invs_fuer_block = [i for i in investitionen if i.typ == "waermepumpe"]
    _wp_kennzahlen_je_geraet = await lade_kennzahlen_je_geraet(
        db, anlage_id, _wp_invs_fuer_block, von=(jahr, monat), bis=(jahr, monat),
    )
    # ── N-472/A-5: dieselbe Tabelle im laufenden Monat, aus der Tagesebene ──
    #
    # ⛔ **Der Dienst liest `InvestitionMonatsdaten` — die es im laufenden Monat
    # nicht gibt.** Die Tabelle *Zahlen je Gerät* blieb deshalb leer, während
    # Kachel und Verlauf darüber dieselben Tage vollständig zeigten; genau das
    # Bild, gegen das dieses Paket gebaut ist, eine Ebene tiefer.
    #
    # ⭐ **Die Kennzahl entsteht trotzdem in derselben Funktion.** Der Dienst
    # trägt für diesen Fall seit WK-16ab `mengen_aus_tageswerten` — dieselbe
    # zweite Herkunft, die *Cockpit → Tag* benutzt; nur der Zeitraum ist ein
    # Monat statt eines Tages. Es entsteht **keine** zweite Rechenstelle.
    #
    # ⚠ **Ersetzt wird nur, was leer ist.** Trägt ein Gerät für diesen Monat
    # schon eine Zeile (gepflegter Teilmonat, Import), gewinnt sie — dieselbe
    # Präzedenz wie oben in der Quellen-Kaskade.
    if _tages_wp_mengen:
        from backend.services.waermepumpe_kennzahlen_je_geraet import (
            kennzahlen_aus_mengen,
            mengen_aus_tageswerten,
        )
        _wp_invs_by_id = {i.id: i for i in _wp_invs_fuer_block}
        _ersetzt: list = []
        for _k in _wp_kennzahlen_je_geraet:
            _m = _tages_wp_mengen.get(str(_k.inv_id))
            _inv = _wp_invs_by_id.get(_k.inv_id)
            if _m is None or _inv is None or traegt_menge(_k.mengen):
                _ersetzt.append(_k)
                continue
            _ersetzt.append(kennzahlen_aus_mengen(mengen_aus_tageswerten(
                _inv,
                strom_kwh=_m.strom_kwh,
                waerme_kwh=waerme_gesamt_kwh(
                    _m.waerme_kwh or None,
                    _m.heizung_kwh + _m.warmwasser_kwh,
                    None,
                ),
                heizung_kwh=_m.heizung_kwh,
                warmwasser_kwh=_m.warmwasser_kwh,
                strom_heizen_kwh=_m.strom_heizen_kwh,
                strom_warmwasser_kwh=_m.strom_warmwasser_kwh,
                kaelte_kwh=_m.kaelte_kwh,
                modus_strom_kuehlen_kwh=_m.modus_strom_kuehlen_kwh,
                funktionsfremd_abzug_kwh=_m.funktionsfremd_abzug_kwh,
                waerme_ist_gesamt=bool(_m.waerme_kwh),
            )))
        _wp_kennzahlen_je_geraet = _ersetzt
    # ── S5 anlagenweit (WK-16i, N-503): die Eingänge der Funktions-Zahlen ──
    #
    # ⛔ **Sie kamen bis zum 15.09.2026 ausschließlich aus den Monats-Fakten**,
    # und der laufende Monat hat keine `Monatsdaten`-Zeile. Im Kasten stand
    # deshalb *„Strom nicht getrennt je Funktion gemessen → Getrennte
    # Strommessung einschalten"*, während die Tabelle **direkt darunter** 5,58
    # und 3,32 zeigte (r28/Prüfstand, September 2026) — zwei Leser, ein
    # Bildschirm (dieselbe Klasse wie N-492).
    #
    # ⭐ **S5 gilt für alle anlagenweiten Wärme/Klima-Eingänge, nicht nur für
    # die Kacheln.** Trägt die Monatszeile eine Menge, gilt sie — sonst
    # entstehen die Eingänge aus **denselben** Geräte-Mengen, die die Tabelle
    # oben speist. Die Faltung steht an EINER Stelle
    # (`waerme_klima_block.funktions_eingaenge_der_anlage`); hier wird sie nur
    # gerufen, und beide Herkünfte tragen dieselben Feldnamen (F-56).
    _wp_zeile_traegt = monats_fakt is not None and traegt_menge(monats_fakt.wp)
    _wp_funktion = (
        monats_fakt.wp if _wp_zeile_traegt
        else funktions_eingaenge_der_anlage(_wp_kennzahlen_je_geraet)
    )
    # ── SOLL §3.2b (10.09.2026): WELCHE Funktionen die Verletzung trifft ──
    #
    # Bis dahin galt sie unbesehen fuer beide Zeilen — bei einer Waermepumpe
    # neben einer Split-Klimaanlage standen deshalb drei Striche, waehrend der
    # Komponenten-Hub fuer dasselbe Geraet 3,0 und 2,5 auswies.
    #
    # ⚠ Die Gleichheit wird je Funktion aus den BEITRAEGEN gezaehlt, nicht aus
    # der Bauart: `strom_warmwasser_kwh` wird ungefiltert gelesen, und
    # `heizenergie_kwh` traegt kein `!luft_luft` — beides kann eine Klimaanlage
    # tragen. Nur Gleichheit in BEIDE Richtungen schuetzt vor einer falschen
    # Zahl (zu hoch wie zu niedrig).
    #
    # ⭐ **Aus derselben Quelle wie die Mengen darüber** (WK-16i). ⚠ Ein Dict aus
    # lauter `None` wirkt wie das frühere `None` (`abgrenzung_je_funktion` legt
    # `global_grund` dann ohnehin auf alle Funktionen); neu ist allein, dass der
    # Rückfall die Deckung **beantworten** kann, statt sie offenzulassen.
    _wp_deckung_je_funktion = {
        f: _wp_funktion.deckung_je_funktion(f) for f in ARBEITSZAHL_FUNKTIONEN
    }
    _wp_abgrenzung_je_funktion = abgrenzung_je_funktion(
        abgrenzung_stoerung=(
            monats_fakt.wp.abgrenzung_stoerung if monats_fakt is not None else None
        ),
        bauarten_gemischt=(
            monats_fakt is not None and monats_fakt.wp.bauarten_gemischt
        ),
        geraete_ohne_waerme=(
            monats_fakt is not None
            and monats_fakt.wp.waerme_deckt_nicht_alle_geraete
        ),
        zeitraum_versetzt=_wp_seiten_teilzeitraum == 1,
        deckung_je_funktion=_wp_deckung_je_funktion,
    )
    _wp_strom_ohne_waerme, _wp_geraete_ohne_waerme = schranken_eingang(
        _wp_kennzahlen_je_geraet,
    )
    wp_abgrenzung_sperrt = abgrenzungs_grund(
        abgrenzung_stoerung=(
            monats_fakt.wp.abgrenzung_stoerung if monats_fakt is not None else None
        ),
        zeitraum_versetzt=_wp_seiten_teilzeitraum == 1,
        geraete_verschieden=(
            monats_fakt is not None and monats_fakt.wp.geraete_verschieden
        ),
    )
    wp_arbeitszahl = systemarbeitszahl(
        wp_waerme, wp_strom,
        waerme_abgeleitet_kwh=wp_waerme_abgeleitet_kwh,
        kuehlstrom_kwh=wp_strom_funktionsfremd_kwh,
        strom_ohne_waerme_kwh=_wp_strom_ohne_waerme,
        geraete_ohne_waerme=_wp_geraete_ohne_waerme,
        abgrenzung_verletzt=wp_abgrenzung_sperrt,
    )
    # B4 (C-2): Herkunft und Vorbehalt — der Faktor nur bei EINER Wärmepumpe
    # (bei mehreren gibt es keinen einen Faktor, der Text nennt dann die Regel).
    from backend.core.berechnungen.modus_split import heiz_effizienz_gepflegt
    from backend.core.berechnungen.waermepumpe_kennzahl import (
        ersparnis_vorbehalt as _ersparnis_vorbehalt,
        waerme_herkunft as _waerme_herkunft,
    )
    _wp_invs_alle = [i for i in investitionen if i.typ == "waermepumpe"]
    _wp_abgeleitet = wp_waerme_abgeleitet_kwh > 0
    wp_waerme_herkunft = _waerme_herkunft(
        _wp_abgeleitet,
        heiz_effizienz_gepflegt(_wp_invs_alle[0].parameter)
        if (_wp_abgeleitet and len(_wp_invs_alle) == 1) else None,
    )
    wp_ersparnis_vorbehalt = _ersparnis_vorbehalt(
        waerme_abgeleitet=_wp_abgeleitet,
        abgrenzung=monats_fakt.wp.abgrenzung_stoerung if monats_fakt is not None else None,
    )

    if wp_waerme is not None and wp_strom is not None and allgemein_tarif:
        # Ohne eigenen WP-Tarif gilt der allgemeine Bezugspreis — bei flexiblem
        # Tarif also der Monatsdurchschnitt, wie im per-Investition-Block
        # (`wp_p`) schon immer. N-267: ein eigener WP-Tarif kann eigene Fenster
        # tragen (§14a-Nachttarife), deshalb ueber denselben Helfer.
        wp_preis_cent = await _zeittarif_preis(
            db, anlage_id, jahr, monat, tarife.get("waermepumpe"),
            netzbezug_preis_effektiv_cent, _zt_cache,
        )
        wp_invs = [i for i in investitionen if i.typ == "waermepumpe"]
        wp_ref_parameter = wp_invs[0].parameter if wp_invs else None

        wp_ersparnis_result = berechne_wp_ersparnis(
            wp_waerme_kwh=wp_waerme,
            wp_strom_kwh=wp_strom,
            wp_strompreis_cent=wp_preis_cent,
            wp_parameter=wp_ref_parameter,
            monats_gaspreis_cent=monats_gaspreis,
            # E-B: Kühlen ersetzt keine Heizung (#263 K-2).
            strom_kuehlen_kwh=get_val("wp_modus_kuehlen_kwh") or 0.0,
        )
        wp_ersparnis = round(wp_ersparnis_result.ersparnis_euro, 2)
        wp_ersparnis_berechnung_text = wp_ersparnis_berechnung(
            wp_ersparnis_result, wp_waerme, wp_strom, wp_preis_cent, wp_ref_parameter,
        )

    # G20-2 (Gernot 2026-07-20): Die eMob-Ersparnis-Aggregation folgt weiter unten
    # als **Summe der Per-Fahrzeug-Ersparnisse** (dieselben Werte wie die
    # investitionen_financials-Zeilen), NACHDEM diese gebaut sind. Der frühere
    # Einmal-Lauf über die Gesamt-km mit dem parameter-Satz des ERSTEN E-Autos
    # (pick_emob_ref_parameter) rechnete bei unterschiedlichem Verbrauch je Fahrzeug
    # falsch (Demo: 167,79 € statt 65,37 + 85,59 = 150,96 €).
    # [[feedback_aggregator_symmetrie]] [[feedback_aggregations_drift]]

    # BKW-Ersparnis wird NICHT separat ausgewiesen — BKW-Erzeugung fließt in
    # pv_erzeugung_total und damit in eigenverbrauch ein → bereits in ev_ersparnis enthalten.

    # ── betriebskosten_und_sonstige_positionen (Vorlage 2: Abschnitt in finanzen.py, Schnittstelle 2 ein / 9 aus) ──
    _out = betriebskosten_und_sonstige_positionen(investitionen=investitionen, monats_fakt=monats_fakt)
    if "anlage_sonstige_ausgaben" in _out: anlage_sonstige_ausgaben = _out["anlage_sonstige_ausgaben"]
    if "anlage_sonstige_ertraege" in _out: anlage_sonstige_ertraege = _out["anlage_sonstige_ertraege"]
    if "betriebskosten_anteilig" in _out: betriebskosten_anteilig = _out["betriebskosten_anteilig"]
    if "betriebskosten_anteilig_anzahl" in _out: betriebskosten_anteilig_anzahl = _out["betriebskosten_anteilig_anzahl"]
    if "betriebskosten_anteilig_jahr" in _out: betriebskosten_anteilig_jahr = _out["betriebskosten_anteilig_jahr"]
    if "gesamtnettoertrag" in _out: gesamtnettoertrag = _out["gesamtnettoertrag"]
    if "sonstige_ausgaben_total" in _out: sonstige_ausgaben_total = _out["sonstige_ausgaben_total"]
    if "sonstige_ertraege_total" in _out: sonstige_ertraege_total = _out["sonstige_ertraege_total"]
    if "sonstige_netto_total" in _out: sonstige_netto_total = _out["sonstige_netto_total"]
    # ── Komponenten-Detail aus den Monats-Fakten (ADR-002/P10, C1d) ──
    # Bis C1d lief hier eine eigene `InvestitionMonatsdaten`-Batch mit fünf
    # anlagenweiten Faltungen daneben — die letzte des Baums. Sie hatte zwei
    # Fehler, die die Schicht nicht hat:
    #
    #   * **E-Auto und Wallbox wurden roh addiert.** Beide messen denselben
    #     Fluss aus zwei Perspektiven; wo beide gepflegt sind, stand die
    #     Netzladung doppelt in der Kachel, im T-Konto (× Arbeitspreis!) und in
    #     der Jahressumme. Die Schicht poolt kanonisch (#262).
    #   * **Kein Laufzeit-Filter.** Die Batch nahm jede IMD-Zeile des Typs,
    #     auch aus Monaten vor der Anschaffung (#236-Klasse).
    #
    # `monats_fakt` ist oben schon geladen (derselbe Monat) — hier wird nichts
    # nachgeladen. Die Präzedenz der vier Quellen bleibt davon unberührt: der
    # Detailblock war immer schon reiner DB-Zweig.
    mf_speicher = monats_fakt.speicher if monats_fakt else None
    mf_wp = monats_fakt.wp if monats_fakt else None
    mf_emob = monats_fakt.emob if monats_fakt else None
    mf_bkw = monats_fakt.bkw if monats_fakt else None
    mf_sonstiges = monats_fakt.sonstiges if monats_fakt else None
    #: Welche Typen im Monat eine SICHTBARE Zeile hatten — trennt „0 gemessen"
    #: von „gar keine Daten" (P4). Vorher leistete das die Frage, ob die
    #: IMD-Batch für den Typ etwas hergab.
    typen_mit_zeile = monats_fakt.meta.typen_mit_zeile if monats_fakt else frozenset()

    # Speicher: Kapazität, Arbitrage-Ladung, Wirkungsgrad, Vollzyklen, Auslastung
    speicher_ladung_netz = None
    speicher_wirkungsgrad = None
    speicher_vollzyklen = None
    speicher_kapazitaet = None
    speicher_auslastungs_basis = None
    speicher_auslastung = None
    speicher_ersparnis = None

    # F-24: nur die Speicher, die es in DIESEM Monat gab. `investitionen` ist
    # oben nur über den `aktiv`-Haken gefiltert — ein ersetztes Gerät trägt
    # aber ein **Stilllegungsdatum** und bleibt `aktiv` (sonst verschwände es
    # auch aus der Historie). Ohne diesen Filter summierte die Kachel unten
    # altes **und** neues Gerät: an einer Kopie des Dev-Bestands 15,4 + 30,8 =
    # 46,2 statt 30,8 kWh, und damit auch Vollzyklen und Auslastung daneben.
    speicher_invs = [
        i for i in investitionen
        if i.typ == "speicher" and i.ist_aktiv_im_monat(jahr, monat)
    ]
    speicher_soc_drift_flag = False
    # F-22: worauf der ausgewiesene η beruht — `soc_korrigiert` · `fenster_lang`
    # · `fenster-zu-kurz` · `keine-ladung` · `nicht-ermittelbar`. Trägt den
    # Grund, wenn kein Wert dasteht (P4: unvollständige Antworten sagen es).
    speicher_wirkungsgrad_quelle = None
    speicher_eff_ladepreis = None
    speicher_eff_ladepreis_quelle = None
    speicher_imd_ladepreis = None
    if speicher_invs:
        # Kapazität aus parameter — BRUTTO, das ist die Zyklen-Konvention des
        # ganzen Baums (docs/BERECHNUNGEN.md §Speicher). Eine hier früher
        # zusätzlich gebildete Netto-Summe (`nutzbare_kapazitaet_kwh` mit
        # Brutto-Fallback) wurde nirgends gelesen — sie suggerierte eine
        # zweite Basis, die es an dieser Stelle nicht gibt (R22-4).
        kap_sum = sum(get_speicher_kapazitaet_kwh(i) or 0 for i in speicher_invs)
        if kap_sum > 0:
            speicher_kapazitaet = round(kap_sum, 1)

        # Arbitrage-Ladung + kWh-gewichteter Ø der erfassten Ladepreise als
        # Preis-Fallback für die Netzladung-Kosten-Kachel (R15-1) — beides aus
        # der Schicht (Kanon-Key + Legacy-Fallback stecken in `imd_typ_beitrag`).
        #
        # Sobald Speicher-Monatsdaten existieren, ist auch 0 kWh ein Ergebnis
        # („nichts aus dem Netz geladen", Rainer-PN 2026-07-25). Nur ganz ohne
        # Zeile bleibt es None = „keine Daten" und die Kachel aus.
        if mf_speicher is not None and "speicher" in typen_mit_zeile:
            speicher_ladung_netz = round(mf_speicher.netzladung_kwh, 2)
        speicher_imd_ladepreis = (
            mf_speicher.netzladung_preis_cent if mf_speicher is not None else None
        )

        # Wirkungsgrad und Vollzyklen
        sl = speicher_ladung or 0
        se = speicher_entladung or 0

        # F-22 (Rainer-PN 2026-08-08, seine ZWEITE Meldung nach 2026-05-22):
        # Der Monats-η läuft über den SoT-Kanon, der die SoC-Drift
        # HERAUSRECHNET, statt sie nur zu erkennen und den Wert zu verwerfen.
        #
        # Was hier bis v4.0.11 stand, war ein Alles-oder-Nichts-Schalter auf
        # |ΔSoC| > 20 pp — und der lag in drei Richtungen falsch (gemessen an
        # der Demo-Anlage, 27 Monate):
        #   * über der Schwelle wurde ausgeblendet, obwohl ein guter Wert
        #     ermittelbar war (2025-11: „—" statt 81,6 %),
        #   * unter der Schwelle stand der ROHE Quotient (2025-10: 83,1 statt
        #     korrekt 82,4 %) — korrigiert wurde also NIE,
        #   * fehlten die SoC-Randwerte, blieb das Flag False und der Wert ging
        #     ungeprüft raus — genau der Pfad zu den >100 %, die Rainer meldete.
        #
        # `berechne_ist_wirkungsgrad` löst alle drei: es rechnet ΔSoC heraus
        # (Energieerhaltung), klemmt auf physikalisch mögliche 100 % und sagt
        # über `quelle`, worauf das Ergebnis beruht. Ausgeblendet wird nur noch,
        # was wirklich nicht ermittelbar ist — und dann MIT Grund (P4).
        if sl > 0 and se > 0:
            from calendar import monthrange
            from backend.core.investition_kennwerte import (
                get_speicher_nutzbare_kapazitaet_kwh,
            )
            from backend.services.speicher_wirtschaftlichkeit import (
                berechne_ist_wirkungsgrad,
            )
            try:
                monat_start = date(jahr, monat, 1)
                monat_ende = date(jahr, monat, monthrange(jahr, monat)[1])
                nutzbar = sum(
                    get_speicher_nutzbare_kapazitaet_kwh(i) or 0 for i in speicher_invs
                )
                _eta = await berechne_ist_wirkungsgrad(
                    db,
                    anlage_id=anlage_id,
                    von=monat_start,
                    bis=monat_ende,
                    ladung_kwh=sl,
                    entladung_kwh=se,
                    nutzbare_kapazitaet_kwh=float(nutzbar),
                    # Ein Kalendermonat — nie das lange Fenster, immer der
                    # SoC-korrigierte Pfad, sofern Randwerte vorliegen.
                    fenster_monate=1,
                )
                speicher_wirkungsgrad_quelle = _eta.quelle
                if _eta.wirkungsgrad_prozent is not None:
                    speicher_wirkungsgrad = round(_eta.wirkungsgrad_prozent, 1)
                else:
                    # Kein SoC am Periodenrand (kein SoC-Sensor, frisch
                    # installiert, Lücke in den Tagesprofilen). Hier NICHT
                    # schweigen: der rohe Quotient ist unkorrigiert, aber
                    # solange er physikalisch möglich ist, ist er eine Aussage —
                    # und für die meisten Anlagen die einzige, die es gibt.
                    # P4 heißt „sagen, was man weiß und wie sicher", nicht
                    # „lieber gar nichts". Ausgeblendet wird nur, was
                    # NACHWEISLICH falsch ist: über 100 % kann kein Speicher.
                    #
                    # Diese drei Zeilen waren bis zum 17.08.2026 eine wörtliche
                    # Zweitschrift des unkorrigierten SoT-Zweigs — gefunden vom
                    # Deckungs-Prüfer zu N-252, nicht vom Abwesenheits-Grep:
                    # *Cockpit → Monat* galt als „gedeckt", weil es den
                    # SoC-Pfad benutzt, und trug die Regel daneben trotzdem
                    # ein zweites Mal.
                    _roh_eta = berechne_speicher_wirkungsgrad(sl, se, None)
                    if _roh_eta.prozent is not None:
                        speicher_wirkungsgrad = round(_roh_eta.prozent, 1)
                        speicher_wirkungsgrad_quelle = _roh_eta.quelle
                # Rückwärtskompatibel: das alte Flag bleibt im Vertrag, trägt
                # jetzt aber die ehrliche Aussage „kein belastbarer η" statt
                # „SoC ist gedriftet". Clients, die es lesen, blenden weiterhin
                # korrekt aus — nur eben in den richtigen Fällen.
                speicher_soc_drift_flag = speicher_wirkungsgrad is None
            except Exception as e:  # noqa: BLE001
                # Der η darf den Endpoint nicht killen. Bei Fehler KEIN roher
                # Fallback-Quotient — das war der Pfad zu den >100 %.
                speicher_wirkungsgrad_quelle = "nicht-ermittelbar"
                speicher_soc_drift_flag = True
                logger.warning(
                    "aktueller_monat: η-Ermittlung fehlgeschlagen "
                    "(anlage=%s, %s/%s): %s", anlage_id, jahr, monat, e,
                )
        # Vollzyklen = ENTLADUNG ÷ Kapazität über den Layer-SoT (Kanon seit
        # 2026-07-28; vorher stand hier die Ladung `sl`).
        _vz = berechne_vollzyklen(se, speicher_kapazitaet)
        if _vz is not None:
            speicher_vollzyklen = round(_vz, 2)

        # #358 Phase 1: Auslastung = Entladung ÷ (Kapazität × Tage).
        #
        # Im LAUFENDEN Monat zählen nur die abgelaufenen Tage. Sonst stünde am
        # 3. eines Monats eine Auslastung von 10 %, die nichts über den Speicher
        # sagt, sondern über das Datum — ein Quotient aus einem vollen Nenner und
        # einem angefangenen Zähler ist genau der Fall, den die P4-Doktrin
        # unterdrücken oder ehrlich machen will (KONZEPT-UNVOLLSTAENDIGE-WERTE
        # §3). Hier ist er ehrlich zu machen: die Basis wächst mit.
        _basis = auslastungs_basis_kwh(speicher_kapazitaet, fenster.tage)
        if _basis is not None:
            speicher_auslastungs_basis = round(_basis, 1)
            _au = auslastung_prozent(se, _basis)
            if _au is not None:
                speicher_auslastung = round(_au, 1)

        # Etappe C1+C4: stundengewichteter effektiver Netz-Ladepreis für den Monat.
        # Helper liefert immer ein Ergebnis (auch bei dünner Datenlage) — UI
        # entscheidet anhand der `quelle`, ob KPI anzeigen oder nur Param.
        if speicher_ladung_netz is not None and speicher_ladung_netz > 0:
            from calendar import monthrange as _monthrange
            from backend.services.speicher_wirtschaftlichkeit import (
                berechne_effektiver_ladepreis as _berechne_eff_ladepreis,
            )
            try:
                _ende = date(jahr, monat, _monthrange(jahr, monat)[1])
                eff = await _berechne_eff_ladepreis(
                    db, anlage_id=anlage_id, von=date(jahr, monat, 1), bis=_ende,
                )
                # Wert nur zurückgeben, wenn belastbar (dyn-tarif/boersenpreis)
                # ODER zumindest mit Diagnose (datenbasis-zu-duenn). Bei
                # `keine-netzladung`/`keine-tep-daten` bleibt das Feld None.
                if eff.effektiver_ladepreis_cent is not None:
                    speicher_eff_ladepreis = round(eff.effektiver_ladepreis_cent, 2)
                speicher_eff_ladepreis_quelle = eff.quelle
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "aktueller_monat: effektiver-Ladepreis-Lookup fehlgeschlagen "
                    "(anlage=%s, %s/%s): %s", anlage_id, jahr, monat, e,
                )

    # WP: Heizung/Warmwasser-Split (Wärme + bei getrennter Strommessung auch Strom, #191)
    wp_heizung = None
    wp_warmwasser = None
    wp_strom_heizen = None
    wp_strom_warmwasser = None
    wp_modus_heizen = None
    wp_modus_kuehlen = None
    wp_modus_warmwasser = None
    wp_modus_lueften = None
    wp_modus_entfeuchten = None
    wp_nutz_lueften = None
    wp_nutz_entfeuchten = None
    wp_modus_rest = None
    wp_modus_abdeckung = None
    wp_modus_gemessen = None
    wp_modus_bezug = None
    # ⭐ **EINE Lesestelle, zwei Herkünfte** (WK-16i): `_wp_funktion` ist die
    # Monatszeile, wo sie eine Menge trägt, sonst die Faltung über die
    # Geräte-Mengen. Beide tragen dieselben Feldnamen — deshalb steht hier
    # **kein** zweiter Zweig, der dieselben vier Gates noch einmal schreibt.
    if _wp_funktion.heizung_kwh > 0:
        wp_heizung = round(_wp_funktion.heizung_kwh, 2)
    if _wp_funktion.warmwasser_kwh > 0:
        wp_warmwasser = round(_wp_funktion.warmwasser_kwh, 2)
    if _wp_funktion.hat_split:
        # Auch 0-Werte zurückgeben, damit Frontend "getrennt erfasst, aktuell 0"
        # vs. "gar nicht getrennt erfasst" unterscheiden kann.
        wp_strom_heizen = round(_wp_funktion.strom_heizen_kwh, 2)
        wp_strom_warmwasser = round(_wp_funktion.strom_warmwasser_kwh, 2)
    if mf_wp is not None:
        # #263 K-2: derselbe Alles-oder-nichts-Grundsatz für den Modus-Split —
        # ohne erfasste Stunde gibt es keine Aufteilung statt einer 0.
        if mf_wp.hat_modus_split:
            wp_modus_heizen = round(mf_wp.modus_strom_heizen_kwh, 2)
            wp_modus_kuehlen = round(mf_wp.modus_strom_kuehlen_kwh, 2)
            wp_modus_warmwasser = round(mf_wp.modus_strom_warmwasser_kwh, 2)
            wp_modus_lueften = round(mf_wp.modus_strom_lueften_kwh, 2)
            wp_modus_entfeuchten = round(mf_wp.modus_strom_entfeuchten_kwh, 2)
            # R-C: nur mit Zahl (D-Sicht) — `or None` statt einer 0-Zeile.
            wp_nutz_lueften = round(mf_wp.nutzenergie_lueften_kwh, 2) or None
            wp_nutz_entfeuchten = (
                round(mf_wp.nutzenergie_entfeuchten_kwh, 2) or None)
            wp_modus_rest = round(mf_wp.modus_nicht_aufgeteilt_kwh, 2)
            wp_modus_abdeckung = round(mf_wp.modus_abdeckung_h, 1)
            wp_modus_gemessen = mf_wp.modus_gemessen
            # W-17b: die Grundmenge des Balkens, damit er nicht stumm unter
            # einer groesseren Kachel steht.
            wp_modus_bezug = round(mf_wp.modus_strom_bezug_kwh, 2)

    # W-4 (SOLL §4.1): je Funktion eine eigene Zahl. Sie beantwortet, was die
    # Gesamtzahl nicht kann — *warum* eine Anlage dasteht, wie sie dasteht.
    # Warmwasser liegt bauartbedingt niedriger (höhere Zieltemperatur); wer viel
    # Warmwasser macht, hat deshalb eine niedrigere Gesamtzahl, **ohne schlechter
    # zu sein**. Dieselben R2-Sperren, weil dieselbe Layer-Funktion gerufen wird.
    # W-5 (SOLL §4.1): Kältemenge ÷ Kühlstrom. Beide Größen nur gemessen — es
    # gibt keinen Weg, eine Kältemenge abzuleiten. Heißt bewusst NICHT „SEER"
    # (genormte Prüfstandsgröße), sondern „Arbeitszahl Kühlen".
    wp_az_kuehlen = arbeitszahl_kuehlen(
        mf_wp.nutzenergie_kuehlen_kwh if mf_wp is not None else None,
        mf_wp.modus_strom_kuehlen_kwh if mf_wp is not None else None,
        # Kuehlen ist KEINE klimaanlagen-exklusive Funktion: A4 ist eine
        # Luft-Wasser-WP mit Kaeltemengenzaehler, und die `luft_luft`-Bedingung
        # der Betriebsart-Felder ist weich. Deshalb dieselbe je-Funktion-Frage
        # wie oben — nicht „gilt hier ohnehin nicht".
        abgrenzung_verletzt=_wp_abgrenzung_je_funktion["kuehlen"],
    )
    wp_az_funktion = arbeitszahl_je_funktion(
        heizung_kwh=wp_heizung,
        strom_heizen_kwh=wp_strom_heizen,
        warmwasser_kwh=wp_warmwasser,
        strom_warmwasser_kwh=wp_strom_warmwasser,
        # WK-16i: dieselbe Quelle wie die vier Mengen darüber — im laufenden
        # Monat also das Kennzeichen der **beitragenden** Geräte, wie es der
        # Tag seit jeher fragt (`views.py::_wp_getrennte_strommessung_tag`).
        hat_split=_wp_funktion.hat_split,
        # **N-479: der Monat darf jetzt denselben Zeitraum-Grund führen wie der
        # Tag.** Er konnte es nicht, weil seine Summen („gemessen 0" und „kein
        # Zähler" tragen beide 0.0 bei) die Frage nicht mehr beantworteten — und
        # meldete deshalb im Sommer „kein Wärmemengenzähler zugeordnet", obwohl
        # beide Zähler hingen und lieferten (simon42 T89667, dietmar1968). Die
        # Marke kommt aus den Monats-Fakten, je Funktion getrennt: Die Heizwärme
        # kann gemessen sein und die Warmwasser-Wärme nicht.
        # ⚠ **BEIDE Seiten müssen gemessen sein, nicht nur die Wärme** (N-438/S3,
        # von deren Probe gefangen). „In diesem Zeitraum nicht geheizt" ist eine
        # Aussage über das Gerät — sie setzt voraus, dass Zähler **und** Nenner
        # dastanden und null meldeten. Bei gemessener Wärme ohne Heizstrom ist
        # „kein Stromverbrauch erfasst" der genauere Satz und behält Vorrang.
        null_ist_gemessen_heizen=(
            _wp_funktion.heizung_gemessen and _wp_funktion.strom_heizen_gemessen
        ),
        null_ist_gemessen_warmwasser=(
            _wp_funktion.warmwasser_gemessen
            and _wp_funktion.strom_warmwasser_gemessen
        ),
        # N-391: Misst EIN gemeinsamer Wärmemengenzähler beide Funktionen, gibt
        # es die Wärme je Funktion nicht — die Zeile sagt dann den Grund, statt
        # die Gesamtwärme durch den Heizstrom zu teilen (gemessen: 5,0 statt 3,0).
        waerme_ist_gesamt=_wp_funktion.waerme_ist_gesamt,
        waerme_abgeleitet_kwh=wp_waerme_abgeleitet_kwh,
        abgrenzung_verletzt=wp_abgrenzung_verletzt,
        abgrenzung_je_funktion_grund=_wp_abgrenzung_je_funktion,
        # **R-2 (WK-16h, N-499): anlagenweit entsteht eine Funktions-Zahl nur
        # aus den Geräten, die die Achse HABEN.** Die Vereinigung über die
        # beitragenden Geräte steht an einer Stelle (`achsen_der_anlage`) —
        # Monat, Tag und Jahr fragen dieselbe. Trägt die Ausstattung nur eine
        # Wärme-Achse, ist die anlagenweite Gesamtzahl ihre Zahl; eine Schranke
        # geht dabei nicht mit (`als_arbeitszahl` liefert dann `None`).
        achsen=achsen_der_anlage(_wp_kennzahlen_je_geraet),
        gesamt=als_arbeitszahl(wp_arbeitszahl),
    )

    # ── D-Sicht: die Tabelle je Gerät und der EINE Kasten ──────────────────
    #
    # Die Gründe werden **hier** gesammelt, weil erst hier alle vier vorliegen;
    # welche davon in den Kasten gehören, entscheidet die Klasse an der
    # Grund-Konstante (`grund_klasse`), nicht diese Route und erst recht nicht
    # der Client.
    wp_block_geraete = geraete_zeilen(_wp_kennzahlen_je_geraet)
    # R-4: die **Geräte**-Ausstattungsgründe kommen mit in den Kasten, mit dem
    # Namen davor und dedupliziert gegen die anlagenweiten Zeilen darüber.
    wp_block_moeglich = was_noch_moeglich([
        ("Arbeitszahl", wp_arbeitszahl.grund),
        ("Arbeitszahl Heizen", wp_az_funktion.heizen.grund),
        ("Arbeitszahl Warmwasser", wp_az_funktion.warmwasser.grund),
        ("Arbeitszahl Kühlen", wp_az_kuehlen.grund),
    ], wp_block_geraete)

    # E-Mobilität: PV/Netz/Extern-Split + V2H
    emob_pv = get_val("emob_pv_ladung_kwh")
    emob_ladung_netz = None
    emob_ladung_extern = None
    emob_v2h = None

    # C1d: der Netz-Anteil kommt aus dem KANONISCHEN Pool, nicht aus einer
    # Roh-Summe über beide Typen. E-Auto und Wallbox messen denselben Fluss
    # (Vehicle- vs. Loadpoint-Perspektive) — genau die Doppelzählung, die
    # `typ_aggregation` oben schon bewusst vermeidet. `emob_pv` daneben stammt
    # aus derselben Trias (`get_val` → Fakten-Zweig), beide passen jetzt
    # zusammen statt aus zwei Rechnungen zu kommen.
    if mf_emob is not None:
        if mf_emob.ladung_netz_kwh > 0:
            emob_ladung_netz = round(mf_emob.ladung_netz_kwh, 2)
        if mf_emob.extern_kwh > 0:
            emob_ladung_extern = round(mf_emob.extern_kwh, 2)
        if mf_emob.v2h_entladung_kwh > 0:
            emob_v2h = round(mf_emob.v2h_entladung_kwh, 2)

    # BKW: Eigenverbrauch
    bkw_eigenverbrauch = None
    if mf_bkw is not None and mf_bkw.eigenverbrauch_gemessen_kwh > 0:
        bkw_eigenverbrauch = round(mf_bkw.eigenverbrauch_gemessen_kwh, 2)

    # Sonstiges: erzeuger + verbraucher aggregieren
    sonstiges_erzeugung = None
    sonstiges_eigenverbrauch = None
    sonstiges_einspeisung = None
    sonstiges_verbrauch = None
    sonstiges_bezug_pv = None
    sonstiges_bezug_netz = None

    sonstiges_geraete: list[SonstigesGeraet] = []
    if mf_sonstiges is not None:
        if mf_sonstiges.erzeugung_kwh > 0:
            sonstiges_erzeugung = round(mf_sonstiges.erzeugung_kwh, 2)
        if mf_sonstiges.eigenverbrauch_kwh > 0:
            sonstiges_eigenverbrauch = round(mf_sonstiges.eigenverbrauch_kwh, 2)
        if mf_sonstiges.einspeisung_kwh > 0:
            sonstiges_einspeisung = round(mf_sonstiges.einspeisung_kwh, 2)
        if mf_sonstiges.verbrauch_kwh > 0:
            sonstiges_verbrauch = round(mf_sonstiges.verbrauch_kwh, 2)
        if mf_sonstiges.bezug_pv_kwh > 0:
            sonstiges_bezug_pv = round(mf_sonstiges.bezug_pv_kwh, 2)
        if mf_sonstiges.bezug_netz_kwh > 0:
            sonstiges_bezug_netz = round(mf_sonstiges.bezug_netz_kwh, 2)

        # Pro-Gerät-Liste in Investitions-Reihenfolge. Die MENGEN kommen aus
        # `je_geraet` (dort schon kategorie-bewusst und laufzeitgefiltert),
        # Bezeichnung und Kategorie aus der Investition — die Anzeige-Form
        # bleibt hier, die Auflösung nicht mehr.
        def _v(x: float) -> Optional[float]:
            return round(x, 2) if x > 0 else None
        for inv in investitionen:
            if inv.typ != "sonstiges":
                continue
            g = mf_sonstiges.je_geraet.get(inv.id)
            if g is None:
                continue
            # N-250: Richtung ohne gepflegte Kategorie aus dem WERT, nicht aus
            # einem Default. Hier stand `.get("kategorie", "erzeuger")` — als
            # einzige von vier Stellen im Baum: die beiden Tages-Schreibpfade
            # (`snapshot/komponenten_beitraege`, `live_sensor_config`) und der
            # Tages-Layer (`core/berechnungen/energie.sonstiges_kwh_je_richtung`)
            # lesen eine leere Kategorie als *Verbraucher*, und `monats_fakten`
            # nimmt ohne sie **beide** Felder mit. Ein ungepflegtes Gerät fiel
            # deshalb in den Erzeuger-Zweig, scheiterte dort an `erzeugung > 0`
            # und war unsichtbar — während seine Zahlen in den Summen darüber
            # mitliefen. Gemeldet an einem Gerät mit Verbrauch (rapahl, 08/2026).
            # Ein *gepflegtes* Gerät ändert sich durch diese Zeile nicht.
            kat = sonstiges_richtung(
                (inv.parameter or {}).get("kategorie"),
                hat_erzeugung=g.erzeugung_kwh > 0,
            )
            if kat == "abgabe":
                if g.abgabe_kwh > 0 or g.einspeise_erloes_euro > 0:
                    sonstiges_geraete.append(SonstigesGeraet(
                        bezeichnung=inv.bezeichnung, kategorie="abgabe",
                        abgabe_kwh=_v(g.abgabe_kwh),
                        erloes_euro=_v(g.einspeise_erloes_euro),
                    ))
            elif kat == "verbraucher":
                if g.verbrauch_kwh > 0 or g.bezug_pv_kwh > 0 or g.bezug_netz_kwh > 0:
                    sonstiges_geraete.append(SonstigesGeraet(
                        bezeichnung=inv.bezeichnung, kategorie="verbraucher",
                        verbrauch_kwh=_v(g.verbrauch_kwh),
                        bezug_pv_kwh=_v(g.bezug_pv_kwh),
                        bezug_netz_kwh=_v(g.bezug_netz_kwh),
                    ))
            else:
                if g.erzeugung_kwh > 0:
                    sonstiges_geraete.append(SonstigesGeraet(
                        bezeichnung=inv.bezeichnung, kategorie="erzeuger",
                        erzeugung_kwh=_v(g.erzeugung_kwh),
                        eigenverbrauch_kwh=_v(g.eigenverbrauch_kwh),
                        einspeisung_kwh=_v(g.einspeisung_kwh),
                    ))

    # (`netzbezug_durchschnittspreis` wird oben mit der Monatszeile geladen —
    #  er geht in die Netzbezugskosten ein und muss dort schon vorliegen.)

    # R15-1 (Rainer-Kostenkacheln): Kosten der Speicher-Netzladung.
    # Preis-Kette TEP-effektiv → IMD-Ø (Handeingabe) → Bezugspreis
    # (Ø-Monatspreis vor festem Tarif) — Berechnungs-Layer-Helper.
    netzladung_kosten = berechne_netzladung_kosten(
        speicher_ladung_netz,
        eff_ladepreis_cent=speicher_eff_ladepreis,
        imd_preis_cent=speicher_imd_ladepreis,
        netzbezug_preis_cent=netzbezug_preis_effektiv_cent,
    )

    # ── Komponenten-Flags ──
    # #239 detLAN: pro Monat filtern, nicht pro Anlage. Sonst wird die
    # Sektion (z.B. Wärmepumpe) im Monatsbericht angezeigt, bevor die
    # Investition angeschafft wurde — alle Werte sind dann "—", was den
    # User irritiert ("Investition vor Anschaffung darstellen" #236 P2).
    hat_speicher = any(
        i.typ == "speicher" and i.ist_aktiv_im_monat(jahr, monat)
        for i in investitionen
    )
    hat_waermepumpe = any(
        i.typ == "waermepumpe" and i.ist_aktiv_im_monat(jahr, monat)
        for i in investitionen
    )

    # ── Issue #169/#238: WP-Counter pro Monat aus TagesZusammenfassung ──
    # Quelle: TagesZusammenfassung.komponenten_starts (JSON, Form
    # {"wp_starts_anzahl": {"<inv_id>": <int>}, "wp_betriebsstunden": {...}}). Pro
    # Tag des Monats werden die Werte aller WP-Investitionen summiert (= Tagessumme
    # der Anlage), daraus max(Tagessumme) und Σ(Tagessumme im Monat). Zeigt was EEDC
    # erfasst hat — Drift gegenüber dem Hersteller-Counter (Cockpit) wird im
    # Daten-Checker ausgewiesen, nicht hier verrechnet. Starts = int, Stunden = float.
    wp_starts_max_tag: Optional[int] = None
    wp_starts_summe_monat: Optional[int] = None
    wp_betriebsstunden_max_tag: Optional[float] = None
    wp_betriebsstunden_summe_monat: Optional[float] = None
    if hat_waermepumpe:
        from backend.models.tages_energie_profil import TagesZusammenfassung
        from sqlalchemy import extract
        wp_invs = [
            i for i in investitionen
            if i.typ == "waermepumpe" and i.ist_aktiv_im_monat(jahr, monat)
        ]
        wp_inv_id_strs = {str(i.id) for i in wp_invs}
        tz_result = await db.execute(
            select(TagesZusammenfassung.komponenten_starts)
            .where(TagesZusammenfassung.anlage_id == anlage_id)
            .where(extract("year", TagesZusammenfassung.datum) == jahr)
            .where(extract("month", TagesZusammenfassung.datum) == monat)
            .where(TagesZusammenfassung.komponenten_starts.is_not(None))
        )

        def _tagessumme(komp: dict | None, feld: str) -> float:
            """Summe eines Counter-Felds über alle aktiven WP-Investitionen an einem Tag."""
            feld_map = (komp or {}).get(feld) or {}
            tag_sum = 0.0
            for inv_id_str, wert in feld_map.items():
                if inv_id_str in wp_inv_id_strs and isinstance(wert, (int, float)) and wert > 0:
                    tag_sum += float(wert)
            return tag_sum

        starts_tagessummen: list[int] = []
        stunden_tagessummen: list[float] = []
        for (komp_starts,) in tz_result.all():
            s = _tagessumme(komp_starts, "wp_starts_anzahl")
            if s > 0:
                starts_tagessummen.append(int(s))
            h = _tagessumme(komp_starts, "wp_betriebsstunden")
            if h > 0:
                stunden_tagessummen.append(h)
        if starts_tagessummen:
            wp_starts_max_tag = max(starts_tagessummen)
            wp_starts_summe_monat = sum(starts_tagessummen)
        if stunden_tagessummen:
            wp_betriebsstunden_max_tag = round(max(stunden_tagessummen), 1)
            wp_betriebsstunden_summe_monat = round(sum(stunden_tagessummen), 1)


    hat_emobilitaet = any(
        i.typ in ("e-auto", "wallbox")
        and not ist_dienstlich(i)
        and i.ist_aktiv_im_monat(jahr, monat)
        for i in investitionen
    )
    hat_balkonkraftwerk = any(
        i.typ == "balkonkraftwerk" and i.ist_aktiv_im_monat(jahr, monat)
        for i in investitionen
    )
    hat_sonstiges = any(
        i.typ == "sonstiges" and i.ist_aktiv_im_monat(jahr, monat)
        for i in investitionen
    )

    # ── Vergleichsdaten ──
    vorjahr = await _load_vorjahr(anlage_id, investitionen, jahr, monat, db)
    soll_pv = await _load_soll_pv(anlage_id, jahr, monat, db, fenster)

    # ── Grundlast (Nacht-Sockel, R12-1: ersetzt PVGIS-SOLL/IST in Cockpit/Monat
    # + Jahr; Formel im Berechnungs-Layer, Median wie der Live-Wert). Im aktuellen
    # Monat nur die bisherigen Tage hochrechnen, sonst alle Kalendertage. ──
    grundlast_tage = fenster.tage
    grundlast = berechne_grundlast(
        nacht_verbrauch_kw=await _load_grundlast_nacht_kw(anlage_id, jahr, monat, db),
        gesamtverbrauch_kwh=gesamtverbrauch,
        tage=grundlast_tage,
    )

    # ── Quellen-Übersicht ──
    # `tagesebene` steht **als eigene Marke** daneben und wird nicht unter
    # „gespeichert" verbucht (N-472): Sie ist nicht gepflegt, sondern abgeleitet
    # — wer sie für einen Monatsabschluss hält, sucht eine Zeile, die es nicht
    # gibt.
    quellen = {
        "ha_statistics": bool(ha_stats),
        "mqtt_inbound": bool(mqtt_energy) if ist_aktueller_monat else False,
        "connector": bool(connector),
        "gespeichert": bool(saved),
        "tagesebene": bool(tagesebene),
    }

    # ── Grund statt Leere (N-472) ──
    # Die Regel und der Wortlaut stehen in `core/monatswert_grund.py`; hier wird
    # nur die eine Frage beantwortet, die diese Route beantworten kann: Hat für
    # diesen Monat überhaupt irgendeine Quelle irgendetwas geliefert?
    _grund = monatswert_grund_text(monatswert_grund(bool(resolved)))
    datenlage_gruende: dict[str, str] = (
        {
            feld: _grund
            for feld in ("pv_erzeugung_kwh", "einspeisung_kwh", "netzbezug_kwh")
            if get_val(feld) is None
        }
        if _grund else {}
    )

    # ── Feld-Quellen extrahieren ──
    feld_quellen = {
        feld: info
        for feld, (_, info) in resolved.items()
        if not feld.startswith("inv_")  # Investitions-Detail-Felder ausblenden
    }

    # ── Aktive Geräte je Typ (Namen) für die „aggregiert aus …"-Hinweise ──
    komponenten_geraete: dict[str, list[str]] = {}
    for _inv in investitionen:
        if _inv.ist_aktiv_im_monat(jahr, monat):
            komponenten_geraete.setdefault(_inv.typ, []).append(_inv.bezeichnung)

    # ── t_konto_je_investition (Vorlage 2: Abschnitt in finanzen.py, Schnittstelle 12 ein / 2 aus) ──
    _out = await t_konto_je_investition(_zt_cache=_zt_cache, allgemein_tarif=allgemein_tarif, anlage_id=anlage_id, db=db, einspeise_cent=einspeise_cent, investitionen=investitionen, jahr=jahr, monat=monat, monats_benzinpreis=monats_benzinpreis, monats_gaspreis=monats_gaspreis, netzbezug_preis_effektiv_cent=netzbezug_preis_effektiv_cent, tarife=tarife)
    if "investitionen_financials" in _out: investitionen_financials = _out["investitionen_financials"]
    if "speicher_ersparnis" in _out: speicher_ersparnis = _out["speicher_ersparnis"]
    # ── emob_aggregat_und_kennzahlen (Vorlage 2: Abschnitt in finanzen.py, Schnittstelle 12 ein / 4 aus) ──
    _out = emob_aggregat_und_kennzahlen(anlage=anlage, einspeise_erloes=einspeise_erloes, emob_ersparnis=emob_ersparnis, ev_ersparnis=ev_ersparnis, get_val=get_val, investitionen=investitionen, investitionen_financials=investitionen_financials, jahr=jahr, monat=monat, netzbezug_kosten=netzbezug_kosten, pv=pv, wp_ersparnis=wp_ersparnis)
    if "emob_eff" in _out: emob_eff = _out["emob_eff"]
    if "emob_ersparnis" in _out: emob_ersparnis = _out["emob_ersparnis"]
    if "gesamtnettoertrag" in _out: gesamtnettoertrag = _out["gesamtnettoertrag"]
    if "spez_ertrag" in _out: spez_ertrag = _out["spez_ertrag"]
    # ── Antwort ──
    return AktuellerMonatResponse(
        anlage_id=anlage.id,
        anlage_name=anlage.anlagenname,
        jahr=jahr,
        monat=monat,
        monat_name=MONAT_NAMEN[monat],
        aktualisiert_um=now.isoformat(),
        quellen=quellen,
        hinweise=hinweise,
        datenlage_gruende=datenlage_gruende,
        # Energie
        pv_erzeugung_kwh=pv,
        einspeisung_kwh=einspeisung,
        netzbezug_kwh=netzbezug,
        eigenverbrauch_kwh=eigenverbrauch,
        direktverbrauch_kwh=direktverbrauch,
        gesamtverbrauch_kwh=gesamtverbrauch,
        autarkie_prozent=autarkie,
        eigenverbrauch_quote_prozent=ev_quote,
        spez_ertrag=round(spez_ertrag, 1) if spez_ertrag is not None else None,
        # Komponenten — Speicher
        speicher_ladung_kwh=speicher_ladung,
        speicher_entladung_kwh=speicher_entladung,
        speicher_ladung_netz_kwh=speicher_ladung_netz,
        speicher_wirkungsgrad_prozent=speicher_wirkungsgrad,
        speicher_wirkungsgrad_quelle=speicher_wirkungsgrad_quelle,
        speicher_vollzyklen=speicher_vollzyklen,
        speicher_kapazitaet_kwh=speicher_kapazitaet,
        speicher_soc_drift_signifikant=speicher_soc_drift_flag,
        speicher_auslastungs_basis_kwh=speicher_auslastungs_basis,
        speicher_auslastung_prozent=speicher_auslastung,
        speicher_ersparnis_euro=speicher_ersparnis,
        speicher_effektiver_ladepreis_cent=speicher_eff_ladepreis,
        speicher_effektiver_ladepreis_quelle=speicher_eff_ladepreis_quelle,
        speicher_ladung_netz_kosten_euro=netzladung_kosten.kosten_euro if netzladung_kosten else None,
        speicher_ladung_netz_preis_cent=netzladung_kosten.preis_cent if netzladung_kosten else None,
        speicher_ladung_netz_preis_quelle=netzladung_kosten.quelle if netzladung_kosten else None,
        hat_speicher=hat_speicher,
        # Komponenten — WP
        wp_strom_kwh=get_val("wp_strom_kwh"),
        wp_waerme_kwh=get_val("wp_waerme_kwh"),
        wp_heizung_kwh=wp_heizung,
        wp_warmwasser_kwh=wp_warmwasser,
        wp_jaz=wp_arbeitszahl.wert,
        wp_jaz_grund=wp_arbeitszahl.grund,
        wp_jaz_hinweis=wp_arbeitszahl.hinweis,
        wp_jaz_zaehler_kwh=wp_arbeitszahl.zaehler_kwh,
        wp_jaz_nenner_kwh=wp_arbeitszahl.nenner_kwh,
        wp_waerme_abgeleitet=wp_waerme_abgeleitet_kwh > 0,
        # Alle Gründe des Blocks in EINE Frage — der Link ist ein Element des
        # Blocks, keine Zeile je Kennzahl.
        wp_hub_hilft=hub_hilft(
            wp_arbeitszahl.grund,
            wp_az_funktion.heizen.grund,
            wp_az_funktion.warmwasser.grund,
            wp_az_kuehlen.grund,
            ist_schranke=wp_arbeitszahl.ist_schranke,
        ),
        # Die Menge nur, wo es überhaupt Wärme gibt — sonst stünde eine 0
        # neben einem „—" und sähe aus wie „nichts gerechnet" statt „nichts
        # gemessen". Gleiche Rundung wie `wp_waerme_kwh` daneben.
        wp_waerme_abgeleitet_kwh=(
            round(wp_waerme_abgeleitet_kwh, 2) if wp_waerme is not None else None
        ),
        wp_waerme_herkunft=wp_waerme_herkunft,
        wp_ersparnis_vorbehalt=wp_ersparnis_vorbehalt,
        wp_ersparnis_berechnung=wp_ersparnis_berechnung_text,
        wp_strom_heizen_kwh=wp_strom_heizen,
        wp_strom_warmwasser_kwh=wp_strom_warmwasser,
        wp_modus_strom_heizen_kwh=wp_modus_heizen,
        wp_modus_strom_kuehlen_kwh=wp_modus_kuehlen,
        wp_modus_strom_warmwasser_kwh=wp_modus_warmwasser,
        wp_jaz_heizen=wp_az_funktion.heizen.wert,
        wp_jaz_heizen_grund=wp_az_funktion.heizen.grund,
        wp_jaz_warmwasser=wp_az_funktion.warmwasser.wert,
        wp_jaz_warmwasser_grund=wp_az_funktion.warmwasser.grund,
        wp_jaz_kuehlen=wp_az_kuehlen.wert,
        wp_jaz_kuehlen_grund=wp_az_kuehlen.grund,
        wp_jaz_ist_schranke=wp_arbeitszahl.ist_schranke,
        wp_jaz_schranke_hinweis=wp_arbeitszahl.schranke_hinweis,
        wp_geraete=wp_block_geraete,
        wp_moeglich=wp_block_moeglich,
        wp_kaelte_kwh=(
            round(mf_wp.nutzenergie_kuehlen_kwh, 2)
            if mf_wp is not None and mf_wp.nutzenergie_kuehlen_kwh > 0 else None
        ),
        wp_modus_strom_lueften_kwh=wp_modus_lueften,
        wp_modus_strom_entfeuchten_kwh=wp_modus_entfeuchten,
        wp_modus_nutzenergie_lueften_kwh=wp_nutz_lueften,
        wp_modus_nutzenergie_entfeuchten_kwh=wp_nutz_entfeuchten,
        wp_modus_nicht_aufgeteilt_kwh=wp_modus_rest,
        wp_modus_abdeckung_h=wp_modus_abdeckung,
        wp_modus_strom_bezug_kwh=wp_modus_bezug,
        wp_modus_gemessen=wp_modus_gemessen,
        wp_starts_max_tag=wp_starts_max_tag,
        wp_starts_summe_monat=wp_starts_summe_monat,
        wp_betriebsstunden_max_tag=wp_betriebsstunden_max_tag,
        wp_betriebsstunden_summe_monat=wp_betriebsstunden_summe_monat,
        hat_waermepumpe=hat_waermepumpe,
        # Komponenten — E-Mobilität
        emob_ladung_kwh=get_val("emob_ladung_kwh"),
        emob_km=get_val("emob_km"),
        emob_verbrauch_100km=round(emob_eff.wert, 1) if emob_eff.wert is not None else None,
        emob_verbrauch_quelle=emob_eff.quelle,
        emob_ladung_pv_kwh=emob_pv if emob_pv else None,
        emob_ladung_netz_kwh=emob_ladung_netz,
        emob_ladung_extern_kwh=emob_ladung_extern,
        emob_v2h_kwh=emob_v2h,
        hat_emobilitaet=hat_emobilitaet,
        # Komponenten — BKW
        bkw_erzeugung_kwh=get_val("bkw_erzeugung_kwh"),
        bkw_eigenverbrauch_kwh=bkw_eigenverbrauch,
        hat_balkonkraftwerk=hat_balkonkraftwerk,
        # Komponenten — Sonstiges
        sonstiges_erzeugung_kwh=sonstiges_erzeugung,
        abgabe_dritte_kwh=round(abgabe_dritte, 2) if abgabe_dritte > 0 else None,
        sonstiges_eigenverbrauch_kwh=sonstiges_eigenverbrauch,
        sonstiges_einspeisung_kwh=sonstiges_einspeisung,
        sonstiges_verbrauch_kwh=sonstiges_verbrauch,
        sonstiges_bezug_pv_kwh=sonstiges_bezug_pv,
        sonstiges_bezug_netz_kwh=sonstiges_bezug_netz,
        sonstiges_geraete=sonstiges_geraete,
        hat_sonstiges=hat_sonstiges,
        # Finanzen
        einspeise_erloes_euro=einspeise_erloes,
        einspeisung_neg_preis_kwh=einspeisung_neg_preis,
        nicht_vergueteter_erloes_euro=nicht_vergueteter_erloes,
        netzbezug_kosten_euro=netzbezug_kosten,
        netzbezug_arbeitspreis_kosten_euro=netzbezug_arbeitspreis_kosten,
        ev_ersparnis_euro=ev_ersparnis,
        netto_ertrag_euro=netto_ertrag,
        wp_ersparnis_euro=wp_ersparnis,
        emob_ersparnis_euro=emob_ersparnis,
        sonstige_ertraege_euro=sonstige_ertraege_total,
        sonstige_ausgaben_euro=sonstige_ausgaben_total,
        sonstige_netto_euro=sonstige_netto_total,
        anlage_sonstige_ertraege_euro=anlage_sonstige_ertraege,
        anlage_sonstige_ausgaben_euro=anlage_sonstige_ausgaben,
        gesamtnettoertrag_euro=gesamtnettoertrag,
        betriebskosten_anteilig_euro=betriebskosten_anteilig,
        betriebskosten_anteilig_jahr_euro=betriebskosten_anteilig_jahr,
        betriebskosten_anteilig_anzahl=betriebskosten_anteilig_anzahl,
        # Tarif-Info
        netzbezug_preis_cent=netzbezug_preis_cent if allgemein_tarif else None,
        # Der Wert zu `_herkunft`/`_abdeckung` — ohne ihn beschreiben die beiden
        # eine Zahl, die die Antwort nicht enthält (SOLL Flex-Tarife H-2).
        netzbezug_preis_effektiv_cent=netzbezug_preis_effektiv_cent,
        netzbezug_preis_herkunft=netzbezug_preis_herkunft,
        netzbezug_preis_abdeckung=netzbezug_preis_abdeckung,
        # N-267: sagt der Anzeige, dass der Preis daneben gewichtet ist.
        netzbezug_preis_zeittarif=hat_zeitfenster(allgemein_tarif),
        einspeise_preis_cent=einspeise_cent if allgemein_tarif else None,
        netzbezug_durchschnittspreis_cent=netzbezug_durchschnittspreis,
        grundgebuehr_euro=grundgebuehr,
        zaehlergebuehr_euro_jahr=zaehlergebuehr_jahr,
        # Vergleiche
        vorjahr=vorjahr,
        soll_pv_kwh=soll_pv.anteilig,
        soll_pv_tage=fenster.tage if soll_pv.anteilig is not None else None,
        soll_pv_tage_gesamt=fenster.tage_gesamt if soll_pv.anteilig is not None else None,
        soll_pv_kwh_monat=soll_pv.monat,
        grundlast_kw=grundlast.grundlast_kw,
        grundlast_kwh=grundlast.grundlast_kwh,
        grundlast_anteil_prozent=grundlast.grundlast_anteil_prozent,
        # Per-Investition Finanzdetails
        investitionen_financials=investitionen_financials,
        komponenten_geraete=komponenten_geraete,
        # Quellen
        feld_quellen=feld_quellen,
    )
