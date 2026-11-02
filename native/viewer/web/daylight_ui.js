// The panel's daylight controls: the time of day (the sun's place for the date and time: sky.js), the Night switch
// (the time of day off), the shadows and the street lamp lights' pool (lighting.js); all remembered in this browser.
// Returns the time of day's line (to refresh with the info panel) and the Night box (checked by the time of day).
import { POOL_SIZES, POOL_DEFAULT } from './lighting.js'

export function daylightPanel({ sky, lights, minimap, parent }) {
  const NIGHT_KEY = 'bnge-viewer-night', POOL_KEY = 'bnge-viewer-lamp-lights', TOD_KEY = 'bnge-viewer-time-of-day',
    SHADOW_KEY = 'bnge-viewer-shadows'
  function stored(k) { try { return localStorage.getItem(k) } catch (e) { return null } }
  function store(k, v) { try { localStorage.setItem(k, v) } catch (e) { /* none */ } }
  function checkbox(label, on, fn) {
    const l = document.createElement('label')
    const cb = document.createElement('input')
    cb.type = 'checkbox'
    cb.checked = !!on
    cb.onchange = () => fn(cb.checked)
    l.append(cb, label)
    return [l, cb]
  }
  const hhmm = m => `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(Math.floor(m % 60)).padStart(2, '0')}`
  const todUi = {}
  {
    let tod = {}
    try { tod = JSON.parse(stored(TOD_KEY)) || {} } catch (e) { /* none */ }
    sky.timeOfDay = !!tod.on
    if (typeof tod.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tod.date)) sky.date = tod.date
    if (Number.isFinite(tod.minutes)) sky.minutes = tod.minutes
    sky.manualNight = stored(NIGHT_KEY) === '1'
    const saveTod = () => store(TOD_KEY, JSON.stringify({ on: sky.timeOfDay, date: sky.date, minutes: sky.minutes }))

    const box = document.createElement('div')
    box.className = 'tod'
    const date = document.createElement('input')
    date.type = 'date'
    date.value = sky.date
    date.onchange = () => { if (date.value) { sky.date = date.value; saveTod() } }
    const slider = document.createElement('input')
    slider.type = 'range'; slider.min = 0; slider.max = 1439; slider.step = 1
    slider.value = sky.minutes
    slider.setAttribute('aria-label', 'Local solar time')
    slider.oninput = () => { sky.minutes = +slider.value; saveTod() }
    const now = document.createElement('button')
    now.textContent = 'Now'
    now.title = "Today, and the time now at the camera's place (its mean solar time)"
    now.onclick = () => {
      const lon = minimap.cam ? minimap.cam.lon : 0
      // (the mean solar time at the camera's longitude, and its day, which may be another than the UTC one)
      const solar = new Date(Date.now() + lon * 4 * 60000)
      sky.date = `${solar.getUTCFullYear()}-${String(solar.getUTCMonth() + 1).padStart(2, '0')}-${String(solar.getUTCDate()).padStart(2, '0')}`
      sky.minutes = solar.getUTCHours() * 60 + solar.getUTCMinutes()
      date.value = sky.date; slider.value = sky.minutes
      saveTod()
    }
    const row = document.createElement('div')
    row.className = 'todRow'
    row.append(date, now)
    const readout = document.createElement('div')
    readout.className = 'todText'
    box.append(row, slider, readout)
    todUi.readout = readout

    const [ln, cbN] = checkbox('Night (lit windows, lamp lenses and glow, street lamp lights)', sky.manualNight, on => {
      sky.manualNight = on; store(NIGHT_KEY, on ? '1' : '0')
    })
    todUi.night = cbN
    function showTod() {
      box.classList.toggle('hidden', !sky.timeOfDay)
      cbN.disabled = sky.timeOfDay
      if (!sky.timeOfDay) cbN.checked = sky.manualNight
      ln.title = sky.timeOfDay ? 'Set by the time of day: night once the sun is 4 degrees under the horizon' : ''
    }
    const [lt] = checkbox('Time of day (the sun where it is at the date and time)', sky.timeOfDay, on => {
      sky.timeOfDay = on; saveTod(); showTod()
    })
    showTod()

    const [ls] = checkbox("Shadows (the sun's)", stored(SHADOW_KEY) === '1', on => { sky.setShadows(on); store(SHADOW_KEY, on ? '1' : '0') })
    if (stored(SHADOW_KEY) === '1') sky.setShadows(true)

    const n = +stored(POOL_KEY)
    lights.size = POOL_SIZES.includes(n) && stored(POOL_KEY) != null ? n : POOL_DEFAULT
    const l2 = document.createElement('label')
    const sel = document.createElement('select')
    for (const v of POOL_SIZES) { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.append(o) }
    sel.value = String(lights.size)
    sel.onchange = () => { lights.setSize(+sel.value); store(POOL_KEY, sel.value) }
    l2.append('Street lamp lights at night: ', sel)
    parent.append(lt, box, ln, ls, l2)
  }

  // the time of day's line: the solar time, its UTC, the sun's place
  function todText() {
    if (!sky.timeOfDay) return
    const s = sky.state.sun
    if (!s) { todUi.readout.textContent = `${hhmm(sky.minutes)} local solar time (no place yet)`; return }
    const u = new Date(sky.state.ms)
    todUi.readout.textContent = `${hhmm(sky.minutes)} local solar time (${hhmm(u.getUTCHours() * 60 + u.getUTCMinutes())} UTC) · ` +
      `sun ${s.elevation.toFixed(1)}° ${s.elevation >= 0 ? 'up' : 'down'}, azimuth ${s.azimuth.toFixed(0)}° · ${sky.state.night ? 'night' : 'day'}`
  }
  return { text: todText, night: todUi.night }
}
