/**
 * Die Detail-Liste des Wärme/Klima-Blocks **je Funktion** — Bauschnitt 8
 * (Konzept Wärme/Klima §4 ②, §5 Position 3; SOLL §4.1).
 *
 * Bis 11.09.2026 stand die Liste nach **Größe** geordnet: erst zwei Stromzeilen,
 * dann drei Arbeitszahlen, die Wärme je Funktion nur im Balken darüber, die Kälte
 * nirgends als Zahl. Wer wissen wollte, *warum* die Heizzahl so aussieht, musste
 * die Zutaten über drei Elemente zusammensuchen.
 *
 * ⭐ **Jede Gruppe zeigt Zähler und Nenner ihrer eigenen Zahl.** Heizen und
 * Warmwasser teilen durch den getrennt gemessenen Strom (F5), Kühlen durch den
 * Betriebsart-Strom — genau die Mengen, die hier daneben stehen. Die Proben
 * `test_bs8_funktions_gruppen.py` halten den Vertrag im Backend fest.
 *
 * ⛔ **Keine Überschrift ohne Menge.** Eine Überschrift behauptet, dass das Gerät
 * diese Funktion hat. Eine Split-Klima bekäme sonst „Warmwasser", jede
 * Heizungs-Wärmepumpe im Winter „Kühlen" (Gegenprüfung G7). Eine Funktion ohne
 * Menge, aber mit Grund, bleibt die eine Zeile, die sie bis hierher war —
 * unverändert im Wortlaut, weil der Name die Funktion ohnehin trägt.
 *
 * Reine Funktion, damit sie ohne Render prüfbar ist (Bauform `verlaufRestZeilen`).
 */
import type { AktuellerMonatResponse } from '../api/aktuellerMonat'

export type WpFunktion = 'heizen' | 'warmwasser' | 'kuehlen'

export type FunktionsZeile =
  | { art: 'strom' | 'nutzenergie'; label: string; kwh: number }
  | { art: 'arbeitszahl'; label: string; wert: number | null; grund: string | null }

export interface FunktionsGruppe {
  funktion: WpFunktion
  titel: string
  zeilen: FunktionsZeile[]
}

export interface FunktionsGruppen {
  gruppen: FunktionsGruppe[]
  /** Funktionen ohne Menge, die trotzdem etwas zu sagen haben (ihren Grund). */
  ohneMenge: FunktionsZeile[]
}

type Zahl = number | null | undefined

interface Definition {
  funktion: WpFunktion
  titel: string
  strom: [string, Zahl]
  nutzenergie: [string, Zahl]
  arbeitszahl: [string, Zahl, string | null | undefined]
}

/** Die Namen der Mengen sind die des Formulars (S1: *Strom Heizen*, *Heizwärme*,
 *  *Warmwasser-Wärme*); die Kennzahl behält ihren eingeführten Namen. */
function definitionen(d: AktuellerMonatResponse): Definition[] {
  return [
    {
      funktion: 'heizen', titel: 'Heizen',
      strom: ['Strom Heizen', d.wp_strom_heizen_kwh],
      nutzenergie: ['Heizwärme', d.wp_heizung_kwh],
      arbeitszahl: ['Arbeitszahl · Heizen', d.wp_jaz_heizen, d.wp_jaz_heizen_grund],
    },
    {
      funktion: 'warmwasser', titel: 'Warmwasser',
      strom: ['Strom Warmwasser', d.wp_strom_warmwasser_kwh],
      nutzenergie: ['Warmwasser-Wärme', d.wp_warmwasser_kwh],
      arbeitszahl: ['Arbeitszahl · Warmwasser', d.wp_jaz_warmwasser, d.wp_jaz_warmwasser_grund],
    },
    {
      // Kühlen: Betriebsart = Funktion, der Strom ist derselbe wie im Segment
      // „Kühlen" des Betriebsart-Balkens.
      funktion: 'kuehlen', titel: 'Kühlen',
      strom: ['Strom Kühlen', d.wp_modus_strom_kuehlen_kwh],
      nutzenergie: ['Kälte', d.wp_kaelte_kwh],
      arbeitszahl: ['Arbeitszahl · Kühlen', d.wp_jaz_kuehlen, d.wp_jaz_kuehlen_grund],
    },
  ]
}

export function wpFunktionsGruppen(d: AktuellerMonatResponse): FunktionsGruppen {
  const gruppen: FunktionsGruppe[] = []
  const ohneMenge: FunktionsZeile[] = []
  for (const def of definitionen(d)) {
    const [azLabel, azWert, azGrund] = def.arbeitszahl
    // ⚠ Auch das gesperrte „—" erscheint, mit seinem Grund (S3) — ohne Wert UND
    // ohne Grund gibt es keine Auskunft, also keine Zeile (N-348).
    const az: FunktionsZeile | null = (azWert != null || azGrund)
      ? { art: 'arbeitszahl', label: azLabel, wert: azWert ?? null, grund: azGrund ?? null }
      : null
    const hatMenge = (def.strom[1] ?? 0) > 0 || (def.nutzenergie[1] ?? 0) > 0
    if (!hatMenge) {
      if (az) ohneMenge.push(az)
      continue
    }
    const zeilen: FunktionsZeile[] = []
    // `!= null`: eine gemessene 0 bleibt stehen — sie sagt „getrennt erfasst,
    // aktuell nichts", und das ist etwas anderes als „nicht erfasst".
    for (const [label, kwh, art] of [
      [...def.strom, 'strom'], [...def.nutzenergie, 'nutzenergie'],
    ] as const) {
      if (kwh != null) zeilen.push({ art, label, kwh })
    }
    if (az) zeilen.push(az)
    gruppen.push({ funktion: def.funktion, titel: def.titel, zeilen })
  }
  return { gruppen, ohneMenge }
}

/** E3 (b): Zeigen die Gruppen einen **getrennt gemessenen** Strom (F5), stehen im
 *  Block zwei verschiedene Strommengen „Heizen" — die der Funktion hier und die
 *  der Betriebsart im Balken (sie enthält den Warmwasser-Strom, IST §888). Dann
 *  muss der Balken sagen, dass er nach Betriebsart aufteilt (S2). */
export function zeigtStromJeFunktion(fg: FunktionsGruppen): boolean {
  return fg.gruppen.some((g) => g.funktion !== 'kuehlen' && g.zeilen.some((z) => z.art === 'strom'))
}
