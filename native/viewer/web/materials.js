// Materials for the shapes' material names. The game's own materials (the mod's textures) are not
// loaded: each name gets a flat colour by kind; buildings keep their per-vertex colours (the shapes'
// `colors`, the facade tint the server chose). Far tiles get their own DDS imagery (world.js).
import * as THREE from 'three'

const RULES = [
  // (the game's stock shapes, the props: trees, glass, signs, lamps)
  [/leaves|leaf|foliage|needle|branch|bush|palm_frond|grass_card/i, 0x4f6b32, ''],
  [/bark|trunk/i, 0x5a4636, ''],
  [/glass|lens|window/i, 0xb9d3e0, ''],
  [/glow|light_on|emissive/i, 0xfff2b0, ''],
  [/trafficlight|signal/i, 0x2b2f33, ''],
  [/sign_stop|stop/i, 0xb3261e, ''],
  [/hydrant/i, 0xc9a227, ''],
  [/chainlink|fence/i, 0x9a9a92, ''],
  [/guardrail|barrier|jersey/i, 0xb5b8ba, ''],
  [/wood/i, 0x8a6a48, ''],
  [/line_yellow|yellow/i, 0xe8c12c, 'mark'],
  [/line_white|marking|white|zebra|stop_?line|arrow/i, 0xf2f2f2, 'mark'],
  [/tactile/i, 0xd9b84a, 'mark'],
  [/edge_dirt|dirt|gravel|soil|track|mud/i, 0x8a7350, ''],
  [/curb|sidewalk|walk|plaza|concrete|pave/i, 0xb5b2aa, ''],
  [/asphalt|road|tarmac|runway|parking/i, 0x4a4a4d, ''],
  [/roof_flat/i, 0x8c8c8c, ''],
  [/roof_slate/i, 0x5a5f66, ''],
  [/roof_shingle/i, 0x6e4f3c, ''],
  [/roof_metal/i, 0x9aa3a8, ''],
  [/roof/i, 0x7a5a48, ''],
  [/bld|facade|siding|stucco|shop|marble|brick|window/i, 0xffffff, 'bld'],
  [/wire|cable/i, 0x111111, ''],
  [/bridge|deck/i, 0x9c9a94, ''],
  [/rail/i, 0x5b4a3a, ''],
  [/guide|exit|sign/i, 0x1f6b3a, ''],
  [/metal|steel|pole/i, 0x8d9296, ''],
  [/grass|lawn/i, 0x5d7d3b, ''],
  [/water/i, 0x2c5d7a, ''],
]

const cache = new Map()
export const settings = { wireframe: false }

// --- the mod's own materials (its *.materials.json, merged by /api/materials) ---------------------------------
// Used as the game would where a material needs nothing outside the mod folder: its base colour (baseColorFactor,
// linear; v1 materials: diffuseColor), its textures from the mod folder (base colour, opacity with alpha test),
// vertex colours. A material whose base colour map lies elsewhere (the game's /assets/...) keeps the name rules.
let modDefs = {}
const MOD_PREFIX = '/levels/beamng_earth/'
export const materialStats = { mod: 0, rules: 0 }

export async function loadModMaterials() {
  try {
    const r = await fetch('/api/materials')
    if (r.ok) modDefs = await r.json()
  } catch (e) {
    console.warn('mod materials:', e)
  }
  return Object.keys(modDefs).length
}

const texLoader = new THREE.TextureLoader()
const texCache = new Map()
function modTexture(path, color) {
  const key = `${path}|${color ? 1 : 0}`
  let t = texCache.get(key)
  if (!t) {
    t = texLoader.load(`/game${encodeURI(path)}`)
    t.flipY = false                 // (the shapes' v is the engine's 1 - v: row 0 at v = 0)
    t.wrapS = t.wrapT = THREE.RepeatWrapping
    t.anisotropy = 4
    if (color) t.colorSpace = THREE.SRGBColorSpace
    texCache.set(key, t)
  }
  return t
}

const isMod = p => typeof p === 'string' && p.startsWith(MOD_PREFIX)

function fromDef(def, hasColors) {
  const s = (def.Stages && def.Stages[0]) || {}
  const colorMap = s.baseColorMap || s.colorMap
  if (colorMap && !isMod(colorMap)) return null          // (a texture the viewer does not load: the name rules)
  const m = new THREE.MeshLambertMaterial({ side: THREE.DoubleSide, wireframe: settings.wireframe })
  const f = Array.isArray(s.baseColorFactor) ? s.baseColorFactor : Array.isArray(s.diffuseColor) ? s.diffuseColor : null
  if (f) m.color.setRGB(+f[0], +f[1], +f[2], THREE.LinearSRGBColorSpace)
  if (colorMap) m.map = modTexture(colorMap, true)
  if (isMod(s.opacityMap)) {
    m.alphaMap = modTexture(s.opacityMap, false)
    if (def.alphaTest) m.alphaTest = (+def.alphaRef || 1) / 255
    else m.transparent = true
  }
  if (s.vertColor && hasColors) m.vertexColors = true
  m.userData.shared = true
  m.userData.mod = true
  return m
}

// --- night lighting (nightlights.lua M.MATERIALS): the materials with a night glow ------------------------------
// The buildings' windows (their night emissive maps, tools/build_night_windows.py) and the street lamps' lenses: dark
// by day (emissiveFactor 0 in the mod's materials), the glow colour after dusk. The game's brightness in nits becomes
// three.js's emissive intensity over NITS_FULL (the windows' 200 nits: full), at most 1: the ratios between them kept,
// the lens (4000 nits) at full.
export const NIGHT_MATERIALS = {
  bne_bld_facade: [[1, 1, 1], 200],
  bne_bld_office: [[0.95, 0.97, 1], 70],
  bne_bld_shop: [[1, 0.80, 0.58], 30],
  bne_bld_sky: [[1, 1, 1], 200],
  bne_lamp_lens: [[1, 0.66, 0.33], 4000],
}
const NITS_FULL = 200
const nightMats = []
let night = false

function applyNight(m) {
  const [c, nits] = m.userData.glow
  if (night) m.emissive.setRGB(c[0], c[1], c[2], THREE.LinearSRGBColorSpace)
  else m.emissive.setRGB(0, 0, 0)
  m.emissiveIntensity = Math.min(1, nits / NITS_FULL)
}

function withNight(name, m) {
  const glow = NIGHT_MATERIALS[name]
  if (!glow || !m.emissive) return m
  const s = (modDefs[name]?.Stages && modDefs[name].Stages[0]) || {}
  if (isMod(s.emissiveMap)) m.emissiveMap = modTexture(s.emissiveMap, true)
  m.userData.glow = glow
  nightMats.push(m)
  applyNight(m)
  return m
}

export function setNight(on) {
  night = !!on
  for (const m of nightMats) applyNight(m)
}

export function forName(name, hasColors) {
  const key = `${name}|${hasColors ? 1 : 0}|${settings.wireframe ? 1 : 0}`
  let m = cache.get(key)
  if (m) return m
  const def = modDefs[name]
  if (def) {
    m = fromDef(def, hasColors)
    if (m) { materialStats.mod++; cache.set(key, withNight(name, m)); return m }
  }
  materialStats.rules++
  let color = 0xc8c3b8, kind = ''
  for (const [re, c, k] of RULES) if (re.test(name)) { color = c; kind = k; break }
  m = new THREE.MeshLambertMaterial({
    color, side: THREE.DoubleSide, vertexColors: !!hasColors && kind === 'bld', wireframe: settings.wireframe,
  })
  if (kind === 'mark') { m.polygonOffset = true; m.polygonOffsetFactor = -2; m.polygonOffsetUnits = -4 }
  m.userData.shared = true
  cache.set(key, withNight(name, m))
  return m
}

export function setWireframe(on) {
  settings.wireframe = on
  for (const m of cache.values()) m.wireframe = on
}

export const farPalette = new THREE.MeshLambertMaterial({ color: 0x8d8a6a, side: THREE.DoubleSide })
farPalette.userData.shared = true

// THREE geometries of a shape's drawn meshes (cdae.drawnMeshes) with their materials
export function meshesOf(drawn, materialNames, makeMaterial) {
  const out = []
  for (const d of drawn) {
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(d.positions, 3))
    if (d.normals) g.setAttribute('normal', new THREE.BufferAttribute(d.normals, 3))
    if (d.uv) g.setAttribute('uv', new THREE.BufferAttribute(d.uv, 2))
    if (d.uv2) g.setAttribute('uv1', new THREE.BufferAttribute(d.uv2, 2))
    if (d.colors) g.setAttribute('color', new THREE.BufferAttribute(d.colors, 4, true))
    g.setIndex(new THREE.BufferAttribute(d.index, 1))
    if (!d.normals) g.computeVertexNormals()
    const mats = []
    d.groups.forEach((gr, k) => {
      g.addGroup(gr.start, gr.count, k)
      const name = gr.material >= 0 ? materialNames[gr.material] || '' : ''
      mats.push(makeMaterial ? makeMaterial(name, !!d.colors) : forName(name, !!d.colors))
    })
    if (!d.groups.length) { g.addGroup(0, d.index.length, 0); mats.push(forName('', false)) }
    g.computeBoundingSphere()
    out.push(new THREE.Mesh(g, mats))
  }
  return out
}

// free an object's geometries and its own (not shared) materials and textures
export function dispose(obj) {
  obj.traverse(o => {
    if (o.geometry) o.geometry.dispose()
    const ms = Array.isArray(o.material) ? o.material : o.material ? [o.material] : []
    for (const m of ms) {
      if (m.userData.shared) continue
      if (m.map) m.map.dispose()
      m.dispose()
    }
  })
}
