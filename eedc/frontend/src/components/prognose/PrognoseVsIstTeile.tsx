/**
 * PrognoseVsIst — geteilte Element-Bausteine (A.5 Sub 4, Block ①).
 *
 * Eine Code-Wahrheit: die IST-Seite `pages/PrognoseVsIst.tsx` UND die v4-Sicht
 * `v4/AuswertungenPrognoseV4.tsx` komponieren aus diesen Teilen. Jeder Teil ist ein
 * eigenständiges Anzeige-Element (v4 umhüllt es mit `Parkbar`; IST rendert es direkt).
 * Zahlen/Einheiten via `lib/einheiten.ts` (R1/R2: kWh→MWh ab 1.000, Tausenderpunkt,
 * pro KPI-Strip eine gemeinsame Einheit über den Referenzwert); Farben aus colors.ts.
 */
import { useState, useEffect, useCallback } from 'react'
import { TrendingUp, TrendingDown, Download } from 'lucide-react'
import {
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
  ComposedChart, Line, ReferenceLine, Bar,
} from 'recharts'
import { Card, Button, ChartLegende, Table, TableHead, TableBody, TableFoot } from '../ui'
import { ZELLE, KOPF_ZELLE } from '../ui/tabelleMasse'
import ChartTooltip from '../ui/ChartTooltip'
import type { KpiStripItem } from '../blocks'
import { pvgisApi, monatsdatenApi } from '../../api'
import type { PVModulPrognose, SollJeMonatResponse } from '../../api/pvgis'
import type { AggregierteMonatsdaten } from '../../api/monatsdaten'
import { SOLL_IST_COLORS, formatEnergie, energieAchse, formatProzent, xAchse, yAchse, achsenEinheit, achsenTick, ACHSEN_MARGIN_TOP } from '../../lib'
import { useLegendenToggle, useSchmaleAchse } from '../../hooks'
import { useChartTheme } from '../../context/ThemeContext'

const monatNamen = ['', 'Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

interface PrognoseData {
  jahresertrag_kwh: number
  monatswerte: Array<{ monat: number; e_m: number }>
  isLive?: boolean
  module?: PVModulPrognose[]
}

export interface VergleichsDaten {
  monat: number
  monatName: string
  /** Der SOLL dieses Monats; 0, wenn es keinen gibt — dann ist `hatSoll` false. */
  prognose: number
  ist: number
  /** Gibt es für diesen Monat überhaupt einen Maßstab? False heißt „kein
   *  Erzeuger in diesem Monat" (Jahr vor der Anlage) — nicht „0 kWh erwartet". */
  hatSoll: boolean
  /** Trägt dieser Monat eine PV-**Messung**? Entscheidet, ob sein SOLL in den
   *  Nenner der Jahres-Abweichung zählt. Bewusst Daten-Anwesenheit statt
   *  `ist !== 0`: ein gemessener Null-Monat ist ein Messwert. */
  hatDaten: boolean
  /** `null`, wenn es keinen Maßstab gibt — nicht 0. */
  abweichung: number | null
  abweichungProzent: number | null
}

export interface PrognoseVsIstVM {
  loading: boolean
  error: string | null
  prognose: PrognoseData | null
  monatsdaten: AggregierteMonatsdaten[]
  verfuegbareJahre: number[]
  vergleichsDaten: VergleichsDaten[]
  /** Das **volle** Jahres-SOLL — die KPI „PVGIS Prognose" heißt „Jahr 2026"
   *  und meint auch das Jahr. Nicht der Nenner der Abweichung. */
  jahresPrognose: number
  /** Das SOLL der Monate **mit Messung** — der Nenner der Abweichung. Im
   *  abgeschlossenen Jahr identisch mit {@link jahresPrognose}. */
  periodenPrognose: number
  jahresIst: number
  /** `null`, wenn kein Monat vergleichbar ist (keine Messung ∨ kein Maßstab).
   *  Bewusst nicht 0 — s. `lib/sollErfuellung.ts`. */
  jahresAbweichung: number | null
  jahresAbweichungProzent: number | null
  monateMitDaten: number
  hochgerechneterJahresIst: number
  saving: boolean
  reload: () => void
  save: () => Promise<void>
}

/** Lädt PVGIS-Prognose (gespeichert ∨ live) + Monatsdaten und berechnet den
 *  Jahres-SOLL/IST-Vergleich für ein konkretes Jahr. Geteilt von IST + v4.
 *  `vorgeladeneMonatsdaten`: bereits geladene aggregierte Monatsdaten (alle
 *  Jahre) — der V4-Dispatcher hält sie in `useAuswertungBasis`; wenn übergeben
 *  (auch leer!), entfällt der eigene listAggregiert-Fetch (Doppel-Fetch).
 *  ⚠️ Muss render-stabil sein (State/useMemo, ist Effekt-Dependency) — ein
 *  Inline-Literal erzeugt eine Refetch-Schleife. */
export function usePrognoseVsIst(
  anlageId: number | null | undefined,
  jahr: number | undefined,
  vorgeladeneMonatsdaten?: AggregierteMonatsdaten[],
): PrognoseVsIstVM {
  const [prognose, setPrognose] = useState<PrognoseData | null>(null)
  const [monatsdaten, setMonatsdaten] = useState<AggregierteMonatsdaten[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [soll, setSoll] = useState<SollJeMonatResponse | null>(null)

  const load = useCallback(async () => {
    if (!anlageId) return
    setLoading(true)
    setError(null)
    try {
      // `!== undefined` bewusst: ein leeres Array gilt als vorgeladen (Basis
      // fertig geladen, ehrlich leer) — kein Fallback-Fetch.
      const md = vorgeladeneMonatsdaten !== undefined
        ? vorgeladeneMonatsdaten
        : await monatsdatenApi.listAggregiert(anlageId)
      setMonatsdaten(md)
      const gespeichert = await pvgisApi.getAktivePrognose(anlageId)
      if (gespeichert) {
        setPrognose({ jahresertrag_kwh: gespeichert.jahresertrag_kwh, monatswerte: gespeichert.monatswerte, isLive: false })
        // Der gekürzte Maßstab gibt es nur zur GESPEICHERTEN Prognose — das
        // Backend wählt die aktive (P5). Zur Live-Prognose kennt es keine, dann
        // bleibt `soll` null und die Rechnung fällt auf die Rohwerte zurück.
        setSoll(jahr != null ? await pvgisApi.getSollJeMonat(anlageId, jahr) : null)
      } else {
        setSoll(null)
        try {
          const live = await pvgisApi.getPrognose(anlageId)
          setPrognose({
            jahresertrag_kwh: live.jahresertrag_kwh,
            monatswerte: live.monatsdaten.map(m => ({ monat: m.monat, e_m: m.e_m })),
            isLive: true, module: live.module,
          })
        } catch (pvgisError) {
          setPrognose(null)
          if (pvgisError instanceof Error && pvgisError.message.includes('PV-Module')) {
            setError('Keine PV-Module für diese Anlage definiert. Bitte unter Einstellungen → Investitionen anlegen.')
          }
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Fehler beim Laden')
    } finally {
      setLoading(false)
    }
  }, [anlageId, jahr, vorgeladeneMonatsdaten])

  useEffect(() => { void load() }, [load, jahr])

  const save = useCallback(async () => {
    if (!anlageId) return
    setSaving(true)
    try {
      await pvgisApi.speicherePrognose(anlageId)
      const currentModule = prognose?.module
      await load()
      if (currentModule && currentModule.length > 0) {
        setPrognose(prev => prev ? { ...prev, module: currentModule } : null)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Fehler beim Speichern')
    } finally {
      setSaving(false)
    }
  }, [anlageId, prognose, load])

  const verfuegbareJahre = [...new Set(monatsdaten.map(m => m.jahr))].sort((a, b) => b - a)

  // ── Der Maßstab ───────────────────────────────────────────────────────────
  // Zwei Quellen, und die Reihenfolge ist der ganze Punkt:
  //
  // 1. `soll` aus `GET /pvgis/soll/{id}/{jahr}` — je Monat mit den GERÄTE-KANTEN
  //    gekürzt (`services/pvgis_soll.py`). Ein Modul zählt erst ab seiner
  //    Anschaffung, der Anschaffungsmonat tagesanteilig. Das ist der richtige
  //    Maßstab und der Grund, warum hier nichts gerechnet wird.
  // 2. `prognose.monatswerte` — das UNGEKÜRZTE Anlagen-SOLL im heutigen Ausbau.
  //    Nur noch Rückfall für die LIVE-Prognose (noch nicht gespeichert): für die
  //    kennt das Backend keinen Maßstab, weil es keine aktive Prognose gibt.
  //
  // Warum das zählt (Michael, 2026-09-18): an seiner Anlage hingen bis zum
  // 08.09.2026 nur 0,85 kWp von 5,15. Weg 2 stellte Januar–August das SOLL der
  // vollen Anlage gegenüber — 743 kWh gegen 3.977 kWh, „−81 % · Unter Plan" in
  // jedem Monat. Gegen die Leistung, die es in diesen Monaten wirklich gab
  // (656 kWh), liegt dieselbe Anlage bei +13 %.
  const sollJeMonat = new Map<number, number | null>(
    (soll?.monate ?? []).map(m => [m.monat, m.soll_kwh]),
  )

  const vergleichsDaten: VergleichsDaten[] = []
  if ((soll || prognose?.monatswerte) && jahr != null) {
    for (let monat = 1; monat <= 12; monat++) {
      const sollWert = soll
        ? sollJeMonat.get(monat) ?? null
        : prognose?.monatswerte.find(m => m.monat === monat)?.e_m ?? null
      // `!= null` statt truthy (CLAUDE.md „0-Werte prüfen"): ein gemessener
      // Null-Monat trägt seinen SOLL mit, eine Zeile ohne PV-Messung nicht.
      const gemessen = monatsdaten.filter(
        m => m.jahr === jahr && m.monat === monat && m.pv_erzeugung_kwh != null,
      )
      const istWert = gemessen.reduce((sum, m) => sum + (m.pv_erzeugung_kwh ?? 0), 0)
      const abweichung = sollWert != null ? istWert - sollWert : null
      vergleichsDaten.push({
        monat, monatName: monatNamen[monat], prognose: sollWert ?? 0, ist: istWert,
        hatSoll: sollWert != null,
        hatDaten: gemessen.length > 0,
        abweichung,
        abweichungProzent: sollWert != null && sollWert > 0 && abweichung != null
          ? (abweichung / sollWert) * 100
          : null,
      })
    }
  }

  // Das volle Jahr — die Kachel daneben heißt „Jahr 2026" und meint auch das
  // Jahr. Aus dem Backend ist das die datumsbewusste Jahres-ERWARTUNG (bei
  // Michael also inkl. des Strings ab September), nicht die Anlage ganzjährig.
  const jahresPrognose = soll
    ? soll.jahr_kwh ?? 0
    : vergleichsDaten.reduce((s, d) => s + d.prognose, 0)
  const jahresIst = vergleichsDaten.reduce((s, d) => s + d.ist, 0)

  // Zähler angefangen, Nenner vollständig — dieselbe Falle wie N-69 (laufender
  // Monat), nur auf der Jahres-Ebene: acht erfasste Monate gegen zwölf Monate
  // SOLL. Der Weg ist derselbe (Entscheid Gernot, 2026-08-04): den Nenner
  // kürzen. Das erklärt 4,6 der 85,6 Prozentpunkte des gemeldeten Falls; die
  // übrigen trägt der Maßstab oben.
  const bewertbareMonate = vergleichsDaten.filter(d => d.hatDaten && d.hatSoll)
  const periodenPrognose = bewertbareMonate.reduce((s, d) => s + d.prognose, 0)
  // `null` statt 0, wenn nichts zu vergleichen ist: eine 0 läse sich als „exakt
  // im Plan" (grün, +0,0 %), heißt aber „keine einzige Messung" — dieselbe
  // Entscheidung wie in `lib/sollErfuellung.ts` und in der Route selbst.
  const bewertbar = periodenPrognose > 0
  const jahresAbweichung = bewertbar
    ? bewertbareMonate.reduce((s, d) => s + d.ist, 0) - periodenPrognose
    : null
  const jahresAbweichungProzent = bewertbar && jahresAbweichung != null
    ? (jahresAbweichung / periodenPrognose) * 100
    : null
  const monateMitDaten = vergleichsDaten.filter(d => d.hatDaten).length
  // Saisonal hochrechnen, nicht flach: `Σ ÷ n × 12` behandelt einen Januar wie
  // einen Juli. Genau das hat #387 am Gemeinschaftsdatensatz verworfen — dort
  // lagen zwischen beiden Wegen 832,1 und 348,6 kWh/kWp. Der Anteil trägt hier
  // zusätzlich den ZUBAU: Michaels Rest-Jahr hat den String, seine acht
  // gemessenen Monate hatten ihn nicht. Flach gerechnet stünden hier 1.115 kWh
  // („−36 % vs. Prognose") neben einer Abweichung von +13 % — zwei Zahlen zur
  // selben Anlage, die sich widersprechen.
  const hochgerechneterJahresIst = bewertbar
    ? (jahresIst / periodenPrognose) * jahresPrognose
    : monateMitDaten > 0 ? (jahresIst / monateMitDaten) * 12 : 0

  return {
    loading, error, prognose, monatsdaten, verfuegbareJahre, vergleichsDaten,
    jahresPrognose, periodenPrognose, jahresIst, jahresAbweichung, jahresAbweichungProzent,
    monateMitDaten, hochgerechneterJahresIst, saving, reload: () => void load(), save,
  }
}

/** R2: gemeinsame Energie-Einheit über alle KPIs des Strips (größter Wert = Referenz). */
export function pvgisKpiItems(vm: PrognoseVsIstVM, jahr: number | undefined): KpiStripItem[] {
  const abw = vm.jahresAbweichung
  const ref = Math.max(vm.jahresPrognose, vm.jahresIst, Math.abs(abw ?? 0), vm.hochgerechneterJahresIst)
  const e = (v: number) => formatEnergie(v, ref)
  const vzP = (abw ?? 0) >= 0 ? '+' : ''
  const teiljahr = vm.monateMitDaten > 0 && vm.monateMitDaten < 12
  const items: KpiStripItem[] = [
    {
      title: 'PVGIS Prognose', value: e(vm.jahresPrognose).wert, unit: e(vm.jahresPrognose).einheit,
      color: 'yellow', subtitle: jahr != null ? `Jahr ${jahr}` : 'Jahr', parkId: 'kpi:pvgis-prognose',
      formel: 'Σ PVGIS-Monatsprognose', ergebnis: `= ${e(vm.jahresPrognose).text}`,
    },
    {
      title: 'IST-Erzeugung', value: e(vm.jahresIst).wert, unit: e(vm.jahresIst).einheit,
      color: 'green', subtitle: `${vm.monateMitDaten} von 12 Monaten`, parkId: 'kpi:ist-erzeugung',
      formel: 'Σ IST-PV-Erzeugung', ergebnis: `= ${e(vm.jahresIst).text}`,
    },
    // Ohne vergleichbaren Monat trägt die Kachel den Display-Token statt einer
    // 0: „+0 kWh · +0,0 %" in Grün läse sich als „exakt im Plan", heißt aber
    // „keine einzige Messung" (dieselbe Wahl wie `lib/sollErfuellung.ts`).
    abw == null
      ? {
          title: 'Abweichung', value: '—', unit: '',
          color: 'gray', subtitle: 'kein vergleichbarer Monat', parkId: 'kpi:abweichung',
          formel: '(IST − SOLL der erfassten Monate) ÷ SOLL der erfassten Monate',
          ergebnis: '= — (kein Monat trägt SOLL und Messung zugleich)',
        }
      : {
          title: 'Abweichung', value: `${vzP}${e(abw).wert}`, unit: e(abw).einheit,
          color: abw >= 0 ? 'green' : 'red', icon: abw >= 0 ? TrendingUp : TrendingDown,
          // Das Fenster steht dabei, sobald es nicht das ganze Jahr ist — sonst
          // liest sich die Zahl wie eine Jahresbilanz (dieselbe Ansage wie
          // „anteilig · 4 von 31 Tagen" im Monats-SOLL).
          subtitle: `${vzP}${formatProzent(vm.jahresAbweichungProzent ?? 0).text}${teiljahr ? ` · ${vm.monateMitDaten} von 12 Monaten` : ''}`,
          parkId: 'kpi:abweichung',
          formel: teiljahr
            ? '(IST − SOLL der erfassten Monate) ÷ SOLL der erfassten Monate'
            : '(IST − SOLL) ÷ SOLL',
          // A6/N-365: die eingesetzten Werte gehören dazu — der Nenner
          // (`periodenPrognose`) steht auf KEINER Nachbarkachel, die zeigt das
          // volle Jahr. Ohne diese Zeile wäre die Zahl auf der Fläche nicht
          // herleitbar.
          ergebnis: `= (${e(vm.jahresIst).text} − ${e(vm.periodenPrognose).text}) ÷ ${e(vm.periodenPrognose).text} = ${vzP}${formatProzent(vm.jahresAbweichungProzent ?? 0).text}`,
        },
  ]
  if (vm.monateMitDaten < 12 && vm.monateMitDaten > 0) {
    const proz = vm.jahresPrognose > 0 ? (vm.hochgerechneterJahresIst / vm.jahresPrognose - 1) * 100 : 0
    items.push({
      title: 'Hochrechnung Jahr', value: e(vm.hochgerechneterJahresIst).wert, unit: e(vm.hochgerechneterJahresIst).einheit,
      color: 'blue', subtitle: `${proz >= 0 ? '+' : ''}${formatProzent(proz).text} vs. Prognose`, parkId: 'kpi:hochrechnung',
      formel: vm.periodenPrognose > 0
        ? 'IST ÷ SOLL der erfassten Monate × Jahres-SOLL'
        : 'IST ÷ Monate mit Daten × 12',
      ergebnis: vm.periodenPrognose > 0
        ? `= ${e(vm.jahresIst).text} ÷ ${e(vm.periodenPrognose).text} × ${e(vm.jahresPrognose).text} = ${e(vm.hochgerechneterJahresIst).text}`
        : `= ${e(vm.hochgerechneterJahresIst).text}`,
    })
  }
  return items
}

/** Live-Prognose-Hinweis + „Prognose speichern" (nur bei Live-Abruf). */
export function PvgisSpeichern({ vm }: { vm: PrognoseVsIstVM }) {
  if (!vm.prognose?.isLive) return null
  return (
    <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4 flex items-center justify-between gap-4">
      <div className="text-blue-700 dark:text-blue-300">
        <strong>Live-Prognose:</strong> Diese Prognose wurde gerade von PVGIS abgerufen und ist noch nicht gespeichert.
      </div>
      <Button size="sm" onClick={vm.save} disabled={vm.saving}>
        <Download className="h-4 w-4 mr-1" />
        {vm.saving ? 'Speichern…' : 'Speichern'}
      </Button>
    </div>
  )
}

/** Monatlicher SOLL/IST-Vergleich (Balken) + Abweichungs-Linie. */
export function PvgisMonatsChart({ vm, jahr }: { vm: PrognoseVsIstVM; jahr: number | undefined }) {
  const legende = useLegendenToggle()
  const achsen = useChartTheme()
  const schmal = useSchmaleAchse()
  const maxKwh = Math.max(0, ...vm.vergleichsDaten.flatMap(d => [d.prognose, d.ist]))
  const eAchse = energieAchse(maxKwh)
  return (
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
        Monatlicher Vergleich{jahr != null ? ` ${jahr}` : ''}
      </h2>
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={vm.vergleichsDaten} margin={{ top: ACHSEN_MARGIN_TOP }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="monatName" {...xAchse(schmal)} /* achsen-allow: Zeit-/Kategorie-Achse */ />
            <YAxis yAxisId="left" tickFormatter={eAchse.tick} label={achsenEinheit(eAchse.einheit)} {...yAchse(schmal)} />
            <YAxis yAxisId="right" orientation="right" {...yAchse(schmal)} tickFormatter={achsenTick} label={achsenEinheit('%', 'rechts')} />
            <Tooltip content={<ChartTooltip formatter={(value: number, name: string) =>
              name.includes('%') ? formatProzent(value).text : formatEnergie(value, maxKwh).text} />} />
            <Legend content={<ChartLegende onItemClick={legende.onItemClick} />} />
            <ReferenceLine yAxisId="right" y={0} stroke={achsen.referenz} strokeDasharray="3 3" />
            {/* D14-10 (detLAN #113): Balken ohne gestrichelte Umrandung — der Dash-Kanon
                (PROGNOSE_DASH) gilt nur für LINIEN-Serien, nicht als Bar-Border. */}
            <Bar yAxisId="left" dataKey="prognose" fill={SOLL_IST_COLORS.soll} name="PVGIS Prognose" hide={legende.istVersteckt('prognose')} />
            <Bar yAxisId="left" dataKey="ist" fill={SOLL_IST_COLORS.ist} name="IST-Erzeugung" hide={legende.istVersteckt('ist')} />
            <Line yAxisId="right" type="monotone" dataKey="abweichungProzent" stroke={SOLL_IST_COLORS.abweichung} strokeWidth={2} name="Abweichung %" dot={{ fill: SOLL_IST_COLORS.abweichung }} hide={legende.istVersteckt('abweichungProzent')} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </Card>
  )
}

/** Monats-Detailtabelle inkl. Gesamt-Fuß + Bewertungsspalte. */
export function PvgisDetailTabelle({ vm }: { vm: PrognoseVsIstVM }) {
  const eRef = Math.max(0, ...vm.vergleichsDaten.flatMap(d => [d.prognose, d.ist]))
  const e = (v: number) => formatEnergie(v, eRef).text
  return (
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Monatliche Details</h2>
      <Table mitFuss flaeche="karte">
          <TableHead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              <th className={`${KOPF_ZELLE} text-left`}>Monat</th>
              <th className={`${KOPF_ZELLE} text-right`}>PVGIS Prognose</th>
              <th className={`${KOPF_ZELLE} text-right`}>IST-Erzeugung</th>
              <th className={`${KOPF_ZELLE} text-right`}>Abweichung</th>
              <th className={`${KOPF_ZELLE} text-right`}>%</th>
              <th className={`${KOPF_ZELLE} text-center`}>Bewertung</th>
            </tr>
          </TableHead>
          <TableBody>
            {/* Die Zeile folgt DEMSELBEN Kriterium wie der KPI-Strip: `hatDaten`
                (Messung vorhanden) und `hatSoll` (Maßstab vorhanden), nicht
                `ist > 0`. Sonst schriebe die Tabelle „Keine Daten" für einen
                gemessenen Null-Monat, den die Kachel daneben mitzählt — und ein
                Monat vor der Anschaffung bekäme ein SOLL, das es nie gab. */}
            {vm.vergleichsDaten.map((d) => {
              const vergleichbar = d.hatDaten && d.hatSoll && d.abweichungProzent != null
              const pos = (d.abweichung ?? 0) >= 0
              return (
              <tr key={d.monat} className="border-b border-gray-100 dark:border-gray-800">
                <td className={`${ZELLE} font-medium`}>{d.monatName}</td>
                <td className={`${ZELLE} text-right tabular-nums text-yellow-600`}>
                  {d.hatSoll ? e(d.prognose) : <span className="text-gray-400 dark:text-gray-500">—</span>}
                </td>
                <td className={`${ZELLE} text-right tabular-nums`}>
                  {d.hatDaten ? <span className="text-green-600">{e(d.ist)}</span> : <span className="text-gray-400 dark:text-gray-500">-</span>}
                </td>
                <td className={`${ZELLE} text-right tabular-nums ${pos ? 'text-green-600' : 'text-red-600'}`}>
                  {vergleichbar ? `${pos ? '+' : ''}${e(d.abweichung ?? 0)}` : '-'}
                </td>
                <td className={`${ZELLE} text-right tabular-nums ${(d.abweichungProzent ?? 0) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {vergleichbar ? `${(d.abweichungProzent ?? 0) >= 0 ? '+' : ''}${formatProzent(d.abweichungProzent ?? 0).text}` : '-'}
                </td>
                <td className={`${ZELLE} text-center`}>
                  {!d.hatDaten ? <span className="text-gray-400 dark:text-gray-500">Keine Daten</span>
                    : !d.hatSoll ? <span className="text-gray-400 dark:text-gray-500">Kein SOLL</span>
                    : (d.abweichungProzent ?? 0) >= 5 ? <span className="inline-flex items-center gap-1 text-green-600"><TrendingUp className="h-4 w-4" />Übertroffen</span>
                    : (d.abweichungProzent ?? 0) >= -5 ? <span className="text-blue-600">Im Plan</span>
                    : (d.abweichungProzent ?? 0) >= -15 ? <span className="text-yellow-600">Leicht unter Plan</span>
                    : <span className="inline-flex items-center gap-1 text-red-600"><TrendingDown className="h-4 w-4" />Unter Plan</span>}
                </td>
              </tr>
              )
            })}
          </TableBody>
          <TableFoot>
            {/* Die Fußzeile muss in SICH aufgehen, sonst steht in einer Spalte
                eine Differenz, die aus den beiden Zellen links daneben nicht
                folgt. Sie zeigt deshalb `periodenPrognose` (den Nenner der
                Abweichung) und nennt das Fenster — nicht das volle Jahr, das in
                der Kachel „PVGIS Prognose" steht. */}
            <tr className="border-t-2 border-gray-300 dark:border-gray-600 font-bold">
              <td className={ZELLE}>
                Gesamt
                {vm.monateMitDaten > 0 && vm.monateMitDaten < 12 && (
                  <span className="ml-1 font-normal text-gray-500 dark:text-gray-400">
                    ({vm.monateMitDaten} von 12 Monaten)
                  </span>
                )}
              </td>
              <td className={`${ZELLE} text-right tabular-nums text-yellow-600`}>{e(vm.periodenPrognose)}</td>
              <td className={`${ZELLE} text-right tabular-nums text-green-600`}>{e(vm.jahresIst)}</td>
              <td className={`${ZELLE} text-right tabular-nums ${(vm.jahresAbweichung ?? 0) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {vm.jahresAbweichung == null ? '—' : `${vm.jahresAbweichung >= 0 ? '+' : ''}${e(vm.jahresAbweichung)}`}
              </td>
              <td className={`${ZELLE} text-right tabular-nums ${(vm.jahresAbweichungProzent ?? 0) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {vm.jahresAbweichungProzent == null ? '—' : `${vm.jahresAbweichungProzent >= 0 ? '+' : ''}${formatProzent(vm.jahresAbweichungProzent).text}`}
              </td>
              <td></td>
            </tr>
          </TableFoot>
        </Table>
    </Card>
  )
}

/** Interpretations-Hilfe (Bewertungs-Schwellen erklärt). */
export function PvgisErklaerung() {
  return (
    <div className="bg-purple-50 dark:bg-purple-900/20 rounded-lg p-4">
      <h3 className="font-medium text-purple-700 dark:text-purple-300 mb-2">Interpretation der Abweichungen</h3>
      <ul className="text-sm text-purple-600 dark:text-purple-400 space-y-1 list-disc list-inside">
        <li><strong>Übertroffen (&gt;5 %):</strong> Die Anlage produziert mehr als erwartet — sehr gut!</li>
        <li><strong>Im Plan (±5 %):</strong> Die Anlage entspricht den Erwartungen von PVGIS.</li>
        <li><strong>Leicht unter Plan (−5 % bis −15 %):</strong> Kleinere Abweichungen, z. B. durch lokale Wetterbedingungen.</li>
        <li><strong>Unter Plan (&lt;−15 %):</strong> Deutliche Minderleistung — Verschattung, Verschmutzung oder technische Probleme prüfen.</li>
      </ul>
      <p className="text-xs text-purple-500 dark:text-purple-400 mt-3">
        Hinweis: PVGIS basiert auf langjährigen Mittelwerten. Einzelne Monate können stark abweichen.
      </p>
    </div>
  )
}
