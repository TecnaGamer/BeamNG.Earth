// The viewer's side of protocol 4: it plays the mod's part (core.lua, farfield.lua, roads.lua) with the
// files earthd writes for it, placed the same way (geodesy.js = geodesy.lua / geotile.lua). Every shape,
// texture and heightmap is the file earthd wrote, fetched unchanged through /data/.
//
// Scene layout (z up, as the game's world): `root` holds one group per anchor (the current anchor's at
// identity, a previous anchor's at its rigid transform into the current world) and the groups of
// origin-independent content (global far tiles, road / far geotiles) placed into the current anchor.
import * as THREE from 'three'
import { DDSLoader } from './vendor/DDSLoader.js'
import { decode, drawnMeshes } from './cdae.js'
import { decodeGray } from './png16.js'
import * as geo from './geodesy.js'
import { meshesOf, dispose, farPalette, forName } from './materials.js'
import { Props } from './props.js'

const MOD_VERSION = 'viewer-0.1'
const POSE_S = 0.5             // core.lua POSE_INTERVAL
const REBASE_M = 8000          // a new anchor once the camera is this far from the origin (Chebyshev)
const KEEP_NEAR_M = 20000      // a teleport this close keeps what is shown (as a rebase)
const LOADERS = 8              // files fetched at once

// --- loading -----------------------------------------------------------------------------------
const queue = []
let active = 0
function pump() {
  while (active < LOADERS && queue.length) {
    const job = queue.shift()
    active++
    job().finally(() => { active--; pump() })
  }
}
function schedule(fn, front = false) {
  return new Promise((resolve, reject) => {
    const job = () => fn().then(resolve, reject)
    if (front) queue.unshift(job); else queue.push(job)
    pump()
  })
}

export function dataUrl(vfs) {
  const m = /^\/?temp\/beamngEarth\/(.+)$/.exec(String(vfs))
  return m ? `/data/${m[1]}` : null
}

async function fetchBytes(vfs) {
  const url = dataUrl(vfs)
  if (!url) throw new Error(`not a session path: ${vfs}`)
  const r = await fetch(url, { cache: 'no-store' })
  if (!r.ok) throw new Error(`${vfs}: HTTP ${r.status}`)
  return new Uint8Array(await r.arrayBuffer())
}

export const stats = { bytes: 0, shapes: 0, failed: 0, lastError: '' }

async function loadShape(vfs) {
  const b = await fetchBytes(vfs)
  stats.bytes += b.length
  stats.shapes++
  const s = decode(b)
  return { shape: s, drawn: drawnMeshes(s), names: s.materials.map(m => m.name) }
}

const dds = new DDSLoader()
function loadTexture(vfs) {
  return new Promise((resolve, reject) => {
    dds.load(dataUrl(vfs), t => {
      t.colorSpace = THREE.SRGBColorSpace
      t.anisotropy = 4
      resolve(t)
    }, undefined, reject)
  })
}

function slotNumber(path) {
  const m = /slot_(\d+)\.cdae$/.exec(String(path))
  return m ? +m[1] : null
}

// --- the world ---------------------------------------------------------------------------------
export class World {
  constructor(net, root) {
    this.net = net
    this.root = root
    this.hello = null
    this.sessionId = null
    this.status = ''
    this.anchors = new Map()     // aid -> { aid, lat, lon, a: geo.Anchor, group }
    this.aid = null              // the current anchor
    this.pending = null          // a prepare_anchor in flight: { requestId, purpose, lat, lon }
    this.nextId = 1
    this.far = new Map()         // 'aid:key' (aid 0: global tiles) -> { msg, aid, obj, loaded, ok, heights }
    this.swaps = []              // far_swap messages waiting for their adds
    this.farDone = null
    this.block = null            // the committed terrain block { aid, cx, cy, msg, obj, heights }
    this.terrainPending = null
    this.geoRoads = new Map()    // gkey -> { frame, parts: {what: {shape, pos, pivot, obj}}, exact, skin }
    this.anchorRoads = new Map() // 'aid:key' -> { aid, objs } (non-geo road tiles)
    this.farGeo = new Map()      // 'kind:gkey' -> { kind, frame, parts: {sub: {..., obj}} }
    this.slotsGone = []
    this.cam = null
    this.poseTimer = 0
    this.lastPose = null
    this.listeners = []
    this.layers = { ground: new THREE.Group(), block: new THREE.Group(), roads: new THREE.Group(),
      content: new THREE.Group(), farbld: new THREE.Group(), props: new THREE.Group(), rails: new THREE.Group(),
      signs: new THREE.Group() }
    for (const [name, g] of Object.entries(this.layers)) { g.name = name; root.add(g) }
    // street furniture ('p' rows) and guard rails ('g' rows), each its own layer (props.js)
    this.props = new Props(this, { props: this.layers.props, rails: this.layers.rails })
    this.layers.block.visible = false
    this.globalGroup = new THREE.Group()
    this.layers.ground.add(this.globalGroup)
  }

  on(fn) { this.listeners.push(fn) }
  emit(ev, data) { for (const fn of this.listeners) fn(ev, data) }
  get anchor() { return this.anchors.get(this.aid)?.a || null }

  // --- anchors ---
  anchorGroups(aid) {
    let e = this.anchors.get(aid)
    if (!e) return null
    if (!e.groups) {
      e.groups = {}
      for (const [name, layer] of Object.entries(this.layers)) {
        const g = new THREE.Group()
        g.name = `${name}:${aid}`
        g.matrixAutoUpdate = false
        layer.add(g)
        e.groups[name] = g
      }
    }
    return e.groups
  }

  // every previous anchor's groups at their rigid move into the current world (exact at the camera)
  placeAnchors() {
    const cur = this.anchor
    if (!cur) return
    const p = this.cam ? this.cam.position : new THREE.Vector3()
    for (const e of this.anchors.values()) {
      if (!e.groups) continue
      const m = new THREE.Matrix4()
      if (e.aid !== this.aid) {
        // the transform of the current camera point back into e's world, then e -> current exact there
        const back = cur.worldTransformTo(e.a, p.x, p.y, p.z).apply(p.x, p.y, p.z)
        m.fromArray(e.a.worldTransformTo(cur, ...back).matrix4())
      }
      for (const g of Object.values(e.groups)) { g.matrix.copy(m); g.matrixWorldNeedsUpdate = true }
    }
    this.placeGeo()
  }

  requestAnchor(lat, lon, purpose = 'teleport', cx = 0, cy = 0) {
    const [slat, slon] = geo.snapAnchor(lat, lon)
    const requestId = this.nextId++
    this.pending = { requestId, purpose, lat: slat, lon: slon, want: [lat, lon] }
    const ok = this.net.send({ type: 'prepare_anchor', requestId, lat: slat, lon: slon, cx, cy, purpose })
    this.status = ok ? `Preparing anchor at ${slat.toFixed(4)}, ${slon.toFixed(4)}...` : 'Not connected to earthd'
    this.emit('status')
    return ok
  }

  teleport(lat, lon) {
    // the camera's place in the new frame: the anchor snaps, the camera goes exactly where asked
    return this.requestAnchor(lat, lon, 'teleport')
  }

  onAnchorReady(msg) {
    const p = this.pending
    if (!p || msg.requestId !== p.requestId) return
    this.pending = null
    const prev = this.anchors.get(this.aid)
    const a = new geo.Anchor(msg.lat, msg.lon)
    this.anchors.set(msg.anchorId, { aid: msg.anchorId, lat: msg.lat, lon: msg.lon, a })
    this.anchorGroups(msg.anchorId)
    const far = prev && Math.hypot(...prev.a.geodeticToWorld(msg.lat, msg.lon, 0).slice(0, 2)) > KEEP_NEAR_M
    let camWorld = null
    if (p.purpose === 'rebase' && prev && this.cam) {
      const c = this.cam.position
      camWorld = prev.a.worldTransformTo(a, c.x, c.y, c.z).apply(c.x, c.y, c.z)
    }
    if (!prev || far || p.purpose === 'teleport') {
      if (far || !prev) this.clearAll(msg.anchorId)
    }
    this.aid = msg.anchorId
    this.net.send({ type: 'commit_anchor', anchorId: msg.anchorId })
    const t = msg.terrain
    if (t) {
      this.net.send({ type: 'commit_terrain', anchorId: msg.anchorId, cx: t.cx, cy: t.cy })
      this.setBlock(msg.anchorId, t)
    }
    if (p.purpose === 'teleport') {
      const [x, y] = a.geodeticToWorld(p.want[0], p.want[1], 0)
      const gz = msg.groundZ ?? (t ? t.zBase + 100 : 0)
      this.emit('teleported', { x, y, z: (gz ?? 0) + 60 })
    } else if (camWorld) {
      this.emit('rebased', { x: camWorld[0], y: camWorld[1], z: camWorld[2] })
    }
    this.placeAnchors()
    this.status = `Anchor ${msg.anchorId} at ${msg.lat.toFixed(5)}, ${msg.lon.toFixed(5)}`
    this.emit('anchor', msg)
    this.emit('status')
  }

  // everything shown goes (a teleport far away): the server prunes what no `held` names. The global far tiles stay
  // (farfield.lua: kept across rebases and teleports, placed again into the new anchor; the server never sends a
  // shown one again, it removes them with swaps) unless the connection itself is gone (keepGlobal false).
  clearAll(keepAid, keepGlobal = true) {
    for (const [k, t] of this.far) {
      if (keepGlobal && t.aid === 0 && (t.obj?.parent || !t.loaded)) continue
      if (t.obj) { t.obj.removeFromParent(); dispose(t.obj) }
      this.releaseSlot(t)
      this.far.delete(k)
    }
    if (!keepGlobal) this.swaps = []      // (else kept: they may show kept global tiles; gone keys are skipped)
    for (const k of [...this.geoRoads.keys()]) this.dropGeoRoad(k)
    for (const k of [...this.anchorRoads.keys()]) this.dropAnchorRoad(k)
    for (const k of [...this.farGeo.keys()]) this.dropFarGeo(k)
    this.props.dropAll()
    if (this.block?.obj) { this.block.obj.removeFromParent(); dispose(this.block.obj) }
    this.block = null
    for (const [aid, e] of this.anchors) {
      if (aid === keepAid) continue
      if (e.groups) for (const g of Object.values(e.groups)) { g.removeFromParent(); dispose(g) }
      this.anchors.delete(aid)
    }
  }

  // previous anchors whose tiles are still shown (pose.held)
  heldAnchors() {
    const s = new Set()
    for (const t of this.far.values()) if (t.aid && t.aid !== this.aid) s.add(t.aid)
    for (const t of this.anchorRoads.values()) if (t.aid !== this.aid) s.add(t.aid)
    return [...s]
  }

  // --- messages ---
  onMessage(msg) {
    switch (msg.type) {
      case '_connected':
        this.net.send({ type: 'hello', protocol: 4, format: 4, modVersion: MOD_VERSION, sessions: [], vegFlat: true,
          geoTiles: geo.GEO_Z, roadGeo: 1, farGeo: 1, globalFar: 1 })
        this.status = 'Connected to earthd'
        break
      case '_disconnected':
        this.status = `Disconnected from earthd: ${msg.error || ''}`
        this.hello = null
        this.pending = null
        this.terrainPending = null
        this.clearAll(null, false)
        this.aid = null
        break
      case 'hello':
        this.hello = msg
        this.sessionId = msg.sessionId
        // what the mod sends after every hello (core.lua), so earthd builds for the viewer what it builds for the
        // game: gas stations on (without it the road tiles differ: canopies stay buildings), highway signs on
        this.net.send({ type: 'gas_stations', on: true })
        this.net.send({ type: 'highway_signs', on: true })
        this.emit('hello', msg)
        break
      case 'status':
        this.status = msg.text || ''
        break
      case 'anchor_ready': this.onAnchorReady(msg); break
      case 'anchor_error':
        if (this.pending && msg.requestId === this.pending.requestId) this.pending = null
        this.status = `Anchor failed: ${msg.error}`
        break
      case 'terrain_ready': this.onTerrainReady(msg); break
      case 'terrain_refresh':
        if (this.block && msg.anchorId === this.block.aid && msg.cx === this.block.cx && msg.cy === this.block.cy) this.setBlock(msg.anchorId, msg)
        break
      case 'terrain_error':
        if (this.terrainPending && msg.requestId === this.terrainPending.requestId) this.terrainPending = null
        break
      case 'far_tile': this.onFarTile(msg); break
      case 'far_swap': this.swaps.push(msg); this.runSwaps(); break
      case 'far_remove': this.removeFar(this.farKey(msg.anchorId, msg.key)); break
      case 'far_done': this.farDone = msg; this.emit('far_done', msg); break
      case 'far_retex': this.onRetex(msg); break
      case 'roads_tile': this.onRoadsTile(msg); break
      case 'roads_skin': this.onSkin(msg); break
      case 'roads_exact': this.onExact(msg); break
      case 'roads_drop':
        if (geo.parseKey(msg.key)) this.dropGeoRoad(msg.key); else this.dropAnchorRoad(`${msg.anchorId}:${msg.key}`)
        break
      case 'bldfar_tile': this.onFarGeo(msg, 'F'); break
      case 'bldsky_tile': this.onFarGeo(msg, 'S'); break
      case 'brfar_tile': this.onFarGeo(msg, 'B'); break
      case 'bldfar_drop': this.dropFarGeo(`F:${msg.key}`); break
      case 'bldsky_drop': this.dropFarGeo(`S:${msg.key}`); break
      case 'brfar_drop': this.dropFarGeo(`B:${msg.key}`); break
      case 'geocode_result': this.emit('geocode_result', msg); return      // (geocode.js)
      case 'where_result': this.emit('where_result', msg); return
      default: return
    }
    this.emit('status')
  }

  // --- poses (core.lua updateWorld) ---
  update(dt, camera) {
    this.cam = camera
    if (!this.aid || !this.net.connected) return
    const p = camera.position
    // origin rebase
    if (!this.pending && Math.max(Math.abs(p.x), Math.abs(p.y)) > REBASE_M) {
      const [lat, lon] = this.anchor.worldToGeodetic(p.x, p.y, 0)
      this.requestAnchor(lat, lon, 'rebase')
    }
    this.followTerrain(p)
    this.poseTimer += dt
    if (this.poseTimer < POSE_S || this.pending?.purpose === 'rebase') return
    const last = this.lastPose
    const vx = last ? (p.x - last.x) / this.poseTimer : 0, vy = last ? (p.y - last.y) / this.poseTimer : 0
    this.poseTimer = 0
    const [lat, lon] = this.anchor.worldToGeodetic(p.x, p.y, 0)
    const f = new THREE.Vector3()
    camera.getWorldDirection(f)
    const gz = this.heightAt(p.x, p.y)
    const pz = gz ?? p.z
    this.net.send({ type: 'pose', anchorId: this.aid, x: p.x, y: p.y, z: pz, vx, vy, lat, lon,
      speed: Math.hypot(vx, vy), follow: true, held: this.heldAnchors(), sessions: [],
      slotsGone: this.slotsGone.splice(0),
      cam: { x: p.x, y: p.y, z: p.z, fx: f.x, fy: f.y, fz: f.z, fovDeg: camera.fov, h: p.z - pz } })
    this.lastPose = { x: p.x, y: p.y }
    this.lat = lat; this.lon = lon
  }

  // the physics block follows the camera's ground point (follow.lua, simplified: a block centred within
  // one grid step of it)
  followTerrain(p) {
    if (!this.hello || this.terrainPending || !this.block || this.block.aid !== this.aid) return
    // (the block's span: 1024 x 2 m, 512 x 2 m (--block 512) or 1024 x 1 m (detail mode); centres on a grid of 1/8 of it)
    const span = (this.block.N || this.hello.blockSize || 1024) * (this.block.s || 2)
    const step = span / 8, reach = span / 4
    if (Math.max(Math.abs(p.x - this.block.cx), Math.abs(p.y - this.block.cy)) < reach) return
    const cx = Math.round(p.x / step) * step, cy = Math.round(p.y / step) * step
    const requestId = this.nextId++
    this.terrainPending = { requestId, cx, cy }
    this.net.send({ type: 'prepare_terrain', requestId, anchorId: this.aid, cx, cy })
  }

  onTerrainReady(msg) {
    const p = this.terrainPending
    if (!p || msg.requestId !== p.requestId) return
    this.terrainPending = null
    if (msg.anchorId !== this.aid) return
    this.net.send({ type: 'commit_terrain', anchorId: msg.anchorId, cx: msg.cx, cy: msg.cy })
    this.setBlock(msg.anchorId, msg)
  }

  // the terrain block (hm_<aid>_<cx>_<cy>.png): h = zBase + q16 * maxHeight / 65536, row 0 = north,
  // sample (gx, gy) at (cx - N + 2 gx, cy - N + 2 gy) with gy counted from the south (server/README.md)
  async setBlock(aid, t) {
    const token = {}
    this.blockToken = token
    let png
    try { png = await decodeGray(await fetchBytes(t.heightmap)) } catch (e) { stats.failed++; stats.lastError = String(e); return }
    if (this.blockToken !== token) return
    const N = png.width, s = t.squareSize || 2
    const heights = new Float32Array(N * N)    // [gy * N + gx], gy from the south
    for (let row = 0; row < N; row++) {
      const gy = N - 1 - row
      for (let gx = 0; gx < N; gx++) heights[gy * N + gx] = t.zBase + png.values[row * N + gx] * t.maxHeight / 65536
    }
    const x0 = t.cx - N * s / 2, y0 = t.cy - N * s / 2
    const STEP = 2     // every 2nd sample drawn (the heights are kept whole for heightAt)
    const n = Math.floor((N - 1) / STEP) + 1
    const pos = new Float32Array(n * n * 3)
    for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
      const gx = i * STEP, gy = j * STEP, k = (j * n + i) * 3
      pos[k] = gx * s; pos[k + 1] = gy * s; pos[k + 2] = heights[gy * N + gx]
    }
    const idx = new Uint32Array((n - 1) * (n - 1) * 6)
    let w = 0
    for (let j = 0; j < n - 1; j++) for (let i = 0; i < n - 1; i++) {
      const a = j * n + i, b = a + 1, c = a + n, d = c + 1
      if (((i + j) & 1) === 0) { idx.set([a, b, d, a, d, c], w) } else { idx.set([a, b, c, b, d, c], w) }
      w += 6
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
    g.setIndex(new THREE.BufferAttribute(idx, 1))
    g.computeVertexNormals()
    const mesh = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: 0xd08040, wireframe: true, transparent: true, opacity: 0.35 }))
    mesh.position.set(x0, y0, 0.02)
    const groups = this.anchorGroups(aid)
    if (!groups) return
    if (this.block?.obj) { this.block.obj.removeFromParent(); dispose(this.block.obj) }
    groups.block.add(mesh)
    this.block = { aid, cx: t.cx, cy: t.cy, msg: t, obj: mesh, heights, N, s, x0, y0 }
    this.emit('block', this.block)
  }

  // the ground height under (x, y) of the current world: the physics block (exact, bilinear), else the finest
  // shown far tile (bilinear on its grid), else null
  heightAt(x, y) {
    const b = this.block
    if (b && b.aid === this.aid) {
      const fx = (x - b.x0) / b.s, fy = (y - b.y0) / b.s
      if (fx >= 0 && fy >= 0 && fx < b.N - 1 && fy < b.N - 1) {
        const i = Math.floor(fx), j = Math.floor(fy), u = fx - i, v = fy - j, H = b.heights, N = b.N
        const h00 = H[j * N + i], h10 = H[j * N + i + 1], h01 = H[(j + 1) * N + i], h11 = H[(j + 1) * N + i + 1]
        return (h00 * (1 - u) + h10 * u) * (1 - v) + (h01 * (1 - u) + h11 * u) * v
      }
    }
    for (let L = -2; L <= 10; L++) {
      const S = 256 * 2 ** L
      const t = this.far.get(`${this.aid}:${L}_${Math.floor(x / S)}_${Math.floor(y / S)}`)
      if (t && t.heights && t.obj && t.obj.parent) {
        const z = World.gridHeight(t.heights, x - t.msg.pos[0], y - t.msg.pos[1])
        if (z != null) return z + t.msg.pos[2]
      }
    }
    // the global tiles (level 3 up, in their own frames): the finest shown one under (x, y), through its placement
    let best = null, bestLevel = Infinity
    const v = new THREE.Vector3()
    for (const t of this.far.values()) {
      if (t.aid !== 0 || !t.heights || !t.obj || !t.obj.parent) continue
      const lvl = t.msg.level ?? 99
      if (lvl >= bestLevel) continue
      if (!t.inv) continue
      v.set(x, y, t.msg.pos[2]).applyMatrix4(t.inv)         // (the tile's local xy under (x, y), at its pivot height)
      const h = World.gridHeight(t.heights, v.x, v.y)
      if (h == null) continue
      v.set(v.x, v.y, h).applyMatrix4(t.obj.matrix)
      best = v.z; bestLevel = lvl
    }
    return best
  }

  // --- far field (farfield.lua) ---
  farKey(aid, key) { return /^m\d+_/.test(key) ? `0:${key}` : `${aid}:${key}` }

  onFarTile(msg) {
    const k = this.farKey(msg.anchorId, msg.key)
    const old = this.far.get(k)
    if (old) { if (old.obj) { old.obj.removeFromParent(); dispose(old.obj) } this.releaseSlot(old) }
    const t = { msg, aid: k.startsWith('0:') ? 0 : msg.anchorId, obj: null, loaded: false, ok: false }
    if (msg.slotSerial != null && msg.material) {
      const n = /_(\d+)$/.exec(msg.material)
      if (n) t.slot = [+n[1], msg.slotSerial]
    }
    this.far.set(k, t)
    t.promise = schedule(async () => {
      try {
        const [s, tex] = await Promise.all([loadShape(msg.shape), msg.texture ? loadTexture(msg.texture) : null])
        if (this.far.get(k) !== t) { if (tex) tex.dispose(); return }
        const mat = tex ? new THREE.MeshLambertMaterial({ map: tex, side: THREE.DoubleSide }) : farPalette
        const obj = new THREE.Group()
        for (const m of meshesOf(s.drawn, s.names, () => mat)) obj.add(m)
        if (tex && !obj.children.length) mat.dispose()
        obj.name = `far ${msg.key}`
        obj.userData.kind = 'far'
        t.obj = obj
        t.heights = this.tileHeights(msg, s.drawn[0])
        t.ok = true
      } catch (e) {
        stats.failed++; stats.lastError = String(e)
      } finally {
        t.loaded = true
        this.releaseSlot(t)
        this.runSwaps()
      }
    })
  }

  // a tile's grid heights (its vertices inside its footprint; skirts hang below the edges and are left out)
  // In the tile's own coordinates (its shape's: anchor tiles from their SW corner, global tiles round their
  // frame's origin); the grid spans the vertices' extent with the layout's cells per edge.
  tileHeights(msg, d) {
    if (!d) return null
    const n = this.hello?.cells || 32, P = d.positions
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
    for (let k = 0; k < P.length; k += 3) {
      x0 = Math.min(x0, P[k]); x1 = Math.max(x1, P[k]); y0 = Math.min(y0, P[k + 1]); y1 = Math.max(y1, P[k + 1])
    }
    const sx = (x1 - x0) / n, sy = (y1 - y0) / n
    if (!(sx > 0 && sy > 0)) return null
    const z = new Float32Array((n + 1) * (n + 1)).fill(-Infinity)
    for (let k = 0; k < P.length; k += 3) {
      const i = Math.round((P[k] - x0) / sx), j = Math.round((P[k + 1] - y0) / sy)
      if (i < 0 || j < 0 || i > n || j > n) continue
      const q = j * (n + 1) + i
      if (P[k + 2] > z[q]) z[q] = P[k + 2]      // (a skirt vertex hangs below its edge vertex)
    }
    for (let q = 0; q < z.length; q++) if (z[q] === -Infinity) z[q] = 0
    return { x0, y0, sx, sy, n, z }
  }

  // bilinear on a tile's grid at local (lx, ly), or null outside it
  static gridHeight(h, lx, ly) {
    const fx = (lx - h.x0) / h.sx, fy = (ly - h.y0) / h.sy
    if (fx < 0 || fy < 0 || fx > h.n || fy > h.n) return null
    const i = Math.min(h.n - 1, Math.floor(fx)), j = Math.min(h.n - 1, Math.floor(fy))
    const u = fx - i, v = fy - j, G = h.z, n1 = h.n + 1
    return (G[j * n1 + i] * (1 - u) + G[j * n1 + i + 1] * u) * (1 - v) + (G[(j + 1) * n1 + i] * (1 - u) + G[(j + 1) * n1 + i + 1] * u) * v
  }

  releaseSlot(t) {
    if (t.slot) { this.slotsGone.push(t.slot); t.slot = null }
  }

  runSwaps() {
    while (this.swaps.length) {
      const s = this.swaps[0]
      const adds = (s.add || []).map(k => this.far.get(this.farKey(s.anchorId, k)))
      if (adds.some(t => t && !t.loaded)) return
      this.swaps.shift()
      for (const t of adds) if (t) this.showFar(t)
      for (const k of s.remove || []) this.removeFar(this.farKey(s.anchorId, k))
    }
    this.emit('far')
  }

  showFar(t) {
    if (!t.obj || t.obj.parent) return
    const m = t.msg
    if (t.aid === 0) {
      t.obj.matrixAutoUpdate = false
      this.globalGroup.add(t.obj)
      this.placeGlobal(t)
    } else {
      const groups = this.anchorGroups(t.aid)
      if (!groups) return
      t.obj.position.set(m.pos[0], m.pos[1], m.pos[2])
      groups.ground.add(t.obj)
    }
  }

  // global far tile (key m<z>_x_y): placed rigidly at its pivot from its own frame (farfield.lua placeGlobal)
  placeGlobal(t) {
    const cur = this.anchor
    if (!cur || !t.obj) return
    t.frame = t.frame || geo.frameOf(t.msg.frame, t.msg.key)
    const pz = t.msg.pos[2]
    const pl = geo.placement(t.frame, cur, 0, 0, pz)
    const xf = pl.xf
    const m = new THREE.Matrix4().fromArray(xf.matrix4())
    m.multiply(new THREE.Matrix4().makeTranslation(0, 0, pz))
    t.obj.matrix.copy(m)
    t.obj.matrixWorldNeedsUpdate = true
    t.inv = m.clone().invert()      // (heightAt)
  }

  removeFar(k) {
    const t = this.far.get(k)
    if (!t) return
    this.far.delete(k)
    if (t.obj) { t.obj.removeFromParent(); dispose(t.obj) }
    this.releaseSlot(t)
  }

  onRetex(msg) {
    const t = this.far.get(this.farKey(msg.anchorId, msg.key))
    if (!t || !t.obj || !msg.texture) return
    loadTexture(msg.texture).then(tex => {
      if (this.far.get(this.farKey(msg.anchorId, msg.key)) !== t) { tex.dispose(); return }
      t.obj.traverse(o => {
        if (o.material && !Array.isArray(o.material) && o.material.map) { o.material.map.dispose(); o.material.map = tex; o.material.needsUpdate = true }
        else if (Array.isArray(o.material)) for (const m of o.material) if (m.map) { m.map.dispose(); m.map = tex; m.needsUpdate = true }
      })
    }, () => {})
  }

  // --- roads (roads.lua) ---
  // a part's shape: its pooled path if the message has one; the slot is let go once the file was read
  async partObject(vfs, slot) {
    try {
      const s = await schedule(() => loadShape(vfs), true)
      const g = new THREE.Group()
      for (const m of meshesOf(s.drawn, s.names)) g.add(m)
      return g
    } catch (e) {
      stats.failed++; stats.lastError = String(e)
      return null
    } finally {
      if (slot) this.slotsGone.push(slot)
    }
  }

  slotOf(msg, what) {
    const e = msg.slots && msg.slots[what]
    if (!Array.isArray(e) || e.length < 2) return null
    const n = slotNumber(e[0])
    return n == null ? null : { path: e[0], gone: [n, e[1]] }
  }

  // release every slot of a message we do not use
  releaseUnused(msg, used) {
    for (const w of Object.keys(msg.slots || {})) {
      if (used.has(w)) continue
      const sl = this.slotOf(msg, w)
      if (sl) this.slotsGone.push(sl.gone)
    }
  }

  async jsonOf(msg) {
    if (msg.data && typeof msg.data === 'object') return msg.data
    const b = await fetchBytes(msg.path)
    return JSON.parse(new TextDecoder().decode(b))
  }

  async onRoadsTile(msg) {
    let data
    try { data = await this.jsonOf(msg) } catch (e) { this.releaseUnused(msg, new Set()); stats.failed++; stats.lastError = String(e); return }
    if (geo.parseKey(msg.key)) return this.onGeoRoad(msg, data)
    // an anchor's 256 m road tile: parts and DecalRoad chunks in the anchor's world
    const k = `${msg.anchorId}:${msg.key}`
    this.dropAnchorRoad(k)
    const t = { aid: msg.anchorId, objs: [] }
    this.anchorRoads.set(k, t)
    const groups = this.anchorGroups(msg.anchorId)
    const used = new Set()
    // (a sign tile, key "s<tx>_<ty>": signstream.py's render-only exit / mile / merge / speed sign shapes)
    for (const what of ['road', 'deck', 'walk', 'walkcol', 'bld', 'bld2', 'bld3', 'bld4', 'wire', 'tunnel', 'rail',
      ...World.SIGNS]) {
      const e = data[what]
      if (!e || !e.shape || !Array.isArray(e.pos) || what === 'walkcol') continue
      const sl = this.slotOf(msg, what)
      if (sl) used.add(what)
      this.partObject(sl ? sl.path : e.shape, sl?.gone).then(obj => {
        if (!obj || this.anchorRoads.get(k) !== t || !groups) { if (obj) dispose(obj); return }
        obj.position.set(e.pos[0], e.pos[1], e.pos[2])
        obj.userData.kind = World.SIGNS.includes(what) ? `${what} signs` : what
        obj.name = `${what} ${msg.key}`
        const layer = World.SIGNS.includes(what) ? groups.signs : what === 'road' || what === 'walk' ? groups.roads : groups.content
        layer.add(obj)
        t.objs.push(obj)
      })
    }
    this.releaseUnused(msg, used)
    if (!data.road && Array.isArray(data.chunks) && groups) {
      const obj = chunkRibbons(data.chunks)
      if (obj) { groups.roads.add(obj); t.objs.push(obj) }
    }
    // street furniture and guard rails in the anchor's world (roads.lua onTile: trees.onRows 'p<key>' / 'g<key>')
    this.props.add(`p${k}`, data.props, null, msg.anchorId)
    this.props.add(`g${k}`, data.guardrails, null, msg.anchorId)
  }

  dropAnchorRoad(k) {
    const t = this.anchorRoads.get(k)
    if (!t) return
    this.anchorRoads.delete(k)
    for (const o of t.objs) { o.removeFromParent(); dispose(o) }
    this.props.drop(`p${k}`)
    this.props.drop(`g${k}`)
  }

  static CONTENT = ['deck', 'bld', 'bld2', 'bld3', 'bld4', 'wire', 'tunnel', 'rail']
  static SIGNS = ['exits', 'mileposts', 'merges', 'speeds']      // (roads.lua M.SIGN_WHATS)

  onGeoRoad(msg, data) {
    const old = this.geoRoads.get(msg.key)
    if (old && old.h != null && old.h === (msg.h ?? data.h)) { this.releaseUnused(msg, new Set()); return }   // the same content
    let skin = null
    if (old) { skin = old.skin; old.skin = null; this.dropGeoRoad(msg.key) }
    const frame = geo.frameOf(msg.frame || data.frame, msg.key)
    if (!frame) return
    const t = { gkey: msg.key, h: msg.h ?? data.h, frame, parts: {}, skin, dropped: false }
    this.geoRoads.set(msg.key, t)
    const used = new Set()
    let pivots = 0, np = 0
    for (const what of World.CONTENT) {
      const e = data[what]
      if (!e || !e.shape || !Array.isArray(e.pos)) continue
      const p = { what, shape: e.shape, pos: e.pos, pivot: +e.pivot || 0, obj: null }
      t.parts[what] = p
      pivots += p.pivot; np++
      const ex = msg.exact && msg.exact[what]
      const sl = this.slotOf(msg, ex ? `x:${what}` : what)
      if (sl) used.add(ex ? `x:${what}` : what)
      this.loadGeoPart(t, p, sl ? sl.path : ex || e.shape, sl?.gone)
    }
    this.releaseUnused(msg, used)
    // its rows (street furniture 'p', guard rails 'g') in the tile's frame, placed at the content's mean pivot
    // (roads.lua onGeoTile: t.rowPivot; the parts roads.lua counts are its CONTENT_WHATS, bldcol and railcol too)
    for (const what of ['bldcol', 'railcol']) {
      const e = data[what]
      if (e && e.shape && Array.isArray(e.pos)) { pivots += +e.pivot || 0; np++ }
    }
    t.rowPivot = np ? pivots / np : 0
    this.props.add(`p${msg.key}`, data.props, { frame, pivot: t.rowPivot })
    this.props.add(`g${msg.key}`, data.guardrails, { frame, pivot: t.rowPivot })
  }

  loadGeoPart(t, p, path, slot) {
    const token = {}
    p.token = token
    this.partObject(path, slot).then(obj => {
      if (!obj || t.dropped || p.token !== token) { if (obj) dispose(obj); return }
      if (p.obj) { p.obj.removeFromParent(); dispose(p.obj) }
      obj.matrixAutoUpdate = false
      obj.userData.kind = p.what
      p.obj = obj
      ;(this.layers.content).add(obj)
      this.placePart(t.frame, p)
      this.emit('roads')
    })
  }

  // a geotile part into the current world (roads.lua geoWalk): p' = R (pos + v) + t, rigid at its pivot
  placePart(frame, p) {
    const cur = this.anchor
    if (!cur || !p.obj) return
    const pl = geo.placement(frame, cur, 0, 0, p.pivot)
    const m = new THREE.Matrix4().fromArray(pl.xf.matrix4())
    m.multiply(new THREE.Matrix4().makeTranslation(p.pos[0], p.pos[1], p.pos[2]))
    p.obj.matrix.copy(m)
    p.obj.matrixWorldNeedsUpdate = true
  }

  placeGeo() {
    for (const t of this.far.values()) if (t.aid === 0 && t.obj) this.placeGlobal(t)
    for (const t of this.geoRoads.values()) for (const p of Object.values(t.parts)) this.placePart(t.frame, p)
    for (const f of this.farGeo.values()) for (const p of Object.values(f.parts)) this.placePart(f.frame, p)
    this.props.placeAll()
  }

  onExact(msg) {
    const t = this.geoRoads.get(msg.key)
    if (!t || msg.anchorId !== this.aid) { this.releaseUnused(msg, new Set()); return }
    const used = new Set()
    for (const [what, path] of Object.entries(msg.parts || {})) {
      const p = t.parts[what]
      if (!p) continue
      const sl = this.slotOf(msg, what)
      if (sl) used.add(what)
      this.loadGeoPart(t, p, sl ? sl.path : path || p.shape, sl?.gone)
    }
    this.releaseUnused(msg, used)
  }

  async onSkin(msg) {
    const t = this.geoRoads.get(msg.key)
    if (!t || msg.anchorId !== this.aid) { this.releaseUnused(msg, new Set()); return }
    let data
    try { data = await this.jsonOf(msg) } catch (e) { this.releaseUnused(msg, new Set()); return }
    const groups = this.anchorGroups(msg.anchorId)
    const skin = { aid: msg.anchorId, objs: [], pending: 0 }
    const old = t.skin
    t.skin = skin
    const used = new Set()
    const done = () => {
      if (--skin.pending > 0) return
      if (old) for (const o of old.objs) { o.removeFromParent(); dispose(o) }
    }
    for (const what of ['road', 'walk']) {
      const w = data[what]
      if (!w || !w.shape || !Array.isArray(w.pos)) continue
      const sl = this.slotOf(msg, what)
      if (sl) used.add(what)
      skin.pending++
      this.partObject(sl ? sl.path : w.shape, sl?.gone).then(obj => {
        if (obj && t.skin === skin && !t.dropped && groups) {
          obj.position.set(w.pos[0], w.pos[1], w.pos[2])
          obj.userData.kind = what
          groups.roads.add(obj)
          skin.objs.push(obj)
        } else if (obj) dispose(obj)
        done()
        this.emit('roads')
      })
    }
    this.releaseUnused(msg, used)
    if (!data.road && Array.isArray(data.chunks) && groups) {
      const obj = chunkRibbons(data.chunks)
      if (obj) { groups.roads.add(obj); skin.objs.push(obj) }
    }
    if (!skin.pending && old) for (const o of old.objs) { o.removeFromParent(); dispose(o) }
  }

  dropGeoRoad(gkey) {
    const t = this.geoRoads.get(gkey)
    if (!t) return
    t.dropped = true
    this.geoRoads.delete(gkey)
    for (const p of Object.values(t.parts)) if (p.obj) { p.obj.removeFromParent(); dispose(p.obj) }
    if (t.skin) for (const o of t.skin.objs) { o.removeFromParent(); dispose(o) }
    this.props.drop(`p${gkey}`)
    this.props.drop(`g${gkey}`)
  }

  // far buildings (F), skyline (S), far bridges (B): geotiles placed rigidly at their pivots (roads.lua onFarGeo)
  async onFarGeo(msg, kind) {
    if (!geo.parseKey(msg.key)) { this.releaseUnused(msg, new Set()); return }      // (anchor tiles: not drawn)
    let data
    try { data = await this.jsonOf(msg) } catch (e) { this.releaseUnused(msg, new Set()); return }
    const key = `${kind}:${msg.key}`
    const old = this.farGeo.get(key)
    if (old && old.h != null && old.h === data.h) { this.releaseUnused(msg, new Set()); return }
    if (old) this.dropFarGeo(key)
    const frame = geo.frameOf(msg.frame || data.frame, msg.key)
    if (!frame) return
    const f = { kind, gkey: msg.key, h: data.h, frame, parts: {}, dropped: false,
      lamps: data.lamps && typeof data.lamps === 'object' && Object.keys(data.lamps).length ? data.lamps : null }
    this.farGeo.set(key, f)
    const list = kind === 'B' ? Object.entries(data.parts || {})
      : typeof data.shape === 'string' ? [['shape', { shape: data.shape, pos: data.pos, pivot: data.pivot }]] : []
    const used = new Set()
    for (const [sub, e] of list) {
      if (!e || typeof e.shape !== 'string' || !Array.isArray(e.pos)) continue
      const sl = this.slotOf(msg, sub)
      if (sl) used.add(sub)
      const p = { sub, pos: e.pos, pivot: +e.pivot || 0, obj: null, near: kind === 'F' ? msg.key : kind === 'B' ? sub : null }
      f.parts[sub] = p
      this.partObject(sl ? sl.path : e.shape, sl?.gone).then(obj => {
        if (!obj || f.dropped || this.farGeo.get(key) !== f) { if (obj) dispose(obj); return }
        obj.matrixAutoUpdate = false
        p.obj = obj
        this.layers.farbld.add(obj)
        this.placePart(f.frame, p)
      })
    }
    this.releaseUnused(msg, used)
  }

  dropFarGeo(key) {
    const f = this.farGeo.get(key)
    if (!f) return
    f.dropped = true
    this.farGeo.delete(key)
    this.farGeoLamps(f, false)
    for (const p of Object.values(f.parts)) if (p.obj) { p.obj.removeFromParent(); dispose(p.obj) }
  }

  // A far geotile's street lamps (roads.lua farGeoLamps; server buildings.far_lamps): while the tile shows, each road
  // geotile key's points the lampglow rows of a props tile 'FL<kind><key>' in that geotile's frame (shown at night only)
  farGeoLamps(f, on) {
    on = on && !!f.lamps && !!this.anchor
    if (on === !!f.lampOn) return
    f.lampOn = on
    for (const [k17, g] of Object.entries(f.lamps || {})) {
      const tk = `FL${f.kind}${k17}`
      if (!on) { this.props.drop(tk); continue }
      const rows = []
      ;(Array.isArray(g?.rows) ? g.rows : []).forEach((pt, i) => {
        if (!Array.isArray(pt) || ![0, 1, 2].every(j => Number.isFinite(+pt[j]))) return
        rows.push([0, +pt[0], +pt[1], +pt[2], 0, 1, 1, (((i + 1) * 7919) % 997) / 997 * 0.19])     // (Lua's i from 1)
      })
      const frame = geo.frameOf(null, k17)
      if (rows.length && frame) this.props.add(tk, { groups: ['lampglow'], items: rows }, { frame, pivot: +g.pivot || 0 })
    }
  }

  // each building shown once at the near / far seam: a far part hides once its road geotile shows its own
  // and a skyline square (zoom 15) shows only while not all 16 far building tiles (zoom 17) under it are in
  updateSeams() {
    const farIn = new Map()     // 'x15_y15' -> far building tiles in
    for (const f of this.farGeo.values()) {
      if (f.kind !== 'F') continue
      const k = geo.parseKey(f.gkey)
      if (!k) continue
      const sq = `${k[1] >> 2}_${k[2] >> 2}`
      farIn.set(sq, (farIn.get(sq) || 0) + 1)
    }
    for (const f of this.farGeo.values()) {
      if (f.kind === 'S') {
        const k = geo.parseKey(f.gkey)
        const covered = k && (farIn.get(`${k[1]}_${k[2]}`) || 0) >= 16
        for (const p of Object.values(f.parts)) if (p.obj) p.obj.visible = !covered
      } else {
        for (const p of Object.values(f.parts)) {
          if (!p.obj || !p.near) continue
          const t = this.geoRoads.get(p.near)
          const own = t && (f.kind === 'B' ? t.parts.deck : t.parts.bld)
          p.obj.visible = !(own && own.obj)
        }
      }
      if (f.lamps) this.farGeoLamps(f, Object.values(f.parts).some(p => p.obj && p.obj.visible))
    }
  }

  counts() {
    let shown = 0, loading = 0
    for (const t of this.far.values()) { if (t.obj && t.obj.parent) shown++; else if (!t.loaded) loading++ }
    return { far: shown, farLoading: loading, swaps: this.swaps.length, roads: this.geoRoads.size + this.anchorRoads.size,
      farGeo: this.farGeo.size, queue: queue.length + active }
  }
}

// DecalRoad chunks ({m, w, n: [[x, y, z(, w)], ...]}) as flat ribbons (a skin or tile without a road mesh)
function chunkRibbons(chunks) {
  const pos = [], idx = []
  for (const c of chunks) {
    const n = Array.isArray(c.n) ? c.n : []
    if (n.length < 2) continue
    const base = pos.length / 3
    for (let k = 0; k < n.length; k++) {
      const a = n[Math.max(0, k - 1)], b = n[Math.min(n.length - 1, k + 1)]
      let dx = b[0] - a[0], dy = b[1] - a[1]
      const l = Math.hypot(dx, dy) || 1
      dx /= l; dy /= l
      const w = (n[k][3] ?? c.w ?? 4) / 2
      pos.push(n[k][0] - dy * w, n[k][1] + dx * w, n[k][2] + 0.05, n[k][0] + dy * w, n[k][1] - dx * w, n[k][2] + 0.05)
      if (k) { const q = base + 2 * k; idx.push(q - 2, q - 1, q, q - 1, q + 1, q) }
    }
  }
  if (!idx.length) return null
  const g = new THREE.BufferGeometry()
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
  g.setIndex(idx)
  g.computeVertexNormals()
  const m = new THREE.Mesh(g, forName('asphalt', false))
  m.userData.kind = 'decalroads'
  return m
}
