// node mod/tests/test_mapsearch.mjs — offline presentation and real EarthMap search-handler tests.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as sm from '../beamng_earth/ui/ui-vue/mods/beamngEarth/mapsearch.js'
import * as mm from '../beamng_earth/ui/ui-vue/mods/beamngEarth/mapmath.js'

const player = { lat: 40, lon: -75 }
const nearby = { name: 'Test Circuit', kind: 'race track', lat: 40.01, lon: -75,
  result_type: 'category', distance_mi: 2.4, country: 'United States', detail: 'race track · 2.4 mi · United States' }
assert.equal(sm.present(nearby, player).group, 'category')
assert.equal(sm.present(nearby, player).meta, 'Race tracks · 2.4 mi · United States')
assert.equal(sm.present(nearby, player).description, '', 'category metadata does not repeat')
assert.equal(sm.present({ ...nearby, saved: true }, player).group, 'saved')
assert.equal(sm.present({ name: 'Main St', kind: 'street', lat: 40, lon: -75, detail: 'Maryland' }, player).group, 'address')
assert.equal(sm.present({ name: 'Landmark', kind: 'wikipedia' }, player).group, 'name')
assert.equal(sm.present({ name: 'Old server', detail: 'Address', kind: 'house' }, null).description, 'Address')
assert.ok(!sm.present({ name: 'No coordinates' }, player).meta.includes('mi'))
assert.ok(!sm.present({ ...nearby, distance_mi: undefined }, null).meta.includes('mi'))
assert.ok(sm.miles(player, { lat: 41, lon: -75 }) > 69)
assert.ok(sm.miles({ lat: 0, lon: 179.9 }, { lat: 0, lon: -179.9 }) < 14)
assert.equal(sm.miles(null, player), null)
assert.equal(sm.validPoint({ lat: NaN, lon: 0 }), false)
assert.equal(sm.moveIndex(-1, -1, 3), 2)
assert.equal(sm.moveIndex(-1, 1, 3), 0)
assert.equal(sm.moveIndex(2, 1, 3), 0)
assert.equal(sm.moveIndex(0, -1, 3), 2)
assert.equal(sm.moveIndex(0, 1, 0), -1)
assert.deepEqual(sm.previewPosition({ x: -50, y: 600 }, 800, 600), { x: 24, y: 530, offscreen: true })
assert.deepEqual(sm.previewPosition({ x: 400, y: 200 }, 800, 600), { x: 400, y: 200, offscreen: false })
const backend = JSON.parse(readFileSync(new URL('../../server/search_categories.json', import.meta.url)))
assert.deepEqual(sm.CATEGORIES.map(c => c.kind).sort(), backend.map(c => c.name).sort(), 'all backend categories have icons')
for (const c of sm.CATEGORIES) {
  assert.ok(c.path && c.query && c.label)
  assert.ok(backend.find(b => b.name === c.kind).aliases.some(a => a.replaceAll('-', ' ') === c.query.replaceAll('-', ' ')), 'chips send reviewed category phrases')
}

const source = readFileSync(new URL('../beamng_earth/ui/ui-vue/mods/beamngEarth/EarthMap.vue', import.meta.url), 'utf8')
const fullScript = source.split('<script setup>')[1].split('</script>')[0]
new Function(fullScript.replace(/^import .*$/gm, '')) // Syntax-check the complete setup script without a Vue install.
const handlers = fullScript.slice(fullScript.indexOf('// ---- search '), fullScript.indexOf('// ---- teleport '))

function harness() {
  let timerId = 0
  const timers = new Map(), unmount = [], calls = [], gps = []
  const ref = value => {
    const r = { watcher: null }
    Object.defineProperty(r, 'value', { get: () => value, set: v => {
      if (value === v) return
      value = v
      if (r.watcher) queueMicrotask(r.watcher)
    } })
    return r
  }
  const query = ref(''), results = ref([]), searchMsg = ref('')
  const state = ref({ places: [], recent: [], player }), view = ref({ ...player, z: 10 }), follow = ref(true)
  const document = { activeElement: null }
  const nextTick = fn => Promise.resolve().then(fn || (() => {}))
  const factory = new Function('ref', 'computed', 'watch', 'onUnmounted', 'nextTick', 'setTimeout', 'clearTimeout',
    'engineLua', 'query', 'results', 'searchMsg', 'state', 'view', 'follow', 'sm', 'mm', 'w', 'h', 'screen',
    'toArray', 'luaStr', 'document', 'searchEl', 'setGps', 'askTeleport',
    handlers + '\nreturn { liveSearch, searchCategory, clearSearch, moveResult, previewResult, leaveResult, selectResult, onSearch, pickResult, searching, searchWide, resultIndex, hoverIndex, searchPreview, searchFocused, searchToolsVisible, searchPanelVisible, searchFocusOut };')
  const api = factory(ref, fn => ({ get value() { return fn() } }), (r, fn) => { r.watcher = fn },
    fn => unmount.push(fn), nextTick, (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId },
    id => timers.delete(id), (script, cb) => calls.push({ script, cb }), query, results, searchMsg, state, view,
    follow, sm, mm, ref(800), ref(600), () => ({ x: 300, y: 200 }), a => Array.isArray(a) ? a : [],
    JSON.stringify, document, ref(null), (...args) => gps.push(args), () => {})
  return { ...api, query, results, searchMsg, calls, gps, timers, unmount,
    tick: async () => { await nextTick(); await nextTick() },
    run: delay => { const job = [...timers].find(([, t]) => t.delay === delay); assert.ok(job, `timer ${delay}`); timers.delete(job[0]); job[1].fn() } }
}
{
  const h = harness()
  await h.searchCategory(sm.QUICK[0])
  assert.equal(h.calls.length, 1, 'chip starts exactly one Enter request')
  assert.match(h.calls[0].script, /uiGeocode\("race track", true\)/)
  assert.equal(h.searchWide.value, true)
  assert.equal(h.searching.value, true)
  assert.ok(![...h.timers.values()].some(t => t.delay === 300), 'no delayed autocomplete after chip')
  h.calls[0].cb({ id: 1 }); h.run(150)
  h.calls[1].cb({ results: [nearby] })
  assert.equal(h.results.value.length, 1)
  assert.equal(h.searching.value, false)
  h.moveResult(-1)
  await h.tick()
  assert.equal(h.resultIndex.value, 0)
  assert.equal(h.searchPreview.value.result.name, nearby.name)
  h.leaveResult()
  assert.ok(h.searchPreview.value, 'keyboard selection remains highlighted after mouse leave')
  h.pickResult(nearby)
  await h.tick()
  assert.equal(h.gps.length, 1, 'preserves set-GPS result activation; X still teleports')
  assert.equal(h.searchPreview.value, null)
  assert.equal(h.timers.size, 0, 'activation cancels timers')
}
{
  const h = harness()
  h.query.value = 'forest'; await h.tick(); h.run(300)
  const old = h.calls[0]
  h.query.value = 'airport'; await h.tick()
  old.cb({ id: 1 })
  assert.equal(h.calls.length, 1, 'query change rejects stale callbacks before debounce fires')
  assert.ok(![...h.timers.values()].some(t => t.delay === 150))
  h.run(300); h.calls[1].cb({ id: 2 }); h.run(150)
  h.calls[2].cb({ results: [] })
  assert.match(h.searchMsg.value, /No matches yet/)
  h.liveSearch(true); h.calls[3].cb({ id: 3 }); h.run(150)
  h.calls[4].cb({ results: [] })
  assert.match(h.searchMsg.value, /No places found/)
}
{
  const h = harness()
  await h.searchCategory(sm.QUICK[0]); h.calls[0].cb({ error: 'Offline' })
  assert.equal(h.searching.value, false)
  assert.equal(h.searchMsg.value, 'Offline')
  h.liveSearch(true); h.calls[1].cb({ id: 1 }); h.run(150)
  const pending = h.calls[2]
  h.previewResult(0)
  h.unmount.forEach(fn => fn())
  pending.cb({ results: [nearby] })
  assert.equal(h.results.value.length, 0, 'closed map ignores pending responses')
  assert.equal(h.timers.size, 0, 'closed map retains no search timers')
}
{
  const h = harness()
  await h.searchCategory(sm.QUICK[0]); h.calls[0].cb({ id: 1 })
  h.results.value = [nearby]; h.previewResult(0)
  assert.ok(h.searchPreview.value)
  h.clearSearch(); await h.tick()
  assert.equal(h.searchPreview.value, null)
  assert.equal(h.timers.size, 0, 'clear cancels hover preload, debounce and polling')
}
{
  const h = harness()
  h.query.value = 'forest'; await h.tick(); h.run(300)
  assert.equal(h.searching.value, true)
  h.onSearch()
  assert.match(h.calls[1].script, /uiMapParse/, 'Enter may widen a typing search still in flight')
  h.calls[1].cb(null)
  assert.match(h.calls[2].script, /uiGeocode\("forest", true\)/)
  h.onSearch()
  assert.equal(h.calls.length, 3, 'repeated Enter while wider search runs cannot select old results')
}
console.log('mapsearch presentation, chips, navigation, stale responses, empty/error states and teardown: OK')

{
  const h = harness()
  assert.equal(h.searchPanelVisible.value, false, 'no idle palette over the map')
  h.searchFocused.value = true
  assert.equal(h.searchToolsVisible.value, true, 'empty focused field offers chips')
  h.searchFocusOut({ currentTarget: { contains: () => true }, relatedTarget: {} })
  assert.equal(h.searchToolsVisible.value, true, 'tabbing to a chip does not collapse it')
  h.searchFocusOut({ currentTarget: { contains: () => false }, relatedTarget: null })
  assert.equal(h.searchToolsVisible.value, false)
  assert.equal(h.searchPanelVisible.value, false, 'leaving an empty search hides chips/hint')
  h.results.value = [nearby]
  assert.equal(h.searchPanelVisible.value, true, 'visible results retain their data attribution')
  assert.equal(h.searchToolsVisible.value, false)
  h.query.value = 'forest'; await h.tick()
  assert.equal(h.searchToolsVisible.value, true, 'typed query keeps chips when focus leaves')
}
assert.match(source, /A sets GPS, then X teleports/)
const areaHandlers = fullScript.slice(fullScript.indexOf('function askRoute('), fullScript.indexOf('// ---- search '))
assert.ok(!areaHandlers.includes('searchPollTimer'), 'area polling never shares the search timer (0494de12)')
