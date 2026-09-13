/**
 * **N-371 — die Option „Fremdanteil auf den Zählern" nennt die Klasse, nicht ein Gerät.**
 *
 * Es ist die **einzige** Angabe, mit der ein Anwender eedc sagen kann, dass sein
 * WP-Stromzähler mehr misst als die Wärmepumpe — und je einzelnem Gerät die
 * einzige Lage, die die Arbeitszahl-Sperre überhaupt auslöst. Bis zum
 * 13.09.2026 hieß sie „Heizstab-Strom liegt mit auf dem Stromzähler". Wer eine
 * **Klimaanlage** auf demselben Zähler hat (dietmar1968, T89667 #290), suchte
 * unter „Heizstab", fand nichts — und bekam weiter eine Arbeitszahl, die zwei
 * Geräte mischt.
 *
 * ⚠ **Gelesen wird das gerenderte `<option>`, nicht die Konstante.** Ein Test,
 * der `ABGRENZUNG_OPTIONEN` importiert, prüft, dass ein Text mit sich selbst
 * übereinstimmt; ob er beim Anwender ankommt, sagt er nicht. Die Konstante ist
 * modul-privat, und das soll sie bleiben.
 *
 * ⛔ **Der Heizstab muss als Beispiel drin bleiben** — nicht aus Nostalgie:
 * ``test_n349_beide_lagen_nennen_den_heizstab_und_das_ist_die_aussage``
 * (Backend) hält fest, dass **beide** Lagen ihn nennen, weil nicht das Gerät
 * über den Fall entscheidet, sondern wo die Zähler sitzen. Die Beschreibung
 * unter dem Feld nennt ihn nicht — das Label ist die einzige Stelle.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { WaermepumpeFelder } from './WaermepumpeFelder'

const noop = () => {}

/** Rendert das WP-Formular und gibt die Auswahl „Fremdanteil auf den Zählern" zurück. */
function fremdanteilAuswahl(params: Record<string, unknown> = {}) {
  render(
    <WaermepumpeFelder
      paramData={{ wp_art: 'luft_wasser', ...params }}
      onInputChange={noop}
      setParam={noop}
      zeige={() => undefined}
      markTouched={noop}
      setFeldRef={() => () => {}}
    />,
  )
  return screen.getByLabelText(/Fremdanteil auf den Zählern/) as HTMLSelectElement
}

describe('WaermepumpeFelder — Fremdanteil auf den Zählern (N-371)', () => {
  it('nennt bei `fremdstrom` einen weiteren Verbraucher, nicht nur den Heizstab', () => {
    const auswahl = fremdanteilAuswahl()
    const option = Array.from(auswahl.options).find((o) => o.value === 'fremdstrom')

    expect(option).toBeTruthy()
    expect(option!.textContent).toBe(
      'Ein weiterer Verbraucher liegt mit auf dem Stromzähler (z. B. Heizstab)',
    )
  })

  it('führt den Heizstab weiter als Beispiel — beide Lagen nennen ihn (N-349)', () => {
    const auswahl = fremdanteilAuswahl()
    const beschriftungen = Array.from(auswahl.options).map((o) => o.textContent ?? '')

    // Die `fremdstrom`-Zeile darf das Beispiel nicht verlieren; sonst liest
    // sich die Liste wieder als Geräte-Aufzählung.
    expect(beschriftungen.filter((t) => t.includes('Heizstab'))).toHaveLength(1)
  })

  it('behält den gespeicherten Wert `fremdstrom` — eine Beschriftung braucht keine Migration', () => {
    const auswahl = fremdanteilAuswahl({ abgrenzung: 'fremdstrom' })

    expect(auswahl.value).toBe('fremdstrom')
    expect(Array.from(auswahl.options).map((o) => o.value)).toEqual([
      '', 'fremdstrom', 'fremdwaerme',
    ])
  })

  it('lässt die Beschreibung unter dem Feld unverändert bei der Zählerlage', () => {
    // Sie war schon vorher allgemein formuliert (Fundtext N-371) und trägt die
    // eigentliche Aussage: nicht das Gerät entscheidet, sondern der Zähler.
    render(
      <WaermepumpeFelder
        paramData={{ wp_art: 'luft_wasser', abgrenzung: 'fremdstrom' }}
        onInputChange={noop}
        setParam={noop}
        zeige={() => undefined}
        markTouched={noop}
        setFeldRef={() => () => {}}
      />,
    )

    expect(
      screen.getByText(/Seine Wärme läuft NICHT über den Wärmemengenzähler/),
    ).toBeTruthy()
  })
})
