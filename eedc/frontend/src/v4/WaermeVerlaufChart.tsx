/**
 * WaermeVerlaufChart — der Verlauf im Wärme/Klima-Block (Konzept Wärme/Klima §8).
 *
 * Generisch über die x-Achse: `rows` tragen einen `name` (Monatskürzel, Tag,
 * Stunde), `bars` stapeln sich auf der kWh-Achse, `linien` liegen darüber —
 * wahlweise auf derselben Achse oder auf einer zweiten rechts. Damit ist es
 * **dieselbe** Komponente für Jahr (x = Monate), Monat (x = Tage) und Tag
 * (x = Stunden); die Sichten liefern nur andere Zeilen.
 *
 * ⭐ **Warum eine eigene Komponente und nicht die des Hubs.**
 * `KomponentenVerlaufChart` ist ein `BarChart` und kennt keine Linien. Ihn auf
 * `ComposedChart` zu heben wäre für den Hub **nicht** folgenlos: recharts
 * wählt die Cursor-Form am `chartName` (`component/Cursor.js:46-48`) —
 * `BarChart` bekommt ein `Rectangle` über die Bandbreite, alles andere eine
 * `Curve`. Das graue Hover-Band des Hubs (`CHART_HOVER_CURSOR`, nur `fill`)
 * würde dabei zur dünnen Linie. Gemessen 10.09.2026, kein Gate misst es.
 *
 * ⚑ Die Komposition ist bewusst je Chart, der **SoT sind die Primitive**:
 * `xAchse`/`yAchse`/`achsenEinheit`/`achsenTick`, `useLegendenToggle`,
 * `ChartLegende`, `eedcTooltipProps`, `CHART_COLORS`. Vorbild ist
 * {@link JahrCo2Chart} — gestapelte Balken links, Linie auf zweiter Achse
 * rechts, Formatter je Serie.
 */
import { useMemo } from 'react'
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { ChartLegende, eedcTooltipProps } from '../components/ui'
import { xAchse, yAchse, achsenEinheit, achsenTick, ACHSEN_MARGIN_TOP, fmtZahl } from '../lib'
import { useLegendenToggle, useSchmaleAchse } from '../hooks'

/** Eine gestapelte Mengen-Serie (kWh) — Farbe aus `lib/colors`. */
export interface VerlaufStapel {
  key: string
  label: string
  farbe: string
}

/** Eine Linien-Serie über dem Stapel. `achse: 'rechts'` legt sie auf die
 *  zweite Achse (andere Einheit); ohne Angabe teilt sie die kWh-Achse. */
export interface VerlaufLinie {
  key: string
  label: string
  farbe: string
  achse?: 'links' | 'rechts'
  /** Nachkommastellen im Tooltip (Default 1). */
  dezimalen?: number
}

export interface WaermeVerlaufRow {
  name: string
  [serie: string]: number | string | null
}

export function WaermeVerlaufChart({
  rows, stapel, linien = [], einheit = 'kWh', rechteEinheit, tall,
}: {
  rows: WaermeVerlaufRow[]
  stapel: VerlaufStapel[]
  linien?: VerlaufLinie[]
  einheit?: string
  /** Einheit der zweiten Achse — nur nötig, wenn eine Linie `achse: 'rechts'` trägt. */
  rechteEinheit?: string
  tall?: boolean
}) {
  const schmal = useSchmaleAchse()
  // B7-Standard: Serien per Legenden-Klick aus-/einblenden. Reset, wenn sich
  // der Serien-Satz ändert (andere Periode, andere Betriebsarten).
  const legende = useLegendenToggle([...stapel, ...linien].map((s) => s.key).join('|'))
  // Einheit je Serie für den Tooltip — die zweite Achse trägt eine andere
  // (°C, %), und ein gemeinsamer `unit` würde sie mit kWh beschriften.
  const einheitJeLabel = useMemo(() => {
    const m = new Map<string, { einheit: string; dezimalen: number }>()
    stapel.forEach((b) => m.set(b.label, { einheit, dezimalen: 0 }))
    linien.forEach((l) => m.set(l.label, {
      einheit: l.achse === 'rechts' ? (rechteEinheit ?? '') : einheit,
      dezimalen: l.dezimalen ?? 1,
    }))
    return m
  }, [stapel, linien, einheit, rechteEinheit])

  if (rows.length === 0) {
    return <p className="text-sm text-gray-500 dark:text-gray-400">Keine Verlaufsdaten erfasst.</p>
  }

  const hatRechte = linien.some((l) => l.achse === 'rechts')

  return (
    <div className={tall ? 'h-[420px]' : 'h-72'}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: ACHSEN_MARGIN_TOP, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-gray-200 dark:stroke-gray-700" />
          <XAxis dataKey="name" {...xAchse(schmal)} interval="preserveStartEnd" /* achsen-allow: Zeit-/Kategorie-Achse */ />
          <YAxis yAxisId="menge" {...yAchse(schmal, 48)} tickFormatter={achsenTick} label={achsenEinheit(einheit)} />
          {hatRechte && (
            <YAxis
              yAxisId="rechts" orientation="right" {...yAchse(schmal, 40)}
              tickFormatter={achsenTick} label={achsenEinheit(rechteEinheit ?? '', 'rechts')}
            />
          )}
          <Tooltip {...eedcTooltipProps({
            formatter: (value: number, name: string) => {
              const e = einheitJeLabel.get(name)
              return `${fmtZahl(value, e?.dezimalen ?? 0)}${e?.einheit ? ` ${e.einheit}` : ''}`
            },
          })} />
          <Legend wrapperStyle={{ fontSize: 11 }} content={<ChartLegende onItemClick={legende.onItemClick} />} />
          {stapel.map((b) => (
            <Bar
              key={b.key} yAxisId="menge" dataKey={b.key} name={b.label}
              stackId="menge" fill={b.farbe} hide={legende.istVersteckt(b.key)}
            />
          ))}
          {linien.map((l) => (
            // `connectNulls` bewusst NICHT: eine Lücke in der gemessenen Wärme
            // ist eine Aussage (E7 — es gibt dort keinen gemessenen Wert), und
            // eine durchgezogene Linie darüber wäre eine Behauptung.
            <Line
              key={l.key} yAxisId={l.achse === 'rechts' ? 'rechts' : 'menge'} type="monotone"
              dataKey={l.key} name={l.label} stroke={l.farbe} strokeWidth={2} dot={false}
              hide={legende.istVersteckt(l.key)}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
