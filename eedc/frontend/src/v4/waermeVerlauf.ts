/**
 * waermeVerlauf — die Serien des Wärme/Klima-Verlaufs, als reine Funktion.
 *
 * Getrennt von der Zeichnung, damit sie ohne Rendering prüfbar ist: Recharts
 * zeichnet in jsdom nichts, eine Probe am gerenderten Chart wäre grün ohne zu
 * messen (Befund aus Sitzung 193, N-424).
 *
 * Die Eingabe ist period-agnostisch — Jahr liefert Monate, Monat liefert Tage,
 * Tag liefert Stunden. Es gibt nur eine Regel-Sammlung, nicht drei.
 */
import { CHART_COLORS } from '../lib'
import type { VerlaufStapel, VerlaufLinie, WaermeVerlaufRow } from './WaermeVerlaufChart'

/** Eine Periode (Monat/Tag/Stunde) mit den Größen, die der Verlauf braucht. */
export interface WaermeVerlaufPunkt {
  /** Beschriftung auf der x-Achse (Monatskürzel, Tagesnummer, Stunde). */
  name: string
  wp_strom_kwh?: number | null
  wp_waerme_kwh?: number | null
  wp_waerme_abgeleitet_kwh?: number | null
  wp_modus_strom_heizen_kwh?: number | null
  wp_modus_strom_warmwasser_kwh?: number | null
  wp_modus_strom_kuehlen_kwh?: number | null
  wp_modus_strom_lueften_kwh?: number | null
  wp_modus_strom_entfeuchten_kwh?: number | null
  wp_modus_nicht_aufgeteilt_kwh?: number | null
  wp_modus_gemessen?: boolean | null
  wp_modus_abdeckung_h?: number | null
  wp_modus_strom_bezug_kwh?: number | null
  /** Monatsmittel der Außentemperatur (°C) — zweite Achse, per Legende
   *  ausblendbar. Fehlt sie, fehlt auch die Linie: Ein kalter Monat ohne
   *  Messreihe ist kein 0-°C-Monat. */
  temperatur_c?: number | null
}

export interface WaermeVerlaufDaten {
  rows: WaermeVerlaufRow[]
  stapel: VerlaufStapel[]
  linien: VerlaufLinie[]
  /** Σ der aufgeteilten Strommenge über alle Perioden (Grundmenge des Stapels). */
  bezugKwh: number
  /** Σ des gesamten Wärmepumpen-Stroms über alle Perioden. */
  stromKwh: number
  /** Hat überhaupt eine Periode eine Aufteilung beigesteuert? */
  hatStapel: boolean
  /** Gibt es mindestens eine Periode mit GEMESSENER Wärme? */
  hatGemesseneWaerme: boolean
  /** Gibt es überhaupt Außentemperatur-Werte? */
  hatTemperatur: boolean
}

const z = (v: number | null | undefined): number => (v == null ? 0 : v)

/**
 * ⚠ **Dieselbe Torbedingung wie beim Aufteilungs-Balken** (`hat_modus_split`,
 * `monats_fakten.py`): ohne erfasste Stunde und ohne Betriebsart-Zähler gibt es
 * keine Aufteilung — statt einer Reihe von Nullen. Der Balken über dem Verlauf
 * erscheint in derselben Lage ebenfalls nicht; zwei verschiedene Antworten auf
 * dieselbe Datenlage wären der teurere Fehler.
 */
const hatSplit = (p: WaermeVerlaufPunkt): boolean =>
  !!p.wp_modus_gemessen || z(p.wp_modus_abdeckung_h) > 0

/** Die sechs Segmente in der Reihenfolge des Aufteilungs-Balkens. */
const SEGMENTE: { key: string; feld: keyof WaermeVerlaufPunkt; label: string; farbe: string; immer?: boolean }[] = [
  { key: 'heizen', feld: 'wp_modus_strom_heizen_kwh', label: 'Heizen', farbe: CHART_COLORS.wpWaerme, immer: true },
  { key: 'warmwasser', feld: 'wp_modus_strom_warmwasser_kwh', label: 'Warmwasser', farbe: CHART_COLORS.wpWarmwasser },
  { key: 'kuehlen', feld: 'wp_modus_strom_kuehlen_kwh', label: 'Kühlen', farbe: CHART_COLORS.modusKuehlen, immer: true },
  { key: 'lueften', feld: 'wp_modus_strom_lueften_kwh', label: 'Lüften', farbe: CHART_COLORS.modusLueften },
  { key: 'entfeuchten', feld: 'wp_modus_strom_entfeuchten_kwh', label: 'Entfeuchten', farbe: CHART_COLORS.modusEntfeuchten },
  { key: 'rest', feld: 'wp_modus_nicht_aufgeteilt_kwh', label: 'Nicht aufgeteilt', farbe: CHART_COLORS.modusNichtAufgeteilt, immer: true },
]

/**
 * Baut Zeilen und Serien. Serien erscheinen nur, wenn sie etwas zu sagen haben:
 * Warmwasser/Lüften/Entfeuchten nur bei eigenem Zähler (E4, Konzept §2.3
 * *„Wer sie nicht erfasst, sieht sie nicht"*), die Wärmelinie nur mit
 * gemessener Wärme (E7).
 */
export function baueWaermeVerlauf(punkte: WaermeVerlaufPunkt[]): WaermeVerlaufDaten {
  const mitSplit = punkte.filter(hatSplit)
  const hatStapel = mitSplit.length > 0

  const aktiveSegmente = SEGMENTE.filter(
    (s) => s.immer || mitSplit.some((p) => z(p[s.feld] as number | null | undefined) > 0),
  )

  // E7 (Konzept §8, SOLL §3.3): NUR gemessene Wärme. Der abgeleitete Anteil
  // ist `Strom × Arbeitszahl` — eine Linie daraus hätte exakt die Form der
  // Stromfläche darunter und sagte nichts.
  //
  // ⚠ Hier zählt die MENGE, nicht das Flag `wp_waerme_abgeleitet`. Das Flag
  // heißt „irgendein Teil irgendeines Geräts" und ist für die Kennzahl richtig
  // so; bei einer Wärmepumpe mit Wärmemengenzähler NEBEN einer Klimaanlage
  // ohne einen solchen wäre es gesetzt, obwohl fast die ganze Wärme gemessen
  // ist. Wer danach ausblendet, verliert Gemessenes.
  const gemesseneWaerme = (p: WaermeVerlaufPunkt): number | null => {
    if (p.wp_waerme_kwh == null) return null
    const rest = p.wp_waerme_kwh - z(p.wp_waerme_abgeleitet_kwh)
    return rest > 0 ? Math.round(rest * 10) / 10 : null
  }
  const hatGemesseneWaerme = punkte.some((p) => gemesseneWaerme(p) != null)
  const hatTemperatur = punkte.some((p) => p.temperatur_c != null)

  const rows: WaermeVerlaufRow[] = punkte.map((p) => {
    const row: WaermeVerlaufRow = { name: p.name }
    const split = hatSplit(p)
    for (const s of aktiveSegmente) {
      // Eine Periode ohne Aufteilung trägt `null`, nicht `0` — sonst stünde
      // dort ein Balken der Höhe 0, der aussieht wie „nichts gelaufen",
      // während die Kachel Strom zeigt.
      row[s.key] = split ? Math.round(z(p[s.feld] as number | null | undefined) * 10) / 10 : null
    }
    if (hatGemesseneWaerme) row.waerme = gemesseneWaerme(p)
    // ⚠ `null` statt 0, wo kein Wert vorliegt — sonst zöge die Linie den
    // Monat auf den Gefrierpunkt.
    if (hatTemperatur) row.temperatur = p.temperatur_c ?? null
    return row
  })

  const stapel: VerlaufStapel[] = hatStapel
    ? aktiveSegmente.map((s) => ({ key: s.key, label: s.label, farbe: s.farbe }))
    : []
  const linien: VerlaufLinie[] = [
    ...(hatGemesseneWaerme
      ? [{ key: 'waerme', label: 'Wärme (gemessen)', farbe: CHART_COLORS.waermeGemessen, dezimalen: 1 }]
      : []),
    // Zweite Achse: °C gehört nicht auf die kWh-Skala. Per Legenden-Klick
    // ausblendbar wie jede andere Reihe.
    ...(hatTemperatur
      ? [{
          key: 'temperatur', label: 'Außentemperatur',
          farbe: CHART_COLORS.temperatur, achse: 'rechts' as const, dezimalen: 1,
        }]
      : []),
  ]

  return {
    rows, stapel, linien,
    // W-17b: die Grundmenge des Stapels. Sie ist NICHT `wp_strom_kwh` —
    // `modus_strom_bezug_kwh` zählt nur die Geräte mit Aufteilung
    // (`monats_fakten.py`: *„Auf Anlagenebene ist `strom_kwh` der falsche
    // Bezug"*). Der Aufrufer nennt die Differenz, wie der Balken es tut.
    bezugKwh: mitSplit.reduce((a, p) => a + z(p.wp_modus_strom_bezug_kwh), 0),
    stromKwh: punkte.reduce((a, p) => a + z(p.wp_strom_kwh), 0),
    hatStapel,
    hatGemesseneWaerme,
    hatTemperatur,
  }
}
