import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { usePrognoseVsIst } from './PrognoseVsIstTeile'
import { monatsdatenApi, pvgisApi } from '../../api'
import type { AggregierteMonatsdaten } from '../../api/monatsdaten'
import { monatsZeile } from '../../test/factories'

// Paket Q (Doppel-Fetch-Bereinigung): der V4-Dispatcher hält die aggregierten
// Monatsdaten in useAuswertungBasis und reicht sie als 3. Argument herein —
// dann darf der Hook listAggregiert NICHT selbst rufen. Ohne 3. Argument
// (V3 pages/PrognoseVsIst) bleibt der Eigen-Fetch-Vertrag erhalten.
// Bewusst vi.spyOn statt vi.mock aufs api-Barrel: das Modul-Mocking des
// Barrels ließ den jsdom-Worker im Import-Graph dieses Files OOM-sterben.

const MD = [monatsZeile(2026, 5), monatsZeile(2025, 5)]
// ⚠️ Identitäts-Vertrag des 3. Arguments (wie jede Effekt-Dependency): Aufrufer
// müssen eine RENDER-STABILE Referenz übergeben (State/useMemo) — ein Inline-
// Literal erzeugt eine Refetch-Schleife. Genau das passierte hier im ersten
// Wurf (inline [] → Endlos-Loop → jsdom-Worker-OOM).
const LEER: AggregierteMonatsdaten[] = []

/** Eine Antwort von `GET /pvgis/soll/{id}/{jahr}`. */
function sollAntwort(jahr: number, jeMonat: (monat: number) => number | null) {
  const monate = Array.from({ length: 12 }, (_, i) => ({ monat: i + 1, soll_kwh: jeMonat(i + 1) }))
  const vorhanden = monate.map(m => m.soll_kwh).filter((v): v is number => v != null)
  return {
    anlage_id: 1,
    jahr,
    monate,
    jahr_kwh: vorhanden.length ? vorhanden.reduce((a, b) => a + b, 0) : null,
  }
}

function mockeSoll(antwort: ReturnType<typeof sollAntwort>) {
  return vi.spyOn(pvgisApi, 'getSollJeMonat').mockResolvedValue(
    antwort as unknown as Awaited<ReturnType<typeof pvgisApi.getSollJeMonat>>,
  )
}

let listSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  listSpy = vi.spyOn(monatsdatenApi, 'listAggregiert').mockResolvedValue(MD) as unknown as ReturnType<typeof vi.fn>
  vi.spyOn(pvgisApi, 'getAktivePrognose').mockResolvedValue(
    { jahresertrag_kwh: 1000, monatswerte: [] } as unknown as Awaited<ReturnType<typeof pvgisApi.getAktivePrognose>>,
  )
  mockeSoll(sollAntwort(2026, () => null))
})
afterEach(() => vi.restoreAllMocks())

describe('usePrognoseVsIst — vorgeladene Monatsdaten (Paket Q)', () => {
  it('mit vorgeladenen Daten: KEIN listAggregiert-Call, Daten kommen aus dem Argument', async () => {
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, MD))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(listSpy).not.toHaveBeenCalled()
    expect(result.current.monatsdaten).toEqual(MD)
    expect(result.current.verfuegbareJahre).toEqual([2026, 2025])
  })

  it('leeres Array gilt als vorgeladen (ehrlich leer) — kein Fallback-Fetch', async () => {
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, LEER))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(listSpy).not.toHaveBeenCalled()
    expect(result.current.monatsdaten).toEqual([])
  })

  it('Gegenprobe (V3-Vertrag): ohne 3. Argument fetcht der Hook selbst', async () => {
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(listSpy).toHaveBeenCalledTimes(1)
    expect(result.current.monatsdaten).toEqual(MD)
  })
})

// ────────────────────────────────────────────────────────────────────────────
// Der Maßstab kennt den Zubau — ein Modul zählt erst ab seiner Anschaffung
// ────────────────────────────────────────────────────────────────────────────
// Der gemessene Schaden (Michael, 2026-09-18): Anlage „Zuhause", 5,15 kWp auf
// dem Papier — davon hingen bis zum 08.09.2026 aber nur die 0,85 kWp eines
// Balkonkraftwerks am Netz. Block ① verglich Januar–August trotzdem gegen das
// SOLL der vollen Anlage: 743 kWh gegen 3.977 kWh, „−81 % · Unter Plan" in
// jedem Monat. Gegen die Leistung, die es in diesen Monaten wirklich gab
// (656 kWh), liegt dieselbe Anlage bei +13 %.
//
// Gerechnet wird das im Backend (`services/pvgis_soll.py` über
// `GET /pvgis/soll/{id}/{jahr}`, Abdeckung in `test_soll_je_monat_zubau.py`).
// Hier wird nur geprüft, dass der Client DIESEN Maßstab nimmt und nicht die
// ungekürzten `monatswerte` — die Trennlinie, an der ADR-001 hängt.
const KLEIN = 100   // was es Jan–Aug wirklich gab
const GROSS = 500   // Anlage im heutigen Ausbau (ungekürzt)

describe('usePrognoseVsIst — der SOLL kommt gekürzt aus dem Backend', () => {
  beforeEach(() => {
    vi.spyOn(pvgisApi, 'getAktivePrognose').mockResolvedValue({
      jahresertrag_kwh: GROSS * 12,
      monatswerte: Array.from({ length: 12 }, (_, i) => ({ monat: i + 1, e_m: GROSS })),
    } as unknown as Awaited<ReturnType<typeof pvgisApi.getAktivePrognose>>)
    mockeSoll(sollAntwort(2026, m => (m <= 8 ? KLEIN : GROSS)))
  })

  it('Januar–August tragen den kleinen Maßstab, nicht das SOLL der ganzen Anlage', async () => {
    const md = Array.from({ length: 8 }, (_, i) => monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 110 }))
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, md))
    await waitFor(() => expect(result.current.loading).toBe(false))

    // Der Kern: 800 (gekürzt) statt 4.000 (roh). Ohne die Route stünde hier 4.000.
    expect(result.current.periodenPrognose).toBe(KLEIN * 8)
    expect(result.current.jahresIst).toBe(880)
    expect(result.current.jahresAbweichung).toBe(80)
    expect(result.current.jahresAbweichungProzent).toBeCloseTo(10, 6)
    expect(result.current.vergleichsDaten[0].prognose).toBe(KLEIN)
  })

  it('die Hochrechnung rechnet saisonal, nicht flach × 12', async () => {
    const md = Array.from({ length: 8 }, (_, i) => monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 110 }))
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, md))
    await waitFor(() => expect(result.current.loading).toBe(false))

    // Flach wären es 880/8×12 = 1.320 — das behandelt einen Januar wie einen
    // Juli UND unterschlägt, dass die Rest-Monate den Zubau tragen (#387).
    // Saisonal: 880 ÷ 800 × 2.800 = 3.080.
    expect(result.current.hochgerechneterJahresIst).toBeCloseTo(3080, 6)
    // Und sie ist mit der Abweichungs-Kachel konsistent: beide +10 %.
    const proz = (result.current.hochgerechneterJahresIst / result.current.jahresPrognose - 1) * 100
    expect(proz).toBeCloseTo(result.current.jahresAbweichungProzent ?? 0, 6)
  })

  it('die Jahres-Kachel zeigt die datumsbewusste Erwartung, nicht die Anlage ganzjährig', async () => {
    const md = Array.from({ length: 8 }, (_, i) => monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 110 }))
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, md))
    await waitFor(() => expect(result.current.loading).toBe(false))

    // 8 × 100 + 4 × 500 = 2.800 — weder 1.200 (nur klein) noch 6.000 (nur groß).
    expect(result.current.jahresPrognose).toBe(KLEIN * 8 + GROSS * 4)
  })

  it('ein Monat ohne Maßstab zählt in keinen Nenner (Jahr vor der Anlage)', async () => {
    mockeSoll(sollAntwort(2023, m => (m >= 9 ? KLEIN : null)))
    const md = Array.from({ length: 12 }, (_, i) => monatsZeile(2023, i + 1, { pv_erzeugung_kwh: 110 }))
    const { result } = renderHook(() => usePrognoseVsIst(1, 2023, md))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.vergleichsDaten[0].hatSoll).toBe(false)
    expect(result.current.vergleichsDaten[8].hatSoll).toBe(true)
    // Nur Sep–Dez sind vergleichbar: 4 × 100 im Nenner, 4 × 110 im Zähler.
    expect(result.current.periodenPrognose).toBe(KLEIN * 4)
    expect(result.current.jahresAbweichung).toBe(40)
  })

  it('Rückfall: ohne GESPEICHERTE Prognose zählen die Live-Rohwerte', async () => {
    // Das Backend kennt nur zur aktiven (gespeicherten) Prognose einen Maßstab.
    // Für die Live-Prognose bleibt es beim ungekürzten Wert — bewusst, sonst
    // stünde die Sicht bis zum ersten „Prognose speichern" leer.
    vi.spyOn(pvgisApi, 'getAktivePrognose').mockResolvedValue(
      null as unknown as Awaited<ReturnType<typeof pvgisApi.getAktivePrognose>>)
    const sollSpy = mockeSoll(sollAntwort(2026, () => KLEIN))
    vi.spyOn(pvgisApi, 'getPrognose').mockResolvedValue({
      jahresertrag_kwh: GROSS * 12,
      monatsdaten: Array.from({ length: 12 }, (_, i) => ({ monat: i + 1, e_m: GROSS })),
      module: [],
    } as unknown as Awaited<ReturnType<typeof pvgisApi.getPrognose>>)

    const md = [monatsZeile(2026, 1, { pv_erzeugung_kwh: 110 })]
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, md))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(sollSpy).not.toHaveBeenCalled()
    expect(result.current.periodenPrognose).toBe(GROSS)
  })
})

// ────────────────────────────────────────────────────────────────────────────
// Die Abweichung misst nur die Monate, die eine Messung tragen
// ────────────────────────────────────────────────────────────────────────────
// Zähler angefangen, Nenner vollständig: acht erfasste Monate gegen zwölf
// Monate SOLL. Dieselbe Falle wie N-69 (laufender Monat), eine Ebene höher —
// und dieselbe Antwort (Entscheid Gernot, 2026-08-04): den Nenner kürzen. Im
// gemeldeten Fall erklärt das 4,6 der 85,6 Prozentpunkte; den Rest trägt der
// Maßstab (Block darüber). `jahresPrognose` bleibt bewusst das volle Jahr —
// die Kachel daneben heißt „Jahr 2026" und meint auch das Jahr.
const JAN_BIS_AUG = Array.from({ length: 8 }, (_, i) =>
  monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 110 }),
)
const MIT_NULL_MONAT = [
  ...Array.from({ length: 7 }, (_, i) => monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 100 })),
  monatsZeile(2026, 8, { pv_erzeugung_kwh: 0 }),
]
const MIT_UNGEMESSENEM_MONAT = [
  ...Array.from({ length: 7 }, (_, i) => monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 100 })),
  monatsZeile(2026, 8), // Zeile da, PV nie gemessen (Factory: pv_erzeugung_kwh = null)
]

describe('usePrognoseVsIst — die Abweichung vergleicht nur den gemessenen Zeitraum', () => {
  beforeEach(() => {
    mockeSoll(sollAntwort(2026, () => 100))
  })

  it('Teiljahr: der SOLL der Abweichung endet mit dem letzten erfassten Monat', async () => {
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, JAN_BIS_AUG))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.jahresPrognose).toBe(1200) // Jahres-KPI: das volle Jahr
    expect(result.current.monateMitDaten).toBe(8)
    expect(result.current.periodenPrognose).toBe(800) // der Nenner der Abweichung
    expect(result.current.jahresIst).toBe(880)
    expect(result.current.jahresAbweichung).toBe(80)
    expect(result.current.jahresAbweichungProzent).toBeCloseTo(10, 6)
  })

  it('ein gemessener Null-Monat zählt mit — 0 ist ein Messwert, nicht „keine Daten"', async () => {
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, MIT_NULL_MONAT))
    await waitFor(() => expect(result.current.loading).toBe(false))

    // Das alte Kriterium `ist === 0` warf genau diesen Monat aus beiden Summen —
    // die CLAUDE.md-Falle „0-Werte prüfen: `is not None` statt `if val`".
    expect(result.current.monateMitDaten).toBe(8)
    expect(result.current.vergleichsDaten[7].hatDaten).toBe(true)
    expect(result.current.periodenPrognose).toBe(800)
    expect(result.current.jahresIst).toBe(700)
    expect(result.current.jahresAbweichungProzent).toBeCloseTo(-12.5, 6)
  })

  it('Gegenprobe: eine Zeile ohne PV-Messung zählt NICHT als Monat mit Daten', async () => {
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, MIT_UNGEMESSENEM_MONAT))
    await waitFor(() => expect(result.current.loading).toBe(false))

    // Diese Probe grenzt gegen die ÜBERkorrektur ab: `hatDaten = zeilen.length > 0`
    // (Zeile statt Messung) wäre hier true und die Summen 8/800. Sie ist bewusst
    // auf `hatDaten` formuliert statt nur auf die Summen — über die allein wäre
    // sie vom alten `ist !== 0` nicht zu unterscheiden.
    expect(result.current.vergleichsDaten[7].hatDaten).toBe(false)
    expect(result.current.monateMitDaten).toBe(7)
    expect(result.current.periodenPrognose).toBe(700)
    expect(result.current.jahresIst).toBe(700)
    expect(result.current.jahresAbweichung).toBe(0)
  })

  it('Volljahr bleibt unberührt: Perioden-SOLL == Jahres-SOLL', async () => {
    const vollJahr = Array.from({ length: 12 }, (_, i) =>
      monatsZeile(2026, i + 1, { pv_erzeugung_kwh: 90 }),
    )
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, vollJahr))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.monateMitDaten).toBe(12)
    expect(result.current.periodenPrognose).toBe(result.current.jahresPrognose)
    expect(result.current.jahresAbweichung).toBe(-120)
    expect(result.current.jahresAbweichungProzent).toBeCloseTo(-10, 6)
  })

  it('kein vergleichbarer Monat ⇒ `null`, nicht „+0 kWh · +0,0 %"', async () => {
    // Ein Jahr mit Zählerzeilen, aber ohne PV-Messung ist über die Jahres-Auswahl
    // erreichbar (`useAuswertungBasis` bildet sie aus ALLEN Monatszeilen). Eine 0
    // läse sich dort als „exakt im Plan" in Grün — dieselbe Entscheidung wie in
    // `lib/sollErfuellung.ts`: lieber kein Wert als ein beruhigender falscher.
    const ohneMessung = Array.from({ length: 12 }, (_, i) => monatsZeile(2026, i + 1))
    const { result } = renderHook(() => usePrognoseVsIst(1, 2026, ohneMessung))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.monateMitDaten).toBe(0)
    expect(result.current.periodenPrognose).toBe(0)
    expect(result.current.jahresAbweichung).toBeNull()
    expect(result.current.jahresAbweichungProzent).toBeNull()
  })
})
