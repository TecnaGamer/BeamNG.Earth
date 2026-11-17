// Offline test of the shared map control's real bridge handler; no Vue dependency needed.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../beamng_earth/ui/ui-vue/mods/beamngEarth/AutodriveSpeed.vue', import.meta.url), 'utf8')
const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')
const calls = [], changes = []
const props = { offset: 0, sendLua: (command, callback) => calls.push({command, callback}) }
const {adjust, error} = new Function('ref', 'defineProps', 'defineEmits', script + '\nreturn {adjust, error}')(
  value => ({value}), () => props, () => (name, value) => changes.push({name, value}))

adjust(5); adjust(5); adjust(-5); adjust(0)
assert.deepEqual(calls.map(c => c.command), [5, 5, -5, 0].map(step =>
  `beamngEarth_settings and beamngEarth_settings.uiGpsSpeedOffset(${step}) or nil`),
  'rapid clicks send increments, not values computed from a stale prop')
calls[0].callback({ok: true, value: 5})
calls[1].callback({ok: true, value: 10})
calls[2].callback({ok: true, value: 5})
calls[3].callback({ok: true, value: 0})
assert.deepEqual(changes, [5, 10, 5, 0].map(value => ({name: 'change', value})))
adjust(2); adjust(NaN); adjust('5')
assert.equal(calls.length, 4, 'unsupported requests cannot reach the engine')
calls[0].callback(null)
assert.equal(error.value, 'Could not update cruise speed')
calls[0].callback({ok: false, error: 'Unavailable'})
assert.equal(error.value, 'Unavailable')
assert.equal(changes.length, 4, 'failed replies do not overwrite the displayed preference')
calls[0].callback({ok: true, value: -5})
assert.equal(error.value, '')
assert.deepEqual(changes.at(-1), {name: 'change', value: -5})

for (const file of ['EarthMap.vue', 'EarthMap3d.vue']) {
  const map = readFileSync(new URL(`../beamng_earth/ui/ui-vue/mods/beamngEarth/${file}`, import.meta.url), 'utf8')
  assert.ok(map.includes('import AutodriveSpeed from "./AutodriveSpeed.vue"'), `${file} uses the shared control`)
  assert.ok(map.includes(':send-lua="engineLua"') && map.includes('gps.speedOffsetMph'), `${file} connects state and bridge`)
  new Function(map.split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, ''))
}
console.log('Autodrive cruise controls: increments, reset, failures and both map integrations passed')
