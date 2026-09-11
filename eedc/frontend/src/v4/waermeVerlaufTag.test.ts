/**
 * Der Tag-Verlauf (Konzept §8, Bauschnitt 5) — dieselbe Funktion, x = Stunden.
 *
 * ⭐ **Der Gegenstand ist die Umbenennung, nicht eine Rechnung.** Die Stunden
 * verteilt das Backend (`tages_stapel.py`, „die Stunde verteilt den Tag"); der
 * Client benennt die Zeilen nur um und legt die Temperatur der Stundenantwort
 * daneben. Schwesterdateien: `waermeVerlauf.test.ts` (Jahr),
 * `waermeVerlaufMonat.test.ts` (Monat).
 *
 * ⚠ Bewusst NICHT am gerenderten Chart: Recharts zeichnet in jsdom nichts
 * (N-424, Sitzung 193).
 */
import { describe, it, expect } from 'vitest'
import { baueTagWaermeVerlauf } from './TagKomponenten'
import { baueWaermeVerlauf } from './waermeVerlauf'
import type { StundenWert, WaermeVerlaufStunde } from '../api/energie_profil'

const zeile = (h: number, over: Partial<WaermeVerlaufStunde> = {}): WaermeVerlaufStunde => ({
  stunde: h,
  wp_strom_kwh: 0.6,
  wp_waerme_kwh: 1.5,
  wp_modus_strom_heizen_kwh: 0.5,
  wp_modus_strom_warmwasser_kwh: 0,
  wp_modus_strom_kuehlen_kwh: 0,
  wp_modus_strom_lueften_kwh: 0,
  wp_modus_strom_entfeuchten_kwh: 0,
  wp_modus_nicht_aufgeteilt_kwh: 0.1,
  wp_modus_strom_bezug_kwh: 0.6,
  wp_modus_abdeckung_h: 0,
  wp_modus_gemessen: true,
  ...over,
})

const stunde = (h: number, temperatur_c: number | null): StundenWert => ({
  stunde: h, pv_kw: null, verbrauch_kw: null, einspeisung_kw: null, netzbezug_kw: null,
  batterie_kw: null, waermepumpe_kw: null, wallbox_kw: null, ueberschuss_kw: null,
  defizit_kw: null, temperatur_c, globalstrahlung_wm2: null, soc_prozent: null,
  komponenten: null, wp_starts_anzahl: null, wp_betriebsstunden: null,
})

describe('Tag-Verlauf — dieselbe Funktion, 24 Stunden', () => {
  it('beschriftet die Slots wie der Stundenverlauf derselben Sicht', () => {
    const punkte = baueTagWaermeVerlauf(Array.from({ length: 24 }, (_, h) => zeile(h)), [])
    expect(punkte).toHaveLength(24)
    expect(punkte[0].name).toBe('0:00')
    expect(punkte[23].name).toBe('23:00')
  })

  it('nimmt die Temperatur aus der Stundenantwort — je Slot, nicht je Position', () => {
    const punkte = baueTagWaermeVerlauf(
      [zeile(5), zeile(6)],
      [stunde(6, -2.5), stunde(5, 1.0)],
    )
    expect(punkte[0].temperatur_c).toBe(1.0)
    expect(punkte[1].temperatur_c).toBe(-2.5)
  })

  it('kennt am Tag keine abgeleitete Wärme — die Linie ist gemessen', () => {
    const v = baueWaermeVerlauf(baueTagWaermeVerlauf([zeile(1), zeile(2)], []))
    expect(v.hatGemesseneWaerme).toBe(true)
    expect(v.rows[0].waerme).toBe(1.5)
  })

  it('summiert die Grundmenge über alle Stunden eines Tages mit Aufteilung', () => {
    // Das Tor ist das Tor des Tages: auch eine Stunde, die nur Rest trägt,
    // zählt mit — sonst verlöre der Stapel seine Summe gegenüber dem Balken.
    const zeilen = [
      zeile(0, { wp_modus_strom_heizen_kwh: 0, wp_modus_nicht_aufgeteilt_kwh: 0.1, wp_modus_strom_bezug_kwh: 0.1 }),
      zeile(1),
    ]
    const v = baueWaermeVerlauf(baueTagWaermeVerlauf(zeilen, []))
    expect(v.hatStapel).toBe(true)
    expect(v.bezugKwh).toBeCloseTo(0.7, 9)
    expect(v.rows[0].rest).toBe(0.1)
  })
})
