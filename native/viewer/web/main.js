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
import { Offline } from './offline.js'

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
const viewerConfig = await fetch('/api/config').then(r => r.json()).catch(() => ({}))
let world
const net = new Net(msg => world.onMessage(msg))
world = new World(net, root)
// the guide sign while earthd can't be reached, with the button that starts it (offline.js)
const offline = new Offline(net, viewerConfig)
const osm = new Osm(world, root)
const controls = new FlyControls(camera, canvas, pick)
const minimap = new Minimap($('minimap'), (lat, lon) => teleport(lat, lon))
minimap.z = 3
$('centre').onclick = () => minimap.centre()

// --- the wide map: the minimap as a strip across the bottom, its height dragged on its top edge ---
// (remembered in this browser: { wide, h: the fraction of the window's height })
const MAP_KEY = 'bnge-viewer-map', MAP_H = 0.33, MAP_MIN = 0.15, MAP_MAX = 0.8
const mapView = { wide: false, h: MAP_H }
try { Object.assign(mapView, JSON.parse(localStorage.getItem(MAP_KEY)) || {}) } catch (e) { /* none */ }
function saveMapView() { try { localStorage.setItem(MAP_KEY, JSON.stringify(mapView)) } catch (e) { /* none */ } }
function setMapHeight(h) {
  mapView.h = Math.min(MAP_MAX, Math.max(MAP_MIN, Number.isFinite(h) ? h : MAP_H))
  document.documentElement.style.setProperty('--map-h', `${(mapView.h * 100).toFixed(1)}vh`)
  $('mapGrip').setAttribute('aria-valuenow', Math.round(mapView.h * 100))
}
function setMapWide(on) {
  mapView.wide = on
  document.body.classList.toggle('mapWide', on)
  const b = $('mapWide')
  b.setAttribute('aria-pressed', on ? 'true' : 'false')
  b.title = on ? 'Shrink the map (M)' : 'Widen the map (M)'
  b.setAttribute('aria-label', on ? 'Shrink the map' : 'Widen the map')
  $('mapGrip').tabIndex = on ? 0 : -1
  saveMapView()
}
setMapHeight(mapView.h)
setMapWide(!!mapView.wide)
$('mapWide').onclick = () => setMapWide(!mapView.wide)
{
  const grip = $('mapGrip')
  let held = false
  // the strip's top edge follows the pointer (its bottom stays a gap above the window's)
  const follow = e => {
    const gap = $('mapbox').getBoundingClientRect().bottom
    setMapHeight((gap - e.clientY) / window.innerHeight)
  }
  grip.addEventListener('pointerdown', e => {
    held = true
    grip.setPointerCapture(e.pointerId)
    grip.classList.add('held')
    document.body.classList.add('resizing')
    e.preventDefault()
  })
  grip.addEventListener('pointermove', e => { if (held) follow(e) })
  const release = () => {
    if (!held) return
    held = false
    grip.classList.remove('held')
    document.body.classList.remove('resizing')
    saveMapView()
  }
  grip.addEventListener('pointerup', release)
  grip.addEventListener('pointercancel', release)
  grip.addEventListener('dblclick', () => { setMapHeight(MAP_H); saveMapView() })
  grip.addEventListener('keydown', e => {
    const step = { ArrowUp: 0.02, ArrowDown: -0.02, PageUp: 0.1, PageDown: -0.1 }[e.key]
    if (step == null && e.key !== 'Home') return
    e.preventDefault()
    e.stopPropagation()
    setMapHeight(e.key === 'Home' ? MAP_H : mapView.h + step)
    saveMapView()
  })
}
// "M" widens or shrinks the map
window.addEventListener('keydown', e => {
  if (e.code !== 'KeyM' || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement) return
  setMapWide(!mapView.wide)
})
// night lighting (lighting.js: nightlights.lua's windows, lamp lenses, far glow and street lamp lights)
// and the sun, sky and shadows (sky.js: the time of day)
const lights = new NightLights(root, world.props)
const sky = new Sky(scene, root, renderer, hemi, sun)
window.viewer = { camera, controls, world, osm, minimap, scene, lights, sky, offline }      // (for the browser console)

const LAST = 'bnge-viewer-last'
function remember(lat, lon) { try { localStorage.setItem(LAST, JSON.stringify([lat, lon])) } catch (e) { /* none */ } }
function lastPlace() {
  const m = /^#(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)$/.exec(decodeURIComponent(location.hash))
  if (m) return [+m[1], +m[2]]
  try { const v = JSON.parse(localStorage.getItem(LAST)); return Array.isArray(v) ? v : null } catch (e) { return null }
}

function teleport(lat, lon, place = null) {
  if (!world.hello) { world.status = 'Waiting for the world server'; return }
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
  a.href = d.url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = d.url; a.style.color = 'var(--water)'
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
const geocodeQuery = (q, lat, lon, full) => geoDirect ? direct.query(q, lat, lon) : search.query(q, lat, lon, 15000, full)
$('credit').textContent = gc.ATTRIBUTION
const input = $('where'), list = $('results')
let results = [], sel = -1, searchSeq = 0, searchTimer = null, fullText = ''

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
function liveSearch(full = false) {
  const text = input.value.trim()
  const seq = ++searchSeq
  fullText = ''
  $('searchMsg').textContent = ''
  const local = text ? gc.recentHits(text) : []
  showResults(local)
  if (text.length < gc.MIN_CHARS || gc.parseLatLon(text)) return
  $('searchMsg').textContent = 'Searching...'
  const [lat, lon] = biasPoint()
  geocodeQuery(text, lat, lon, full === true).then(r => {
    if (seq !== searchSeq) return
    if (full === true) fullText = text
    showResults([...local, ...r.results])
    $('searchMsg').textContent = results.length ? '' : (r.error || r.text) ? gc.errorText(r) : full === true ? 'Nothing found' : 'Nothing yet: press Enter to search streets and addresses too'
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
  // Enter: the full search too (streets and house numbers), unless earthd is sure of the first line; then the first line
  if (results.length && (sel >= 0 || fullText === text || results[0].sure)) { choose(results[Math.max(0, sel)]); return }
  if (fullText !== text) { clearTimeout(searchTimer); liveSearch(true); return }
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
// [group, label, detail, the object, on]
const LAYERS = [
  ['layersWorld', 'Ground', 'Far tiles with imagery', () => world.layers.ground, true],
  ['layersWorld', 'Physics block', 'The heightmap the tyres drive on', () => world.layers.block, false],
  ['layersWorld', 'Roads and sidewalks', '', () => world.layers.roads, true],
  ['layersWorld', 'Buildings, decks and wires', '', () => world.layers.content, true],
  ['layersWorld', 'Far buildings and skyline', '', () => world.layers.farbld, true],
  ['layersWorld', 'Street furniture', 'Lights, poles, hydrants, benches, signals', () => world.layers.props, true],
  ['layersWorld', 'Guard rails and barriers', '', () => world.layers.rails, true],
  ['layersWorld', 'Highway signs', 'Exits, mileposts, merges, speed limits', () => world.layers.signs, true],
  ['layersOsm', 'OSM roads', 'Click one to see its tags', () => osm.ways, true],
  ['layersOsm', 'Connectors', 'Overture road nodes', () => osm.nodes, true],
]
// a switch row: the box, its label and an optional line of detail (also daylight_ui.js)
function option(label, detail, on, fn) {
  const l = document.createElement('label')
  l.className = 'opt'
  const cb = document.createElement('input')
  cb.type = 'checkbox'
  cb.checked = !!on
  cb.onchange = () => fn(cb.checked)
  const t = document.createElement('span')
  t.textContent = label
  if (detail) { const d = document.createElement('small'); d.textContent = detail; t.append(d) }
  l.append(cb, t)
  return l
}
for (const [group, label, detail, get, on] of LAYERS) {
  get().visible = on
  $(group).append(option(label, detail, on, v => { get().visible = v }))
}
for (const [group, label, detail, fn, on] of [
  ['layersOsm', 'Draw OSM on top', 'Through the ground and buildings', on => osm.setOnTop(on), false],
  ['layersRender', 'Game materials', "The game's own textures. Off: flat colours by kind", setGameMaterials, matSettings.game],
  ['layersRender', 'Wireframe', '', on => setWireframe(on), false],
  ['layersPlaces', 'Place names online', 'Town, region and country from Photon', on => { where.online = on; where.reset(); if (!on) $('location').textContent = '' }, true],
  ['layersPlaces', 'Search from this page', 'For debugging: ask Photon directly, not through the world server', setGeoDirect, geoDirect]]) {
  $(group).append(option(label, detail, on, fn))
}

// "/" goes to the search box, as on most map sites
window.addEventListener('keydown', e => {
  if (e.key !== '/' || e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
  e.preventDefault()
  input.focus()
  input.select()
})

// the time of day, Night, shadows and lamp lights controls (daylight_ui.js)
const daylight = daylightPanel({ sky, lights, minimap, parent: $('light'), option })

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
  a.style.color = 'var(--water)'
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
  offline.update()
  minimap.offline = !net.connected
  if (world.hello && net.connected) $('conn').title = `World server ${viewerConfig.earthd || ''}: session ${world.sessionId}, ${world.hello.nearVisual || ''} mode`
  $('status').textContent = net.connected ? world.status : ''
  const c = world.counts(), o = osm.counts(), p = camera.position
  const rows = []
  $('infoEmpty').classList.toggle('hidden', net.connected)
  if (!net.connected) { $('info').textContent = ''; return }
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
