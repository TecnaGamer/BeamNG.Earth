// .cdae (TSShape v31, server/cdae.py and server/README.md ".cdae far tiles") decoded in the browser:
// u32 version word 0x007C001F, u32 header size, a msgpack header map, then a body that is a flat
// sequence of msgpack values in TSShape write order. Only what the server writes is supported
// (standard meshes and null meshes, no sequences), as cdae.decode.

const SHAPE_VECTORS = ['nodes', 'objects', 'subShapeFirstNode', 'subShapeFirstObject', 'subShapeNumNodes',
  'subShapeNumObjects', 'defaultRotations', 'defaultTranslations', 'nodeRotations', 'nodeTranslations',
  'nodeUniformScales', 'nodeAlignedScales', 'nodeArbitraryScaleFactors', 'nodeArbitraryScaleRots',
  'groundTranslations', 'groundRotations', 'objectStates', 'triggers', 'details']
const MESH_VECTORS = ['verts', 'tverts', 'tverts2', 'colors', 'norms', 'encodedNorms', 'primitives', 'indices', 'tangents']
const PRIM_NO_MATERIAL = 0x10000000
const PRIM_MATERIAL_MASK = 0x0FFFFFFF
const utf8 = new TextDecoder()

class Reader {
  constructor(buf, i) { this.b = buf; this.v = new DataView(buf.buffer, buf.byteOffset, buf.byteLength); this.i = i }
  next() {
    const b = this.b, v = this.v
    const t = b[this.i++]
    if (t <= 0x7f) return t
    if (t >= 0xe0) return t - 256
    if (t >= 0xa0 && t <= 0xbf) return this.str(t & 31)
    if (t >= 0x90 && t <= 0x9f) return this.arr(t & 15)
    if (t >= 0x80 && t <= 0x8f) return this.map(t & 15)
    let n
    switch (t) {
      case 0xc0: return null
      case 0xc2: return false
      case 0xc3: return true
      case 0xcc: n = v.getUint8(this.i); this.i += 1; return n
      case 0xcd: n = v.getUint16(this.i); this.i += 2; return n
      case 0xce: n = v.getUint32(this.i); this.i += 4; return n
      case 0xcf: n = Number(v.getBigUint64(this.i)); this.i += 8; return n
      case 0xd0: n = v.getInt8(this.i); this.i += 1; return n
      case 0xd1: n = v.getInt16(this.i); this.i += 2; return n
      case 0xd2: n = v.getInt32(this.i); this.i += 4; return n
      case 0xd3: n = Number(v.getBigInt64(this.i)); this.i += 8; return n
      case 0xca: n = v.getFloat32(this.i); this.i += 4; return n
      case 0xcb: n = v.getFloat64(this.i); this.i += 8; return n
      case 0xd9: n = v.getUint8(this.i); this.i += 1; return this.str(n)
      case 0xda: n = v.getUint16(this.i); this.i += 2; return this.str(n)
      case 0xdb: n = v.getUint32(this.i); this.i += 4; return this.str(n)
      case 0xc4: n = v.getUint8(this.i); this.i += 1; return this.bin(n)
      case 0xc5: n = v.getUint16(this.i); this.i += 2; return this.bin(n)
      case 0xc6: n = v.getUint32(this.i); this.i += 4; return this.bin(n)
      case 0xdc: n = v.getUint16(this.i); this.i += 2; return this.arr(n)
      case 0xdd: n = v.getUint32(this.i); this.i += 4; return this.arr(n)
      case 0xde: n = v.getUint16(this.i); this.i += 2; return this.map(n)
      case 0xdf: n = v.getUint32(this.i); this.i += 4; return this.map(n)
    }
    throw new Error(`cdae: msgpack type 0x${t.toString(16)} at ${this.i - 1}`)
  }
  str(n) { const s = utf8.decode(this.b.subarray(this.i, this.i + n)); this.i += n; return s }
  bin(n) { const s = this.b.subarray(this.i, this.i + n); this.i += n; return s }
  arr(n) { const a = []; for (let k = 0; k < n; k++) a.push(this.next()); return a }
  map(n) { const m = {}; for (let k = 0; k < n; k++) { const key = this.next(); m[key] = this.next() } return m }
  vector() {
    const count = this.next(), size = this.next(), data = this.next()
    if (!(data instanceof Uint8Array) || data.length !== count * size) throw new Error('cdae: bad vector')
    return { count, size, data }
  }
}

// a vector's bytes as a typed array (copied: the source may be unaligned)
function typed(vec, Ctor) {
  const copy = new Uint8Array(vec.data.length)
  copy.set(vec.data)
  return new Ctor(copy.buffer)
}

export function decode(buf) {
  buf = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  if (buf.length < 8) throw new Error('cdae: file too short')
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)
  const version = dv.getUint32(0, true), hsize = dv.getUint32(4, true)
  if ((version & 0xff) !== 31) throw new Error(`cdae: shape version word 0x${version.toString(16)}`)
  const hr = new Reader(buf, 8)
  const header = hr.next()
  if (header.compression) throw new Error('cdae: compressed bodies are not supported')
  const s = new Reader(buf, 8 + hsize)
  const out = { version, header }
  out.smallestVisibleSize = s.next(); out.smallestVisibleDL = s.next()
  out.radius = s.next(); out.tubeRadius = s.next()
  out.center = s.next(); out.bounds = s.next()
  for (const name of SHAPE_VECTORS) out[name] = s.vector()
  const nNames = s.next()
  out.names = []
  for (let k = 0; k < nNames; k++) out.names.push(s.next())
  const nMeshes = s.next()
  out.meshes = []
  for (let k = 0; k < nMeshes; k++) {
    const m = { type: s.next() }
    if (m.type === 4) { out.meshes.push(m); continue }
    if (m.type !== 0) throw new Error(`cdae: mesh type ${m.type}`)
    m.numFrames = s.next(); m.numMatFrames = s.next(); m.parentMesh = s.next()
    m.bounds = s.next(); m.center = s.next(); m.radius = s.next()
    for (const name of MESH_VECTORS) m[name] = s.vector()
    m.vertsPerFrame = s.next(); m.flags = s.next()
    out.meshes.push(m)
  }
  out.numSequences = s.next()
  if (out.numSequences) throw new Error('cdae: sequences are not supported')
  const nMats = s.next()
  out.materials = []
  for (let k = 0; k < nMats; k++) {
    out.materials.push({ name: s.next(), flags: s.next(), reflectanceMap: s.next(), bumpMap: s.next(),
      detailMap: s.next(), detailScale: s.next(), reflectionAmount: s.next() })
  }
  return out
}

// The drawn meshes of a decoded shape (each object's finest detail; collision objects left out) as plain arrays:
// { name, positions, normals, uv, uv2, colors, index, groups: [{ start, count, material }] }.
// Triangles are stored with reversed winding (cdae.py): the index order is turned back here.
export function drawnMeshes(shape) {
  const objs = typed(shape.objects, Int32Array)
  const out = []
  for (let o = 0; o < shape.objects.count; o++) {
    const [nameIndex, numMeshes, start] = [objs[o * 6], objs[o * 6 + 1], objs[o * 6 + 2]]
    const name = shape.names[nameIndex] || ''
    if (/^colmesh|^collision/i.test(name)) continue
    let m = null
    for (let k = 0; k < numMeshes; k++) {
      const c = shape.meshes[start + k]
      if (c && c.type === 0 && c.verts.count > 0) { m = c; break }
    }
    if (!m) continue
    const positions = typed(m.verts, Float32Array)
    const normals = m.norms.count === m.verts.count ? typed(m.norms, Float32Array) : null
    const uv = m.tverts.count === m.verts.count ? typed(m.tverts, Float32Array) : null
    const uv2 = m.tverts2.count === m.verts.count ? typed(m.tverts2, Float32Array) : null
    const colors = m.colors.count === m.verts.count ? typed(m.colors, Uint8Array) : null
    const index = typed(m.indices, Uint32Array)
    for (let k = 0; k + 2 < index.length; k += 3) { const t = index[k + 1]; index[k + 1] = index[k + 2]; index[k + 2] = t }
    const prims = typed(m.primitives, Int32Array)
    const groups = []
    for (let p = 0; p < m.primitives.count; p++) {
      const ps = prims[p * 3], pc = prims[p * 3 + 1], mat = prims[p * 3 + 2]
      groups.push({ start: ps, count: pc, material: (mat & PRIM_NO_MATERIAL) ? -1 : (mat & PRIM_MATERIAL_MASK) })
    }
    out.push({ name, positions, normals, uv, uv2, colors, index, groups })
  }
  return out
}
