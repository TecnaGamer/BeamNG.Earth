// The OSM overlay: the ways (Overture roads with their OpenStreetMap source records and tags) and the
// connectors (the network's nodes) of the map-data cells around the camera, from /api/osm (osm.rs, read
// from earthd's own map-data mirror). Drawn as lines a little above the ground the game shows (the physics
// block, else the far tiles: World.heightAt), in the current anchor's world.
import * as THREE from 'three'
import { tileOf } from './geodesy.js'

const Z = 14
const LIFT = 0.6                // m above the ground
const RETRY_MS = 10000          // a cell missing from the mirror is asked again after this long
const COLORS = {
  motorway: 0xe0457b, trunk: 0xf08a3c, primary: 0xf2b134, secondary: 0xe8e24a, tertiary: 0xb8e05a,
  residential: 0xffffff, living_street: 0xdddddd, unclassified: 0xcfcfcf, service: 0x9fb8c8,
  pedestrian: 0x9ad0ff, footway: 0x6fb6ff, cycleway: 0x4fd0c0, path: 0x8fa8ff, steps: 0xc08fff, track: 0xc8a070,
  bridleway: 0x90c070, unknown: 0xff66ff,
}
const color = c => COLORS[c] ?? 0xcccccc

export class Osm {
  constructor(world, root) {
    this.world = world
    this.group = new THREE.Group()
    this.group.name = 'osm'
    this.ways = new THREE.Group()
    this.nodes = new THREE.Group()
    this.group.add(this.ways, this.nodes)
    root.add(this.group)
    this.cells = new Map()      // 'x_y' -> { state: 'loading'|'ok'|'missing'|'error', data, at, objs, aid }
    this.onTop = false
    this.dirty = false
    this.selected = null
    this.selMesh = null
  }

  setOnTop(on) {
    this.onTop = on
    this.group.traverse(o => { if (o.material) { o.material.depthTest = !on; o.renderOrder = on ? 10 : 0 } })
  }

  // load the 3 x 3 cells round the camera's ground point (only while low enough to see them)
  update(camPos) {
    const w = this.world, a = w.anchor
    if (!a) return
    const ground = w.heightAt(camPos.x, camPos.y) ?? 0
    if (camPos.z - ground > 6000) return
    const [lat, lon] = a.worldToGeodetic(camPos.x, camPos.y, 0)
    const [cx, cy] = tileOf(lat, lon, Z)
    const now = performance.now()
    for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
      const x = cx + dx, y = cy + dy, k = `${x}_${y}`
      const c = this.cells.get(k)
      if (c && (c.state === 'loading' || c.state === 'ok' || now - c.at < RETRY_MS)) continue
      this.load(k, x, y)
    }
    // a cell drawn in another anchor's world, or before the ground under it came: drawn again
    if (this.dirty) {
      this.dirty = false
      for (const c of this.cells.values()) if (c.state === 'ok') this.build(c)
    }
  }

  async load(k, x, y) {
    const c = { state: 'loading', at: performance.now(), objs: [] }
    this.cells.set(k, c)
    try {
      const r = await fetch(`/api/osm?z=${Z}&x=${x}&y=${y}`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const d = await r.json()
      if (d.missing) { c.state = 'missing'; c.at = performance.now(); return }
      c.data = d
      c.state = 'ok'
      this.build(c)
    } catch (e) {
      console.warn('osm cell', k, e)
      c.state = 'error'
      c.at = performance.now()
    }
  }

  // lines and points of a cell in the current world
  build(c) {
    for (const o of c.objs) { o.removeFromParent(); o.geometry.dispose(); o.material.dispose() }
    c.objs = []
    const w = this.world, a = w.anchor
    if (!a || !c.data) return
    c.aid = w.aid
    const toWorld = (lon, lat) => {
      const [x, y] = a.geodeticToWorld(lat, lon, 0)
      const h = w.heightAt(x, y)
      return [x, y, (h ?? 0) + LIFT]
    }
    const pos = [], col = [], wayOf = []
    const tmp = new THREE.Color()
    for (const way of c.data.ways) {
      tmp.setHex(color(way.tags.class))
      for (const part of way.g) {
        let prev = null
        for (let k = 0; k + 1 < part.length; k += 2) {
          const p = toWorld(part[k], part[k + 1])
          if (prev) { pos.push(...prev, ...p); col.push(tmp.r, tmp.g, tmp.b, tmp.r, tmp.g, tmp.b); wayOf.push(way) }
          prev = p
        }
      }
    }
    if (pos.length) {
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
      g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3))
      const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, depthTest: !this.onTop }))
      lines.renderOrder = this.onTop ? 10 : 0
      lines.userData.wayOf = wayOf
      this.ways.add(lines)
      c.objs.push(lines)
    }
    const np = []
    c.nodesOf = []
    for (const n of c.data.nodes) { np.push(...toWorld(n.p[0], n.p[1])); c.nodesOf.push(n) }
    if (np.length) {
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(np, 3))
      const pts = new THREE.Points(g, new THREE.PointsMaterial({ color: 0xffee88, size: 5, sizeAttenuation: false, depthTest: !this.onTop }))
      pts.renderOrder = this.onTop ? 11 : 0
      pts.userData.nodeOf = c.nodesOf
      this.nodes.add(pts)
      c.objs.push(pts)
    }
  }

  // the way or node under a screen ray: { kind, item, point }
  pick(raycaster) {
    raycaster.params.Line.threshold = 2
    raycaster.params.Points.threshold = 2
    const hits = raycaster.intersectObjects([...(this.nodes.visible ? this.nodes.children : []),
      ...(this.ways.visible ? this.ways.children : [])], false)
    for (const h of hits) {
      if (h.object.userData.wayOf) return { kind: 'way', item: h.object.userData.wayOf[h.index / 2 | 0], point: h.point }
      if (h.object.userData.nodeOf) return { kind: 'node', item: h.object.userData.nodeOf[h.index], point: h.point }
    }
    return null
  }

  // the selected way drawn thick in white
  highlight(way) {
    if (this.selMesh) { this.selMesh.removeFromParent(); this.selMesh.geometry.dispose(); this.selMesh = null }
    if (!way) return
    const a = this.world.anchor
    const pos = []
    for (const part of way.g) {
      let prev = null
      for (let k = 0; k + 1 < part.length; k += 2) {
        const [x, y] = a.geodeticToWorld(part[k + 1], part[k], 0)
        const p = [x, y, (this.world.heightAt(x, y) ?? 0) + LIFT + 0.3]
        if (prev) pos.push(...prev, ...p)
        prev = p
      }
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    this.selMesh = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0x00ffff, depthTest: false }))
    this.selMesh.renderOrder = 20
    this.group.add(this.selMesh)
  }

  clear() {
    for (const c of this.cells.values()) for (const o of c.objs) { o.removeFromParent(); o.geometry.dispose(); o.material.dispose() }
    this.cells.clear()
    this.highlight(null)
  }

  counts() {
    let ok = 0, missing = 0, ways = 0
    for (const c of this.cells.values()) {
      if (c.state === 'ok') { ok++; ways += c.data.ways.length } else if (c.state === 'missing') missing++
    }
    return { ok, missing, ways }
  }
}
