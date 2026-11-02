// The viewer page: the scene, the world (world.js: earthd's files as the game gets them), the OSM overlay,
// the fly camera and the minimap.
import * as THREE from 'three'
import { Net } from './net.js'
import { World, stats } from './world.js'
import { Osm } from './osm.js'
import { FlyControls } from './controls.js'
import { Minimap } from './minimap.js'
import { setWireframe, loadModMaterials, materialStats, setGameMaterials, settings as matSettings } from './materials.js'
import { setRenderer, textureStats } from './textures.js'
import * as gc from './geocode.js'
import { itemOf } from './props.js'
import { NightLights } from './lighting.js'
import { Sky } from './sky.js'
import { daylightPanel } from './daylight_ui.js'

const $ = id => document.getElementById(id)

// --- scene ---
const canvas = $('view')
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true })
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1))
setRenderer(renderer)       // (textures.js: the compressed formats this WebGL takes)
const scene = new THREE.Scene()
scene.background = new THREE.Color(0x8fb4d6)
const camera = new THREE.PerspectiveCamera(60, 1, 0.5, 2e6)
camera.up.set(0, 0, 1)
camera.position.set(0, 0, 200)
const hemi = new THREE.HemisphereLight(0xdfeeff, 0x4a4030, 1.3)
scene.add(hemi)
const sun = new THREE.DirectionalLight(0xffffff, 1.6)
sun.position.set(0.4, -0.6, 1)
scene.add(sun)
const root = new THREE.Group()
scene.add(root)

function resize() {
  const w = window.innerWidth, h = window.innerHeight
  renderer.setSize(w, h, false)
  camera.aspect = w / h
  camera.updateProjectionMatrix()
}
window.addEventListener('resize', resize)
resize()

// --- world ---
// (the mod's material definitions before any shape is made: the signs' lettering, the mod's colours)
const modMaterialCount = await loadModMaterials()
let world
const net = new Net(msg => world.onMessage(msg))
world = new World(net, root)
const osm = new Osm(world, root)
const controls = new FlyControls(camera, canvas, pick)
const minimap = new Minimap($('minimap'), (lat, lon) => teleport(lat, lon))
minimap.z = 3
$('centre').onclick = () => minimap.centre()
// night lighting (lighting.js: nightlights.lua's windows, lamp lenses, far glow and street lamp lights)
// and the sun, sky and shadows (sky.js: the time of day)
const lights = new NightLights(root, world.props)
const sky = new Sky(scene, root, renderer, hemi, sun)
window.viewer = { camera, controls, world, osm, minimap, scene, lights, sky }      // (for the browser console)

const LAST = 'bnge-viewer-last'
function remember(lat, lon) { try { localStorage.setItem(LAST, JSON.stringify([lat, lon])) } catch (e) { /* none */ } }
function lastPlace() {
  const m = /^#(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)$/.exec(decodeURIComponent(location.hash))
  if (m) return [+m[1], +m[2]]
  try { const v = JSON.parse(localStorage.getItem(LAST)); return Array.isArray(v) ? v : null } catch (e) { return null }
}

function teleport(lat, lon, place = null) {
  if (!world.hello) { world.status = 'Not connected to earthd'; return }
  world.teleport(lat, lon)
  minimap.pending = { lat, lon }
  remember(lat, lon)
  if (place) gc.addRecent(place)
  where.reset()
  $('location').textContent = ''
}

// --- place search and the location line (geocode.js: earthd's geocode / where, as the mod) ---
const search = new gc.Search(m => net.send(m))
const where = new gc.Where()
// the debugging switch: Photon asked by this page (geocode.js Direct) instead of through earthd
const DIRECT_KEY = 'bnge-viewer-geocode-direct'
let geoDirect = false
try { geoDirect = localStorage.getItem(DIRECT_KEY) === '1' } catch (e) { /* none */ }
const direct = new gc.Direct(d => {
  const ok = d.status === 200 || d.status === 'cache'
  const line = `${d.status} ${d.ms ? `${d.ms.toFixed(0)} ms` : ''} ${d.n != null ? `${d.n} features` : ''} GET ${d.url}`
  ;(ok ? console.debug : console.warn)('geocode (viewer):', line, d.error || '')
  const el = $('geoDebug')
  el.textContent = ''
  const s = document.createElement('span')
  s.className = ok ? '' : 'bad'
  s.textContent = `viewer -> Photon: ${d.status}${d.ms ? `, ${d.ms.toFixed(0)} ms` : ''}${d.n != null ? `, ${d.n} features` : ''}${d.error ? `, ${d.error}` : ''}`
  const a = document.createElement('a')
  a.href = d.url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = d.url; a.style.color = 'var(--accent)'
  el.append(s, document.createElement('br'), a)
})
function setGeoDirect(on) {
  geoDirect = on
  try { localStorage.setItem(DIRECT_KEY, on ? '1' : '0') } catch (e) { /* none */ }
  $('geoDebug').classList.toggle('hidden', !on)
  if (on) $('geoDebug').textContent = 'viewer -> Photon: no request yet'
  where.reset()
  $('location').textContent = ''
}
if (geoDirect) setGeoDirect(true)
const geocodeQuery = (q, lat, lon) => geoDirect ? direct.query(q, lat, lon) : search.query(q, lat, lon)
$('credit').textContent = gc.ATTRIBUTION
const input = $('where'), list = $('results')
let results = [], sel = -1, searchSeq = 0, searchTimer = null

function biasPoint() {
  if (minimap.cam) return [minimap.cam.lat, minimap.cam.lon]
  return minimap.z >= 5 ? [minimap.lat, minimap.lon] : [null, null]
}

function showResults(rs) {
  results = rs
  sel = -1
  list.textContent = ''
  rs.forEach((r, k) => {
    const li = document.createElement('li')
    li.setAttribute('role', 'option')
    li.id = `result-${k}`
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = r.recent ? 'recent' : (r.kind || '')
    const name = document.createElement('span'); name.className = 'name'; name.textContent = r.name
    const detail = document.createElement('span'); detail.className = 'detail'; detail.textContent = r.detail || ''
    li.append(tag, name, detail)
    li.addEventListener('mousedown', e => { e.preventDefault(); choose(r) })
    list.append(li)
  })
  list.classList.toggle('hidden', !rs.length)
  input.setAttribute('aria-expanded', rs.length ? 'true' : 'false')
}

function highlight(k) {
  sel = k
  list.querySelectorAll('li').forEach((li, i) => li.setAttribute('aria-selected', i === k ? 'true' : 'false'))
  if (k >= 0) { list.children[k]?.scrollIntoView({ block: 'nearest' }); input.setAttribute('aria-activedescendant', `result-${k}`) }
}

function choose(r) {
  showResults([])
  input.value = r.name
  $('searchMsg').textContent = r.detail ? `${r.name}, ${r.detail}` : ''
  teleport(r.lat, r.lon, r)
}

// 300 ms after the last key, 3+ characters (EarthMap.vue liveSearch): recent places at once, then earthd's answer
function liveSearch() {
  const text = input.value.trim()
  const seq = ++searchSeq
  $('searchMsg').textContent = ''
  const local = text ? gc.recentHits(text) : []
  showResults(local)
  if (text.length < gc.MIN_CHARS || gc.parseLatLon(text)) return
  $('searchMsg').textContent = 'Searching...'
  const [lat, lon] = biasPoint()
  geocodeQuery(text, lat, lon).then(r => {
    if (seq !== searchSeq) return
    showResults([...local, ...r.results])
    $('searchMsg').textContent = results.length ? '' : (r.error || r.text) ? gc.errorText(r) : 'Nothing found'
    if (r.error) console.warn('geocode:', r.error)
  }, e => {
    if (seq !== searchSeq) return
    $('searchMsg').textContent = local.length ? '' : e.message
  })
}

input.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(liveSearch, gc.DEBOUNCE_MS) })
input.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!results.length) return
    e.preventDefault()
    highlight((sel + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length)
  } else if (e.key === 'Escape') {
    showResults([])
  }
})
input.addEventListener('blur', () => setTimeout(() => showResults([]), 150))
input.addEventListener('focus', () => { if (input.value.trim().length >= gc.MIN_CHARS && !results.length) liveSearch() })

$('goto').addEventListener('submit', e => {
  e.preventDefault()
  const text = input.value.trim()
  if (!text) return
  const ll = gc.parseLatLon(text)
  if (ll) { showResults([]); $('searchMsg').textContent = ''; teleport(ll[0], ll[1]); return }
  if (results.length) { choose(results[Math.max(0, sel)]); return }
  clearTimeout(searchTimer)
  liveSearch()
})

world.on((ev, d) => {
  if (ev === 'geocode_result') { search.onResult(d); return }
  if (ev === 'where_result') { where.onResult(d); $('location').textContent = where.text(); return }
  if (ev === 'hello') {
    where.reset()
    const p = lastPlace()
    if (p && !world.aid) teleport(p[0], p[1])
  } else if (ev === 'teleported') {
    camera.position.set(d.x, d.y, d.z)
    controls.apply()
    minimap.pending = null
    minimap.follow = true
    if (minimap.z < 13) minimap.z = 15
    osm.clear()
    pick(null)
  } else if (ev === 'rebased') {
    camera.position.set(d.x, d.y, d.z)
    controls.apply()
    osm.dirty = true
  } else if (ev === 'far_done' || ev === 'block' || ev === 'anchor') {
    osm.dirty = true
  }
})

// --- layers ---
const LAYERS = [
  ['Ground (far tiles + imagery)', () => world.layers.ground, true],
  ['Physics block (heightmap)', () => world.layers.block, false],
  ['Road surfaces and sidewalks', () => world.layers.roads, true],
  ['Buildings, decks, wires', () => world.layers.content, true],
  ['Far buildings and skyline', () => world.layers.farbld, true],
  ['Street furniture (props)', () => world.layers.props, true],
  ['Guard rails and barriers', () => world.layers.rails, true],
  ['Highway signs (exits, mileposts, merges, speed limits)', () => world.layers.signs, true],
  ['OSM ways', () => osm.ways, true],
  ['OSM nodes (connectors)', () => osm.nodes, true],
]
for (const [label, get, on] of LAYERS) {
  const l = document.createElement('label')
  const cb = document.createElement('input')
  cb.type = 'checkbox'
  cb.checked = on
  get().visible = on
  cb.onchange = () => { get().visible = cb.checked }
  l.append(cb, label)
  $('layers').append(l)
}
for (const [label, fn, on] of [['OSM drawn on top', on => osm.setOnTop(on)], ['Wireframe', on => setWireframe(on)],
  ["Game materials (the game's textures; off: flat colours by kind)", setGameMaterials, matSettings.game],
  ['Place names online (Photon)', on => { where.online = on; where.reset(); if (!on) $('location').textContent = '' }, true],
  ['Geocode in the viewer (debug: Photon from this page, not earthd)', setGeoDirect, geoDirect]]) {
  const l = document.createElement('label')
  const cb = document.createElement('input')
  cb.type = 'checkbox'
  cb.checked = !!on
  cb.onchange = () => fn(cb.checked)
  l.append(cb, label)
  $('layers').append(l)
}

// the time of day, Night, shadows and lamp lights controls (daylight_ui.js)
const daylight = daylightPanel({ sky, lights, minimap, parent: $('layers') })

// --- inspecting ---
const ray = new THREE.Raycaster()
function showPick(title, rows) {
  $('pickTitle').textContent = title
  const dl = $('pickBody')
  dl.textContent = ''
  for (const [k, v] of rows) {
    if (v == null || v === '') continue
    const dt = document.createElement('dt'); dt.textContent = k
    const dd = document.createElement('dd')
    if (v instanceof Node) dd.append(v); else dd.textContent = typeof v === 'object' ? JSON.stringify(v) : String(v)
    dl.append(dt, dd)
  }
  $('pick').classList.remove('hidden')
}
$('pickClose').onclick = () => pick(null)

function placeRows(p) {
  const a = world.anchor
  if (!a || !p) return []
  const [lat, lon] = a.worldToGeodetic(p.x, p.y, p.z)
  return [['lat, lon', `${lat.toFixed(6)}, ${lon.toFixed(6)}`], ['world', `${p.x.toFixed(2)}, ${p.y.toFixed(2)}, ${p.z.toFixed(2)}`]]
}

function osmLink(rec) {
  const m = /^([wnr])(\d+)/.exec(rec || '')
  if (!m) return rec
  const a = document.createElement('a')
  a.href = `https://www.openstreetmap.org/${{ w: 'way', n: 'node', r: 'relation' }[m[1]]}/${m[2]}`
  a.target = '_blank'
  a.rel = 'noopener'
  a.textContent = rec
  a.style.color = 'var(--accent)'
  return a
}

function pick(e) {
  if (!e) { $('pick').classList.add('hidden'); osm.highlight(null); return }
  const r = canvas.getBoundingClientRect()
  const ndc = new THREE.Vector2((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1)
  ray.setFromCamera(ndc, camera)
  const o = osm.pick(ray)
  if (o && o.kind === 'way') {
    const t = o.item.tags
    osm.highlight(o.item)
    const rows = [['class', t.class], ['subtype', t.subtype], ['name', t.name],
      ['OSM', t.src_dataset === 'OpenStreetMap' ? osmLink(t.src_record) : t.src_record], ['Overture id', o.item.id]]
    for (const [k, v] of Object.entries(t)) {
      if (!['class', 'subtype', 'name', 'src_record', 'src_dataset'].includes(k)) rows.push([k, v])
    }
    for (const [k, v] of Object.entries(o.item.rules || {})) rows.push([`rule ${k}`, v])
    rows.push(['vertices', o.item.g.reduce((s, p) => s + p.length / 2, 0)])
    showPick(t.name || t.class || 'way', [...rows, ...placeRows(o.point)])
    return
  }
  if (o && o.kind === 'node') {
    osm.highlight(null)
    showPick('connector', [['Overture id', o.item.id], ...placeRows(o.point)])
    return
  }
  osm.highlight(null)
  const targets = [world.layers.signs, world.layers.props, world.layers.rails, world.layers.content, world.layers.roads, world.layers.farbld, world.layers.ground].filter(g => g.visible)
  const hit = ray.intersectObjects(targets, true)[0]
  if (!hit) { pick(null); return }
  const prop = itemOf(hit)
  if (prop && prop.row) {
    const r = prop.row
    showPick(r.g, [['shape', prop.shape + (hit.object.userData.marker ? ' (not compiled by the game yet: a marker)' : '.cdae')],
      ['tile', prop.tile], ['height', `${r.h} m (shape ${r.h0} m, scale ${r.sc.toFixed(3)})`], ['yaw', `${r.yaw}°`],
      ['rank', r.rank], ['row', `${r.x.toFixed(2)}, ${r.y.toFixed(2)}, ${r.z.toFixed(2)}`],
      ['distance', `${hit.distance.toFixed(1)} m`], ...placeRows(hit.point)])
    return
  }
  let obj = hit.object, kind = ''
  while (obj && !kind) { kind = obj.userData.kind || ''; if (!kind) obj = obj.parent }
  const mats = Array.isArray(hit.object.material) ? hit.object.material : [hit.object.material]
  const mat = hit.face && mats[hit.face.materialIndex]
  const g = hit.object.geometry
  showPick(kind || hit.object.name || 'shape', [
    ['object', obj?.name || hit.object.name], ['triangles', g.index ? g.index.count / 3 : ''],
    ['material color', mat?.color ? `#${mat.color.getHexString()}` : ''], ['distance', `${hit.distance.toFixed(1)} m`],
    ...placeRows(hit.point)])
}

// --- loop ---
let last = performance.now(), slow = 0, slower = 0
const drawSize = new THREE.Vector2()
function frame(now) {
  requestAnimationFrame(frame)
  const dt = Math.min(0.1, (now - last) / 1000)
  last = now
  const p = camera.position
  const ground = world.anchor ? world.heightAt(p.x, p.y) : null
  const agl = ground == null ? null : p.z - ground
  controls.update(dt, agl)
  world.update(dt, camera)
  // the near plane from the height above the ground (depth precision for the road markings up close)
  const h = Math.max(1, Math.abs(agl ?? p.z))
  camera.near = Math.min(50, Math.max(0.3, h / 200))
  camera.far = Math.max(200000, h * 300)
  camera.updateProjectionMatrix()
  slow += dt
  if (slow > 0.25) {
    slow = 0
    osm.update(p)
    world.updateSeams()
    if (world.anchor) world.props.update(p.x, p.y)
    ui(agl)
    daylight.text()
  }
  slower += dt
  if (slower > 5 && world.lat != null) { slower = 0; remember(world.lat, world.lon) }
  if (world.anchor) {
    const [lat, lon] = world.anchor.worldToGeodetic(p.x, p.y, 0)
    minimap.cam = { lat, lon, heading: controls.heading, fov: camera.fov * camera.aspect }
    // the location line: the place under the camera (statusbar.lua's timing), not during an anchor change
    if (!world.pending) {
      const q = where.request(now / 1000, lat, lon)
      if (q && geoDirect) {
        // the street from earthd's map data still (not a geocoder's), the place names asked by this page
        const placeDue = q.online
        q.online = false
        if (!net.send(q)) where.last = null
        if (placeDue) {
          direct.reverse(lat, lon).then(place => {
            if (!place || !geoDirect) return
            where.onResult({ ...place })
            $('location').textContent = where.text()
          })
        }
      } else if (q && !net.send(q)) where.last = null
    }
    const b = world.block
    minimap.block = b && b.aid === world.aid
      ? [[b.x0, b.y0], [b.x0 + (b.N - 1) * b.s, b.y0], [b.x0 + (b.N - 1) * b.s, b.y0 + (b.N - 1) * b.s], [b.x0, b.y0 + (b.N - 1) * b.s]]
        .map(([x, y]) => world.anchor.worldToGeodetic(x, y, 0).slice(0, 2))
      : null
  }
  {
    const ll = world.anchor ? world.anchor.worldToGeodetic(p.x, p.y, 0) : [null, null]
    const night = sky.update(now / 1000, camera, ll[0], ll[1], agl, ground)
    lights.setNight(night)
    if (sky.timeOfDay) daylight.night.checked = night
  }
  lights.update(now / 1000, camera, renderer.getDrawingBufferSize(drawSize).y)
  minimap.draw()
  renderer.render(scene, camera)
}
requestAnimationFrame(frame)

function ui(agl) {
  $('conn').textContent = net.connected
    ? `earthd ${world.hello ? `session ${world.sessionId}, ${world.hello.nearVisual || ''} mode` : 'connected'}`
    : 'earthd not connected (start it: run_server.bat / start.py)'
  $('status').textContent = world.status
  const c = world.counts(), o = osm.counts(), p = camera.position
  const rows = []
  if (world.anchor) {
    rows.push(['anchor', `${world.aid} (${world.anchor.lat.toFixed(4)}, ${world.anchor.lon.toFixed(4)})`])
    if (minimap.cam) rows.push(['camera', `${minimap.cam.lat.toFixed(5)}, ${minimap.cam.lon.toFixed(5)}`])
    rows.push(['height', `${p.z.toFixed(0)} m${agl != null ? `, ${agl.toFixed(0)} m above ground` : ''}`])
  }
  rows.push(['far tiles', `${c.far} shown, ${c.farLoading} loading, ${c.swaps} swaps waiting`])
  rows.push(['road tiles', `${c.roads} (far: ${c.farGeo})`])
  const ps = world.props.stats
  rows.push(['props', `${ps.shown} of ${ps.items} shown in ${ps.tiles} tiles; ${ps.shapes} shapes${ps.missing.size ? `, ${ps.missing.size} as markers` : ''}`])
  let signs = 0
  world.layers.signs.traverse(o => { if (o.userData.kind?.endsWith(' signs')) signs++ })
  rows.push(['signs', `${signs} sign shapes shown`])
  if (lights.night) rows.push(['night', `${lights.stats.lit} of ${lights.pool.length} lamp lights on; ${lights.stats.lenses} lamps in the road tiles`])
  const ts = textureStats
  rows.push(['materials', `${materialStats.game} from the game's definitions (${modMaterialCount} in the mod), ${materialStats.rules} by name` +
    `${materialStats.pending ? `, ${materialStats.pending} textures loading` : ''}`])
  rows.push(['textures', `${ts.loaded} (${(ts.bytes / 1e6).toFixed(0)} MB; ${Object.entries(ts.formats).map(([k, v]) => `${k} ${v}`).join(', ')})` +
    `${ts.failed ? `, ${ts.failed} missing` : ''}${ts.unsupported ? `, ${ts.unsupported} in formats this browser lacks` : ''}`])
  rows.push(['OSM', `${o.ways} ways in ${o.ok} cells${o.missing ? `, ${o.missing} not cached yet` : ''}`])
  rows.push(['files', `${stats.shapes} shapes, ${(stats.bytes / 1e6).toFixed(1)} MB, ${c.queue} queued`])
  if (stats.failed) rows.push(['failed', `${stats.failed}: ${stats.lastError}`])
  rows.push(['speed', `x${controls.speedScale.toFixed(2)}`])
  const dl = $('info')
  dl.textContent = ''
  for (const [k, v] of rows) {
    const dt = document.createElement('dt'); dt.textContent = k
    const dd = document.createElement('dd'); dd.textContent = v
    dl.append(dt, dd)
  }
}
