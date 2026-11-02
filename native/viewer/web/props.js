// The road tiles' props (street furniture: street lights, poles, hydrants, benches, bins, doors, fences, signals,
// stop signs ...) and guard rails, as the mod's trees.lua makes them: the rows' groups mapped to the game's own
// shapes (CATALOG, its variant picked by height and rank: pick), placed at the row's position, yaw and scale, a
// geotile's rows through its placement (geodesy.js), shown by the same distance rules (RINGS, TILE_CAP,
// GROUP_MAX_M; the mod's "normal" quality and distance). The shapes are the game's compiled .cdae files, served
// unchanged by /game/ (game.rs). A shape the game has not compiled yet (the mod's .dae-only ones) is drawn as a
// marker of the item's height.
import * as THREE from 'three'
import { decode, drawnMeshes } from './cdae.js'
import { forName } from './materials.js'
import * as geo from './geodesy.js'

const ECA = '/levels/east_coast_usa/art/shapes/'
const WCA = '/levels/west_coast_usa/art/shapes/'
const UT = '/levels/Utah/art/shapes/'
const JV = '/levels/johnson_valley/art/shapes/'
const JRI = '/levels/jungle_rock_island/art/shapes/'
const IT = '/levels/italy/art/shapes/'
const BNE = '/levels/beamng_earth/art/shapes/beamngEarth/'
const ATT = '/levels/automation_test_track/art/shapes/objects/'
const GD = '/art/shapes/garage_and_dealership/Clutter/'

// trees.lua M.CATALOG: group -> [[shape, height m], ...] (with vienna_convention_speed_signs.lua's km/h signs)
export const CATALOG = {
  cliff: [[IT + 'rocks/italy_rockface_short_wide', 2.9], [IT + 'rocks/italy_rockface_tall_wide', 13.2]],
  deciduous: [[ECA + 'trees/trees_oak/tree_oak_sml_a', 8.6], [ECA + 'trees/trees_oak/tree_oak_sml_b', 9.5],
    [ECA + 'trees/trees_aspen/tree_aspen_small_a', 8.3], [ECA + 'trees/trees_aspen/tree_aspen_small_c', 9.7],
    [ECA + 'trees/trees_beech/tree_beech_small_c', 9.9], [ECA + 'trees/trees_oak/tree_oak_large_a', 13.2],
    [ECA + 'trees/trees_oak/tree_oak_large_b', 15.9], [ECA + 'trees/trees_oak/tree_oak_large_c', 15.5],
    [ECA + 'trees/trees_oak/tree_oak_forest_a', 15.9], [ECA + 'trees/trees_oak/tree_oak_forest_b', 16.0],
    [ECA + 'trees/trees_beech/tree_beech_large_c', 17.4], [ECA + 'trees/trees_aspen/tree_aspen_forest_b', 18.5],
    [ECA + 'trees/trees_beech/tree_beech_forest_a', 20.2], [ECA + 'trees/trees_beech/tree_beech_large_b', 21.1]],
  conifer: [[ECA + 'trees/trees_douglasfir/tree_douglasfir_small_b', 11.2], [ECA + 'trees/trees_douglasfir/tree_douglasfir_small_a', 13.4],
    [ECA + 'trees/trees_douglasfir/tree_douglasfir_large_c', 18.9], [ECA + 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6],
    [ECA + 'trees/trees_douglasfir/tree_douglasfir_large_b', 21.4], [ECA + 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7],
    [IT + 'trees/trees_italy/scots_pine', 18.9]],
  shrub: [[ECA + 'trees/ECA_coast_bush/generibush', 1.8], [ECA + 'trees/trees_aspen/tree_aspen_bush_a', 2.2],
    [ECA + 'trees/trees_douglasfir/tree_douglasfir_bush_a', 2.7], [ECA + 'trees/trees_oak/tree_oak_bush_c', 3.1],
    [ECA + 'trees/trees_beech/tree_beech_bush_b', 3.3], [ECA + 'trees/trees_beech/tree_beech_bush_c', 3.9],
    [ECA + 'trees/trees_oak/tree_oak_bush_b', 4.1]],
  rock: [[ECA + 'rocks/eca_rock_small', 0.6], [ECA + 'rocks/rock_01_a_large_a', 0.7], [ECA + 'rocks/eca_rock_small_2', 0.9],
    [ECA + 'rocks/eca_rock_medium_1', 1.5], [ECA + 'rocks/eca_rock_medium_2', 1.6], [ECA + 'rocks/eca_rock_big_narrow', 1.9],
    [ECA + 'rocks/eca_rock_big_wide', 1.9]],
  pine_west: [[WCA + 'trees/trees_conifer/pine_radiata_small', 8.6], [WCA + 'trees/trees_conifer/pine_radiata_forest', 13.3],
    [WCA + 'trees/trees_conifer/pine_radiata_large', 13.2], [WCA + 'trees/trees_conifer/pine_radiata_weathered', 13.2],
    [ECA + 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6], [ECA + 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7],
    [IT + 'trees/trees_italy/scots_pine', 18.9], [WCA + 'trees/trees_conifer/redwood_tall', 38.5],
    [WCA + 'trees/trees_conifer/redwood_split', 38.1], [WCA + 'trees/trees_conifer/redwood_broad', 38.5]],
  oak_west: [[WCA + 'trees/trees_oak/oak_d', 7.1], [WCA + 'trees/trees_oak/oak_b', 8.2], [WCA + 'trees/trees_oak/oak_c', 8.4],
    [WCA + 'trees/trees_oak/oak_a', 9.6], [WCA + 'trees/trees_eucalyptus/eucalyptus_double', 14.7],
    [WCA + 'trees/trees_eucalyptus/eucalyptus_tall', 22.5]],
  palm: [[JRI + 'trees/trees_palm/palm_small', 8.9], [JRI + 'trees/trees_palm/palm_short', 9.1],
    [WCA + 'trees/trees_palm/fanpalm_short', 10.6], [WCA + 'trees/trees_palm/fanpalm_skirt', 11.7],
    [WCA + 'trees/trees_palm/fanpalm_full', 13.8], [JRI + 'trees/trees_palm/palm_medium', 18.0],
    [WCA + 'trees/trees_palm/fanpalm_tall', 18.5], [WCA + 'trees/trees_palm/fanpalm_slim', 20.8],
    [JRI + 'trees/trees_palm/palm_tall', 25.3]],
  juniper: [[UT + 'trees/Juniper/Juniper_bush_large_01', 2.6], [UT + 'trees/Juniper/Juniper_tree_04', 4.5],
    [UT + 'trees/Juniper/Juniper_tree_03', 5.2], [UT + 'trees/Juniper/Juniper_tree_01', 6.3],
    [UT + 'trees/Juniper/Juniper_tree_02', 7.4], [UT + 'trees/Juniper/Juniper_ancient_01', 7.5]],
  desert_shrub: [[UT + 'groundcover/s_gc_utah_grey_bush_01', 0.7], [JV + 'groundcover/s_gc_jv_dry_bush_01', 0.7],
    [UT + 'groundcover/s_gc_utah_grey_bush_03', 1.0], [JV + 'groundcover/s_gc_jv_dry_bush_03', 1.0],
    [UT + 'trees/Juniper/Juniper_bush_small_01', 1.2], [UT + 'trees/Juniper/Juniper_bush_small_dry_01', 2.0],
    [UT + 'trees/Juniper/Juniper_bush_medium_01', 2.4], [JV + 'trees/deserttrees/deserttree_bush_medium', 2.9]],
  desert_rock: [[UT + 'rocks/rock_01_a_small_a', 0.3], [UT + 'rocks/utah_rock_flat_a', 0.5], [UT + 'rocks/utah_boulder_5', 0.6],
    [JV + 'rocks/s_rocks_desert_small_01', 0.9], [JV + 'rocks/s_rocks_desert_medium_03', 1.4],
    [UT + 'rocks/utah_boulder_3', 1.7], [UT + 'rocks/utah_boulder_1', 1.9], [JV + 'rocks/s_rocks_desert_medium_01', 2.1]],
  door: [['/art/shapes/common/building_architect_modules/doors/R_HS_A_DR_01_100x210', 2.2],
    ['/art/shapes/common/building_architect_modules/doors/R_HS_B_DR_03_100x210', 2.2]],
  signal: [['/art/shapes/objects/s_trafficlight_boom_sn', 9.4]],
  stop: [[ECA + 'signs_usa/sign_stop', 3.0]],
  giveway: [[GD + 'italy_traf_sign_give_way', 3.0]],
  fence: [[ECA + 'buildings/eca_bld_wood_fence_c', 1.3]],
  wallsign_logistics: [[WCA + 'objects/sign_logistics', 5.8]], wallsign_rentabox: [[WCA + 'objects/sign_rentabox', 5.7]],
  wallsign_lemon: [[WCA + 'objects/sign_lemon', 3.6]], wallsign_turboburger: [[WCA + 'objects/sign_turboburger', 2.4]],
  speed30: [[UT + 'ut_roadsigns/ut_30_speed_limit', 2.6]], speed40: [[UT + 'ut_roadsigns/ut_40_speed_limit', 2.6]],
  speed50: [[UT + 'ut_roadsigns/ut_50_speed_limit', 2.6]], speed60: [[UT + 'ut_roadsigns/ut_60_speed_limit', 2.6]],
  speed70: [[UT + 'ut_roadsigns/ut_70_speed_limit', 2.6]],
  hydrant: [[WCA + 'objects/hydrant1', 1.5]],
  tollbooth: [[WCA + 'objects/s_toll_booth_center', 4.96]],
  bench: [[ECA + 'clutter/clutter_city_bench_wood', 0.9]],
  bin: [[GD + 'clutter_city_bin_round', 1.1], [WCA + 'objects/garbage_bin_a', 1.1]],
  polelamp: [[ECA + 'buildings/electrical_pole_lamp', 1.1]],
  streetlight: [[GD + 'eca_lamppost', 9.0]],
  pole: [[ATT + 's_powerpole_3wire', 9.2]],
  pylon: [[ATT + 'pylon', 35.8]],
  gantry: [[WCA + 'objects/substation_truss', 12.73]],
  sub_breaker: [[WCA + 'objects/substation_circuitbreaker', 5.48]],
  sub_mast: [[WCA + 'objects/substation_lightning', 25.29]],
  sub_support: [[WCA + 'objects/substation_support', 4.94]],
  sub_transformer: [[WCA + 'objects/substation_transformer', 6.18]],
  sub_fence: [[WCA + 'objects/s_chainlink_old', 1.76]],
  lamplens: [[BNE + 'bne_lamp_lens', 1.0]],
  lampglow: [[BNE + 'bne_lamp_glow', 1.0]],
  rrcross: [[BNE + 'bne_rr_crossbuck', 3.263]], rrcross_eu: [[BNE + 'bne_rr_cross_eu', 3.263]],
  rrflash: [[BNE + 'bne_rr_flasher', 4.7]], rrflash_eu: [[BNE + 'bne_rr_flasher_eu', 4.7]],
  rrgate: [[BNE + 'bne_rr_gate', 8.25]], rrgate_l: [[BNE + 'bne_rr_gate_l', 8.25]],
  rrgate_eu: [[BNE + 'bne_rr_gate_eu', 8.25]], rrgate_eu_l: [[BNE + 'bne_rr_gate_eu_l', 8.25]],
  tropical: [[JRI + 'trees/bush_tropical_1/tro_bush_huge', 5.5], [JRI + 'trees/bush_tropical_2/tro_bush_2_small', 6.1],
    [JRI + 'trees/trees_tropical_2/tro_tree_2_filler', 20.2], [JRI + 'trees/trees_tropical_1/tro_tree_1_huge', 20.6],
    [JRI + 'trees/trees_tropical_1/tro_tree_1_filler', 33.0]],
  guardrail: [['/art/shapes/objects/italy_guardrails_basic', 0.9]],
  guardrail_start: [['/art/shapes/objects/italy_guardrails_basic_end_cw', 0.9]],
  guardrail_end: [['/art/shapes/objects/italy_guardrails_basic_end_ccw', 0.9]],
  guardrail_back: [['/art/shapes/objects/italy_guardrails_basic', 0.9]],
  guardrail_back_start: [['/art/shapes/objects/italy_guardrails_basic_end_ccw', 0.9]],
  guardrail_back_end: [['/art/shapes/objects/italy_guardrails_basic_end_cw', 0.9]],
  jersey: [['/art/shapes/objects/jerseybarrier_3m', 0.88]],
  jersey_end: [['/art/shapes/objects/jerseybarrier_end', 0.88]],
  cable: [[BNE + 'bne_cable_barrier', 1.0]],
}
for (const lim of [30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150]) CATALOG[`speedk${lim}`] = [[`${BNE}sign_speed${lim}kph`, 2.47]]
CATALOG.speedknone = [[`${BNE}sign_speed_none`, 2.47]]
// a km/h sign is two items at one spot: its face and the body every sign shares (vienna body)
const SIGN_BODY = `${BNE}sign_round_body`
const isSign = g => /^speedk/.test(g)

// trees.lua M.GROUP_MAX_M: the tile distance (m) beyond which a group is not shown
export const GROUP_MAX_M = { shrub: 250, desert_shrub: 250, rock: 400, desert_rock: 400, cliff: 1500, pylon: 1500, gantry: 800,
  sub_breaker: 400, sub_mast: 1000, sub_support: 300, sub_transformer: 500, sub_fence: 200, lamplens: 800, lampglow: 6000,
  rrcross: 300, rrcross_eu: 300, rrflash: 400, rrflash_eu: 400, rrgate: 400, rrgate_l: 400, rrgate_eu: 400, rrgate_eu_l: 400,
  door: 120, hydrant: 200, bin: 200, bench: 200, polelamp: 400, stop: 300, giveway: 300, signal: 500, fence: 150,
  wallsign_logistics: 800, wallsign_rentabox: 800, wallsign_lemon: 800, wallsign_turboburger: 500,
  speed30: 300, speed40: 300, speed50: 300, speed60: 300, speed70: 300,
  guardrail: 4000, guardrail_start: 4000, guardrail_end: 4000, guardrail_back: 4000, guardrail_back_start: 4000,
  guardrail_back_end: 4000, jersey: 4000, jersey_end: 4000, cable: 1500 }
for (const g of Object.keys(CATALOG)) if (isSign(g)) GROUP_MAX_M[g] = 300
export const RINGS = [[300, 1.0], [700, 0.6], [1500, 0.35], [1e9, 0.2]]       // tile distance -> rank shown below
export const TILE_CAP = [[300, 6000], [700, 2400], [1500, 900], [1e9, 400]]    // ... and at most this many items a tile
export const GROUP_OFF = { lampglow: true }      // (the lamps' far glow: night only, nightlights.lua)
const isBarrier = g => g != null && (g.startsWith('guardrail') || g.startsWith('jersey') || g === 'cable')
const STRIDE = 8

// trees.lua M.pick: the variant of a group for an item of height h (near its size first, then wider, else the
// nearest; chosen by the rank), and its scale
export function pick(group, h, rank) {
  const list = CATALOG[group] || CATALOG.deciduous
  let cands = []
  for (const [lo, hi] of [[0.8, 1.25], [0.6, 1.6]]) {
    for (const v of list) if (v[1] >= h * lo && v[1] <= h * hi) cands.push(v)
    if (cands.length) break
  }
  if (!cands.length) {
    let best = null, bd = Infinity
    for (const v of list) { const d = Math.abs(Math.log(v[1] / Math.max(h, 0.1))); if (d < bd) { best = v; bd = d } }
    cands = [best]
  }
  const v = cands[Math.floor(rank * 7919) % cands.length]
  return [v[0], Math.max(0.3, Math.min(2.5, h / v[1])), v[1]]
}

function threshold(d) {
  for (const [m, r] of RINGS) if (d <= m) return r
  return 0
}
// trees.lua tileThreshold: lowered to keep the tile within TILE_CAP (the cap-th lowest rank)
function tileThreshold(d, t) {
  const want = threshold(d)
  for (const [m, cap] of TILE_CAP) {
    if (d <= m) {
      if (t.n <= cap) return want
      if (!t.ranks) t.ranks = [...t.items.map(it => it.rank)].sort((a, b) => a - b)
      return Math.min(want, t.ranks[cap])
    }
  }
  return want
}

// rows ([[group, x, y, z, yaw, scale, height, rank, (basis x9)], ...]) or a flat array -> items
function itemsOf(block) {
  const groups = Array.isArray(block.groups) ? block.groups : []
  const out = []
  const push = (r, s) => {
    if (!(Number.isFinite(r[1]) && Number.isFinite(r[2]) && Number.isFinite(r[3]))) return
    const it = { g: groups[r[0] | 0], x: r[1], y: r[2], z: r[3], yaw: r[4] || 0, h: r[6] ?? 10, rank: r[7] || 0, basis: null }
    if (s >= 17 && Number.isFinite(r[16])) it.basis = r.slice(8, 17)
    out.push(it)
  }
  if (Array.isArray(block.flat)) {
    const s = Math.max(STRIDE, block.stride | 0), f = block.flat
    for (let b = 0; b + s <= f.length; b += s) push(f.slice(b, b + s), s)
  } else if (Array.isArray(block.items)) {
    for (const r of block.items) if (Array.isArray(r)) push(r, r.length)
  }
  return out
}

const markerGeom = new Map()
function marker(h) {
  let g = markerGeom.get(h)
  if (!g) {
    g = new THREE.BoxGeometry(0.25, 0.25, h)
    g.translate(0, 0, h / 2)
    markerGeom.set(h, g)
  }
  return g
}
const markerMat = new THREE.MeshLambertMaterial({ color: 0xff40ff })
markerMat.userData.shared = true

export class Props {
  constructor(world, layers) {
    this.world = world
    this.layers = layers        // { props, rails }: the geotiles' rows (placed into the current world), by kind
    this.tiles = new Map()      // key -> tile
    this.shapes = new Map()     // shape path -> Promise<[{geometry, materials}] | null>
    this.stats = { tiles: 0, items: 0, shown: 0, shapes: 0, missing: new Set(), sources: {} }
  }

  shape(path) {
    let p = this.shapes.get(path)
    if (!p) {
      p = (async () => {
        const r = await fetch(`/game${encodeURI(path)}.cdae`)
        if (!r.ok) { this.stats.missing.add(path); return null }
        const src = (r.headers.get('x-source') || '?').split(':')[0]
        this.stats.sources[src] = (this.stats.sources[src] || 0) + 1
        const s = decode(new Uint8Array(await r.arrayBuffer()))
        const names = s.materials.map(m => m.name)
        const parts = []
        for (const d of drawnMeshes(s)) {
          const g = new THREE.BufferGeometry()
          g.setAttribute('position', new THREE.BufferAttribute(d.positions, 3))
          if (d.normals) g.setAttribute('normal', new THREE.BufferAttribute(d.normals, 3))
          if (d.uv) g.setAttribute('uv', new THREE.BufferAttribute(d.uv, 2))
          if (d.colors) g.setAttribute('color', new THREE.BufferAttribute(d.colors, 4, true))
          g.setIndex(new THREE.BufferAttribute(d.index, 1))
          const mats = []
          d.groups.forEach((gr, k) => { g.addGroup(gr.start, gr.count, k); mats.push(forName(gr.material >= 0 ? names[gr.material] || '' : '', !!d.colors)) })
          if (!d.normals) g.computeVertexNormals()
          if (d.matrix) g.applyMatrix4(new THREE.Matrix4().fromArray(d.matrix))
          g.computeBoundingSphere()
          parts.push({ geometry: g, materials: mats.length ? mats : [forName('', false)] })
        }
        this.stats.shapes++
        return parts
      })().catch(e => { console.warn('prop shape', path, e); this.stats.missing.add(path); return null })
      this.shapes.set(path, p)
    }
    return p
  }

  // a tile's rows: geo = { frame, pivot } (a geotile's, placed into the current world) or null (in anchor aid's world)
  add(key, block, geoInfo, aid) {
    this.drop(key)
    if (!block || typeof block !== 'object') return
    const items = itemsOf(block)
    if (!items.length) return
    const t = { key, items, n: items.length, geo: geoInfo, aid, group: new THREE.Group(), byShape: new Map(), shown: null, dropped: false }
    t.group.name = `props ${key}`
    t.group.matrixAutoUpdate = false
    for (let i = 0; i < items.length; i++) {
      const it = items[i]
      if (!it.g || !CATALOG[it.g]) { it.skip = true; continue }
      const [shape, sc, h0] = pick(it.g, it.h, it.rank)
      it.shape = shape; it.sc = sc; it.h0 = h0
      for (const s of isSign(it.g) ? [shape, SIGN_BODY] : [shape]) {
        if (!t.byShape.has(s)) t.byShape.set(s, { idx: [], meshes: [] })
        t.byShape.get(s).idx.push(i)
      }
    }
    // (a tile's kind from its key: 'g...' guard rails and barriers, else street furniture)
    const kind = key.startsWith('g') ? 'rails' : 'props'
    if (geoInfo) this.layers[kind].add(t.group)
    else this.world.anchorGroups(aid)?.[kind].add(t.group)
    this.tiles.set(key, t)
    this.place(t)
    for (const [s, e] of t.byShape) {
      this.shape(s).then(parts => {
        if (t.dropped) return
        const list = parts || [{ geometry: marker(t.items[e.idx[0]].h0 || 1), materials: [markerMat], marker: true }]
        for (const p of list) {
          const m = new THREE.InstancedMesh(p.geometry, p.materials.length === 1 ? p.materials[0] : p.materials, e.idx.length)
          m.userData.kind = 'prop'
          m.userData.shape = s
          m.userData.marker = !!p.marker
          e.meshes.push(m)
          t.group.add(m)
        }
        this.fill(t, e)
      })
    }
    this.stats.tiles = this.tiles.size
  }

  drop(key) {
    const t = this.tiles.get(key)
    if (!t) return
    t.dropped = true
    t.group.removeFromParent()
    for (const e of t.byShape.values()) for (const m of e.meshes) m.dispose()
    this.tiles.delete(key)
    this.stats.tiles = this.tiles.size
  }

  dropAll() { for (const k of [...this.tiles.keys()]) this.drop(k) }

  // the item matrices (world.js placeGeo: after every anchor change)
  placeAll() { for (const t of this.tiles.values()) this.place(t) }

  place(t) {
    const cur = this.world.anchor
    t.pl = t.geo && cur ? geo.placement(t.geo.frame, cur, 0, 0, t.geo.pivot) : null
    const yawAdd = t.pl ? Math.atan2(t.pl.r[3], t.pl.r[0]) * 180 / Math.PI : 0
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), v = new THREE.Vector3(), s = new THREE.Vector3(), axis = new THREE.Vector3(0, 0, 1)
    for (const it of t.items) {
      if (it.skip) continue
      let [x, y, z] = [it.x, it.y, it.z]
      if (t.pl) [x, y, z] = geo.applyPlacement(t.pl, x, y, z)
      if (it.basis) {
        // (a guard rail's orthonormal basis, the columns its local axes; a geotile's turned by its placement)
        let c = [0, 1, 2].map(k => it.basis.slice(3 * k, 3 * k + 3))
        if (t.pl) { const r = t.pl.r; c = c.map(([a, b, d]) => [r[0] * a + r[1] * b + r[2] * d, r[3] * a + r[4] * b + r[5] * d, r[6] * a + r[7] * b + r[8] * d]) }
        m.set(c[0][0], c[1][0], c[2][0], x, c[0][1], c[1][1], c[2][1], y, c[0][2], c[1][2], c[2][2], z, 0, 0, 0, 1)
        m.scale(s.set(it.sc, it.sc, it.sc))
      } else {
        // (the row's yaw counter-clockwise from +x, as trees.lua gives the engine's clockwise heading negated)
        q.setFromAxisAngle(axis, (it.yaw + yawAdd) * Math.PI / 180)
        m.compose(v.set(x, y, z), q, s.set(it.sc, it.sc, it.sc))
      }
      it.m = (it.m || new THREE.Matrix4()).copy(m)
    }
    // the tile's centre and half size in the current world (for the distance rules)
    if (t.pl) {
      ;[t.cx, t.cy] = geo.applyPlacement(t.pl, 0, 0, t.geo.pivot)
      t.half = 128
    } else {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
      for (const it of t.items) { x0 = Math.min(x0, it.x); x1 = Math.max(x1, it.x); y0 = Math.min(y0, it.y); y1 = Math.max(y1, it.y) }
      t.cx = (x0 + x1) / 2; t.cy = (y0 + y1) / 2; t.half = Math.max(128, (x1 - x0) / 2, (y1 - y0) / 2)
    }
    t.shown = null       // (filled again at the next update)
    for (const e of t.byShape.values()) this.fill(t, e)
  }

  // which items a tile shows from (px, py): trees.lua's rules at the box distance d
  visible(t, d) {
    const want = tileThreshold(d, t)
    let rowCap = null
    for (const [m, cap] of TILE_CAP) if (d <= m) { rowCap = cap; break }
    return t.items.map(it => {
      if (it.skip) return false
      let r = it.rank
      const cap = GROUP_MAX_M[it.g]
      if (isBarrier(it.g)) {
        if (cap && d > cap) r = 2
        else if (rowCap == null || t.n <= rowCap) r = 0
      } else if (cap && d > cap) r = 2
      if (GROUP_OFF[it.g]) r = 2
      return r < want
    })
  }

  fill(t, e) {
    if (!t.shown) return
    for (const m of e.meshes) {
      const rows = []      // instance -> its row (inspect)
      for (const i of e.idx) {
        if (!t.shown[i] || !t.items[i].m) continue
        m.setMatrixAt(rows.length, t.items[i].m)
        rows.push(t.items[i])
      }
      m.count = rows.length
      m.userData.rows = rows
      m.userData.tile = t.key
      m.instanceMatrix.needsUpdate = true
      m.computeBoundingSphere()
    }
  }

  // every half second: the tiles' shown items from the camera's ground point
  update(px, py) {
    let items = 0, shown = 0
    for (const t of this.tiles.values()) {
      const dx = Math.max(Math.abs(px - t.cx) - t.half, 0), dy = Math.max(Math.abs(py - t.cy) - t.half, 0)
      const vis = this.visible(t, Math.hypot(dx, dy))
      const sig = vis.join()
      if (sig !== t.sig) {
        t.sig = sig
        t.shown = vis
        for (const e of t.byShape.values()) this.fill(t, e)
      }
      items += t.n
      shown += vis.filter(Boolean).length
    }
    this.stats.items = items
    this.stats.shown = shown
  }
}

// a picked instance: { shape, tile, row } or null
export function itemOf(hit) {
  const u = hit.object?.userData
  if (!u || !u.rows || hit.instanceId == null) return null
  return { shape: u.shape, tile: u.tile, row: u.rows[hit.instanceId] }
}
