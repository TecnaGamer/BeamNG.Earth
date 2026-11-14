// Byte parity: the viewer serves earthd's files exactly as the game gets them.
// Needs earthd and the viewer running. Through the viewer's /ws it plays a minimal mod (hello, an anchor,
// commits, poses), then compares the SHA-256 of files fetched from /data/ with the files on disk, and
// decodes some shapes and the heightmap with the page's own decoders (web/cdae.js, web/png16.js).
//
//   node native/viewer/tests/parity.mjs [lat lon] [--viewer http://127.0.0.1:47700] [--user-dir DIR] [--seconds 45]
import { createHash } from 'crypto'
import { readFileSync } from 'fs'
import { join } from 'path'
import { pathToFileURL, fileURLToPath } from 'url'

const args = process.argv.slice(2)
const opt = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args.splice(i, 2)[1] : def }
const VIEWER = opt('--viewer', 'http://127.0.0.1:47700')
const SECONDS = +opt('--seconds', 45)
const cfg = await (await fetch(`${VIEWER}/api/config`)).json()
const USER = opt('--user-dir', cfg.userDir)
const [lat, lon] = args.length >= 2 ? [+args[0], +args[1]] : [35.46, -118.78]
const web = join(fileURLToPath(import.meta.url), '..', '..', 'web')
const { decode, drawnMeshes } = await import(pathToFileURL(join(web, 'cdae.js')))
const { decodeGray } = await import(pathToFileURL(join(web, 'png16.js')))

const ws = new WebSocket(VIEWER.replace(/^http/, 'ws') + '/ws')
const counts = {}, paths = new Set()
let aid = null
const send = m => ws.send(JSON.stringify(m))
ws.onmessage = ev => {
  const m = JSON.parse(ev.data)
  counts[m.type] = (counts[m.type] || 0) + 1
  if (m.type === '_connected') {
    send({ type: 'hello', protocol: 4, format: 4, modVersion: 'viewer-parity', sessions: [], vegFlat: true,
      geoTiles: 17, roadGeo: 1, farGeo: 1, globalFar: 1 })
  } else if (m.type === '_disconnected') {
    console.error('earthd:', m.error); process.exit(1)
  } else if (m.type === 'hello') {
    send({ type: 'prepare_anchor', requestId: 1, lat, lon, cx: 0, cy: 0, purpose: 'teleport' })
  } else if (m.type === 'anchor_ready') {
    aid = m.anchorId
    send({ type: 'commit_anchor', anchorId: aid })
    send({ type: 'commit_terrain', anchorId: aid, cx: m.terrain.cx, cy: m.terrain.cy })
    paths.add(m.terrain.heightmap)
  } else if (m.type === 'anchor_error') {
    console.error('anchor_error:', m.error); process.exit(1)
  } else if (m.type === 'far_tile') {
    paths.add(m.shape); if (m.texture) paths.add(m.texture)
  } else if (m.type === 'roads_tile' || m.type === 'roads_skin') {
    if (m.path) paths.add(m.path)
  }
}
setInterval(() => {
  if (aid == null) return
  // (no slotsGone: the files of pooled slots stay ours, so they cannot change while we read them)
  send({ type: 'pose', anchorId: aid, x: 0, y: 0, z: 0, vx: 0, vy: 0, lat, lon, speed: 0, follow: true, held: [],
    sessions: [], cam: { x: 0, y: 0, z: 300, fx: 0, fy: 1, fz: -0.3, fovDeg: 60, h: 300 } })
}, 500)

setTimeout(async () => {
  console.log('messages', counts)
  let same = 0, diff = 0, miss = 0, decoded = 0
  for (const p of paths) {
    const rel = p.replace(/^\/temp\/beamngEarth\//, '')
    const r = await fetch(`${VIEWER}/data/${rel}`)
    let disk
    try { disk = readFileSync(join(USER, 'temp', 'beamngEarth', rel)) } catch { miss++; continue }
    if (!r.ok) { miss++; continue }
    const served = Buffer.from(await r.arrayBuffer())
    const h = b => createHash('sha256').update(b).digest('hex')
    if (h(served) === h(disk)) same++; else { diff++; console.log('DIFFERENT', rel) }
    if (rel.endsWith('.cdae')) { drawnMeshes(decode(served)); decoded++ }
    if (/hm_\d+_-?\d+_-?\d+\.png$/.test(rel)) { const g = await decodeGray(served); console.log('heightmap', rel, g.width, g.depth); decoded++ }
  }
  console.log(`bytes: ${same} identical, ${diff} different, ${miss} gone or unreadable; ${decoded} decoded by the page's decoders`)
  process.exit(diff ? 1 : 0)
}, SECONDS * 1000)
