// Materials for the shapes' material names, as the game draws them: each name's definition (the mod's
// *.materials.json, else the game's: /api/material, game.rs picks the one the engine would for the shape) made a PBR
// material with the definition's textures (textures.js: the game's DDS files) and factors: base colour (and vertex
// colours: the buildings' facade tint), normal, roughness, metallic, ambient occlusion, opacity with alpha test or
// translucency, emissive, double-sidedness. Until its definition and textures arrive, and where there is none (or with
// "Game materials" off), a name gets a flat colour by its kind (RULES). Far tiles get their own DDS imagery (world.js).
//
// The game's data maps are single channel (BC4: red) and its normal maps two (BC5: red, green), so the shaders read
// roughness, metallic and opacity from red and rebuild the normal's z (patch). The shapes' v is the engine's 1 - v, so
// no texture is flipped, and v runs down the image. The game's normal maps are OpenGL-style (green up the image:
// measured on the plaster and oak bark maps, by the correlation of their red and green slopes), so green is negated.
import * as THREE from 'three'
import { texture } from './textures.js'

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

const cache = new Map()        // name|hasColors -> material
// wireframe: drawn as lines; game: the game's definitions and textures (off: the flat colours by kind)
export const settings = { wireframe: false, game: true }
export const materialStats = { game: 0, rules: 0, pending: 0 }

// --- the definitions ------------------------------------------------------------------------------------------------
// The mod's (loaded before any shape is made: its level's, the engine's first), else asked of the server by name.
let modDefs = {}
export async function loadModMaterials() {
  try {
    const r = await fetch('/api/materials')
    if (r.ok) modDefs = await r.json()
  } catch (e) {
    console.warn('mod materials:', e)
  }
  return Object.keys(modDefs).length
}

const defCache = new Map()     // lowercased name -> Promise<definition | null>
function definition(name, near) {
  if (!name) return Promise.resolve(null)
  if (modDefs[name]) return Promise.resolve(modDefs[name])
  const k = name.toLowerCase()
  let p = defCache.get(k)
  if (!p) {
    p = fetch(`/api/material?name=${encodeURIComponent(name)}&near=${encodeURIComponent(near || '')}`)
      .then(r => r.ok ? r.json() : null).catch(() => null)
    defCache.set(k, p)
  }
  return p
}

// a texture path of a definition: absolute in the game's file system, or relative to its file's folder
function pathOf(def, p) {
  if (typeof p !== 'string' || !p.trim()) return null
  p = p.trim().replace(/\\/g, '/')
  if (p.startsWith('/')) return p
  if (/^(art|levels|assets|vehicles)\//i.test(p)) return `/${p}`
  return `${def.__dir || ''}/${p}`
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
  if (!glow) return m
  m.userData.glow = glow
  const s = (modDefs[name]?.Stages && modDefs[name].Stages[0]) || {}
  const p = pathOf(modDefs[name] || {}, s.emissiveMap)
  if (p) texture(p, true).then(t => { if (t) { m.emissiveMap = t; m.needsUpdate = true } })
  nightMats.push(m)
  applyNight(m)
  return m
}

export function setNight(on) {
  night = !!on
  for (const m of nightMats) applyNight(m)
}

// --- the shaders: single-channel data maps, two-channel normal maps -----------------------------------------------
const C = THREE.ShaderChunk
const FRAG = {
  '#include <roughnessmap_fragment>': C.roughnessmap_fragment.replace('texelRoughness.g', 'texelRoughness.r'),
  '#include <metalnessmap_fragment>': C.metalnessmap_fragment.replace('texelMetalness.b', 'texelMetalness.r'),
  '#include <alphamap_fragment>': C.alphamap_fragment.replace('vAlphaMapUv ).g', 'vAlphaMapUv ).r'),
  '#include <normal_fragment_maps>': C.normal_fragment_maps.replace(
    'vec3 mapN = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;',
    'vec3 mapN = vec3( texture2D( normalMap, vNormalMapUv ).xy * 2.0 - 1.0, 0.0 );\n\tmapN.y = - mapN.y;\n\tmapN.z = sqrt( max( 0.0, 1.0 - dot( mapN.xy, mapN.xy ) ) );'),
}
function patch(m) {
  m.onBeforeCompile = sh => {
    for (const [k, v] of Object.entries(FRAG)) sh.fragmentShader = sh.fragmentShader.replace(k, v)
  }
  m.customProgramCacheKey = () => 'bne-game-maps'
}

// --- a material: the flat colour by kind, then its definition ------------------------------------------------------
const MAPS = ['map', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'alphaMap']

function asRule(m) {
  const { name, hasColors } = m.userData
  let color = 0xc8c3b8, kind = ''
  for (const [re, c, k] of RULES) if (re.test(name)) { color = c; kind = k; break }
  for (const k of MAPS) m[k] = null
  if (!m.userData.glow) { m.emissiveMap = null; m.emissive.setRGB(0, 0, 0) }
  m.color.setHex(color)
  m.vertexColors = !!hasColors && kind === 'bld'
  m.roughness = 0.9
  m.metalness = 0
  m.opacity = 1
  m.alphaTest = 0
  m.alphaToCoverage = false
  m.transparent = false
  m.depthWrite = true
  m.blending = THREE.NormalBlending
  m.side = THREE.DoubleSide
  m.userData.kind = kind
  m.userData.game = false
  // (road markings over the asphalt they lie on)
  m.polygonOffset = kind === 'mark'
  m.polygonOffsetFactor = -2; m.polygonOffsetUnits = -4
  m.needsUpdate = true
}

function asGame(m, def) {
  const s = (Array.isArray(def.Stages) && def.Stages[0]) || {}
  const { hasColors } = m.userData
  const v15 = +def.version >= 1.5
  m.userData.game = true
  m.userData.def = def
  for (const k of MAPS) m[k] = null
  const f = Array.isArray(s.baseColorFactor) ? s.baseColorFactor : Array.isArray(s.diffuseColor) ? s.diffuseColor : [1, 1, 1, 1]
  m.color.setRGB(+f[0], +f[1], +f[2], THREE.LinearSRGBColorSpace)
  m.vertexColors = !!s.vertColor && !!hasColors
  const has = k => typeof s[k] === 'string' && s[k].trim() !== ''
  m.roughness = Number.isFinite(+s.roughnessFactor) && s.roughnessFactor != null ? +s.roughnessFactor : has('roughnessMap') ? 1 : v15 ? 0.6 : 0.8
  m.metalness = Number.isFinite(+s.metallicFactor) && s.metallicFactor != null ? +s.metallicFactor : has('metallicMap') ? 1 : 0
  m.side = def.doubleSided ? THREE.DoubleSide : THREE.FrontSide
  m.alphaTest = def.alphaTest ? Math.max(1, +def.alphaRef || 0) / 255 : 0
  // (alpha-tested leaves and fences: their coverage from the alpha with the canvas's multisampling, as the engine's
  // alpha-to-coverage; without it a mipmap's averaged alpha falls under the test and the leaves vanish with distance)
  m.alphaToCoverage = m.alphaTest > 0
  m.transparent = !!def.translucent
  m.depthWrite = !def.translucent || !!def.translucentZWrite
  m.blending = def.translucent && /^add/i.test(def.translucentBlendOp || '') ? THREE.AdditiveBlending : THREE.NormalBlending
  m.opacity = def.translucent && Number.isFinite(+s.opacityFactor) && s.opacityFactor != null ? +s.opacityFactor : 1
  if (!m.userData.glow) {
    const e = Array.isArray(s.emissiveFactor) ? s.emissiveFactor : null
    const lit = e && (+e[0] > 0 || +e[1] > 0 || +e[2] > 0)
    m.emissive.setRGB(lit ? +e[0] : 0, lit ? +e[1] : 0, lit ? +e[2] : 0, THREE.LinearSRGBColorSpace)
    m.emissiveIntensity = lit && +s.emissiveIntensityNits > 0 ? Math.min(1, +s.emissiveIntensityNits / NITS_FULL) : 1
    if (!lit) m.emissiveMap = null
  }
  m.needsUpdate = true
  // the textures, each as it arrives (the material drawn with the ones in so far)
  const maps = [['map', s.baseColorMap || s.colorMap || s.diffuseMap, true, 'baseColorMapUseUV', 'diffuseMapUseUV'],
    ['normalMap', s.normalMap, false, 'normalMapUseUV'], ['roughnessMap', s.roughnessMap, false, 'roughnessMapUseUV'],
    ['metalnessMap', s.metallicMap, false, 'metallicMapUseUV'], ['aoMap', s.ambientOcclusionMap, false, 'ambientOcclusionMapUseUV'],
    ['alphaMap', s.opacityMap, false, 'opacityMapUseUV']]
  if (!m.userData.glow && m.emissive.r + m.emissive.g + m.emissive.b > 0) maps.push(['emissiveMap', s.emissiveMap, true, 'emissiveMapUseUV'])
  for (const [k, p, srgb, ...uvKeys] of maps) {
    const path = pathOf(def, p)
    if (!path) continue
    const channel = uvKeys.some(u => +s[u] === 1) ? 1 : 0
    materialStats.pending++
    texture(path, srgb).then(t => {
      materialStats.pending--
      if (!t || m.userData.def !== def || !m.userData.game) return
      if (t.channel !== channel) { t = t.clone(); t.channel = channel; t.needsUpdate = true }
      m[k] = t
      m.needsUpdate = true
    })
  }
}

export function forName(name, hasColors, near) {
  const key = `${name}|${hasColors ? 1 : 0}`
  let m = cache.get(key)
  if (m) return m
  m = new THREE.MeshStandardMaterial({ wireframe: settings.wireframe })
  m.name = name
  m.userData = { shared: true, name, hasColors: !!hasColors, near: near || '' }
  patch(m)
  asRule(m)
  withNight(name, m)
  cache.set(key, m)
  materialStats.rules++
  if (settings.game) gameOf(m)
  return m
}

function gameOf(m) {
  definition(m.userData.name, m.userData.near).then(def => {
    if (!def || !settings.game || m.userData.game) return
    materialStats.rules--
    materialStats.game++
    asGame(m, def)
  })
}

// the game's materials on (their definitions and textures) or off (the flat colours by kind)
export function setGameMaterials(on) {
  settings.game = !!on
  for (const m of cache.values()) {
    if (on) { if (!m.userData.game) gameOf(m) } else if (m.userData.game) { asRule(m); materialStats.game--; materialStats.rules++ }
  }
}

export function setWireframe(on) {
  settings.wireframe = on
  for (const m of cache.values()) m.wireframe = on
}

export const farPalette = new THREE.MeshLambertMaterial({ color: 0x8d8a6a, side: THREE.DoubleSide })
farPalette.userData.shared = true

// THREE geometries of a shape's drawn meshes (cdae.drawnMeshes) with their materials
export function meshesOf(drawn, materialNames, makeMaterial, near) {
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
      mats.push(makeMaterial ? makeMaterial(name, !!d.colors) : forName(name, !!d.colors, near))
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
