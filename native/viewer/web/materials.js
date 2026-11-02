// Materials for the shapes' material names. The game's own materials (the mod's textures) are not
// loaded: each name gets a flat colour by kind; buildings keep their per-vertex colours (the shapes'
// `colors`, the facade tint the server chose). Far tiles get their own DDS imagery (world.js).
import * as THREE from 'three'

const RULES = [
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

export function forName(name, hasColors) {
  const key = `${name}|${hasColors ? 1 : 0}|${settings.wireframe ? 1 : 0}`
  let m = cache.get(key)
  if (m) return m
  let color = 0xc8c3b8, kind = ''
  for (const [re, c, k] of RULES) if (re.test(name)) { color = c; kind = k; break }
  m = new THREE.MeshLambertMaterial({
    color, side: THREE.DoubleSide, vertexColors: !!hasColors && kind === 'bld', wireframe: settings.wireframe,
  })
  if (kind === 'mark') { m.polygonOffset = true; m.polygonOffsetFactor = -2; m.polygonOffsetUnits = -4 }
  m.userData.shared = true
  cache.set(key, m)
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
