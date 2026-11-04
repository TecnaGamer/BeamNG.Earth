// The game's textures for the materials (materials.js), from /game/ (game.rs: the mod folder, the content zips, their
// .link redirects, a .png path as its .dds), decoded here: DDS as GPU-compressed textures (BC1-3 / DXT, BC4, BC5 and
// BC7: what the game's materials ship, in their mipmaps), PNG / JPEG as images. A DDS whose format this browser's
// WebGL does not take (or BC6H, the sky's HDR) gives null, and the material keeps its colour.
import * as THREE from 'three'

// the largest mipmap uploaded (the game's 2048 and 4096 px textures: their smaller levels, to keep GPU memory down)
export const MAX_SIZE = 1024
export const textureStats = { loaded: 0, failed: 0, unsupported: 0, bytes: 0, formats: {} }

let renderer = null
export function setRenderer(r) { renderer = r }

// DXGI formats (the DX10 header) and four-character codes: [three.js format, bytes per 4x4 block, extension]
const S3TC = 'WEBGL_compressed_texture_s3tc', RGTC = 'EXT_texture_compression_rgtc', BPTC = 'EXT_texture_compression_bptc'
const DXGI = {
  71: ['BC1', THREE.RGBA_S3TC_DXT1_Format, 8, S3TC], 72: ['BC1', THREE.RGBA_S3TC_DXT1_Format, 8, S3TC],
  74: ['BC2', THREE.RGBA_S3TC_DXT3_Format, 16, S3TC], 75: ['BC2', THREE.RGBA_S3TC_DXT3_Format, 16, S3TC],
  77: ['BC3', THREE.RGBA_S3TC_DXT5_Format, 16, S3TC], 78: ['BC3', THREE.RGBA_S3TC_DXT5_Format, 16, S3TC],
  80: ['BC4', THREE.RED_RGTC1_Format, 8, RGTC], 81: ['BC4s', THREE.SIGNED_RED_RGTC1_Format, 8, RGTC],
  83: ['BC5', THREE.RED_GREEN_RGTC2_Format, 16, RGTC], 84: ['BC5s', THREE.SIGNED_RED_GREEN_RGTC2_Format, 16, RGTC],
  98: ['BC7', THREE.RGBA_BPTC_Format, 16, BPTC], 99: ['BC7', THREE.RGBA_BPTC_Format, 16, BPTC],
}
const FOURCC = {
  DXT1: DXGI[71], DXT3: DXGI[74], DXT5: DXGI[77], ATI1: DXGI[80], BC4U: DXGI[80], BC4S: DXGI[81],
  ATI2: DXGI[83], BC5U: DXGI[83], BC5S: DXGI[84],
}

function supported(ext) {
  if (!renderer) return false
  return renderer.extensions.has(ext)
}

// a DDS file -> CompressedTexture (null when its format is not one of the above or not supported here)
export function parseDDS(buf) {
  const u32 = new Uint32Array(buf, 0, 32)
  if (u32[0] !== 0x20534444) return null                    // 'DDS '
  const height = u32[3], width = u32[4]
  const mips = Math.max(1, u32[7])
  const four = String.fromCharCode(...new Uint8Array(buf, 84, 4))
  let fmt, off = 128
  if (four === 'DX10') {
    fmt = DXGI[new Uint32Array(buf, 128, 1)[0]]
    off = 148
  } else fmt = FOURCC[four]
  if (!fmt) { textureStats.unsupported++; return null }
  if (!supported(fmt[3])) { textureStats.unsupported++; return null }
  let [name, format, block] = fmt
  // (three.js r170 does not upload BC4 (RED_RGTC1: "unsupported compressed texture format", though WebGL has it),
  // while BC5 works: a BC5 block is two BC4 blocks (red, green), so each BC4 block is sent twice, red = green)
  const bc4 = format === THREE.RED_RGTC1_Format
  if (bc4) format = THREE.RED_GREEN_RGTC2_Format
  const levels = []
  let w = width, h = height
  for (let i = 0; i < mips && off < buf.byteLength; i++) {
    const size = Math.max(1, (w + 3) >> 2) * Math.max(1, (h + 3) >> 2) * block
    if (off + size > buf.byteLength) break
    let data = new Uint8Array(buf, off, size)
    if (bc4) {
      const two = new Uint8Array(size * 2)
      for (let b = 0; b < size; b += 8) { const blk = data.subarray(b, b + 8); two.set(blk, b * 2); two.set(blk, b * 2 + 8) }
      data = two
    }
    levels.push({ data, width: w, height: h })
    off += size
    w = Math.max(1, w >> 1); h = Math.max(1, h >> 1)
  }
  // (the levels above MAX_SIZE left out; a level not a multiple of 4 below the first is fine in WebGL 2)
  let first = 0
  while (first < levels.length - 1 && Math.max(levels[first].width, levels[first].height) > MAX_SIZE) first++
  const use = levels.slice(first)
  if (!use.length || use[0].width % 4 || use[0].height % 4) { textureStats.unsupported++; return null }
  const t = new THREE.CompressedTexture(use, use[0].width, use[0].height, format)
  t.minFilter = use.length > 1 ? THREE.LinearMipmapLinearFilter : THREE.LinearFilter
  t.magFilter = THREE.LinearFilter
  t.generateMipmaps = false
  t.userData.format = name
  textureStats.formats[name] = (textureStats.formats[name] || 0) + 1
  t.needsUpdate = true
  return t
}

const cache = new Map()      // path|srgb -> Promise<Texture|null>

// the texture at a game path (as a material names it), sRGB for colour maps
export function texture(path, srgb) {
  const key = `${path.toLowerCase()}|${srgb ? 1 : 0}`
  let p = cache.get(key)
  if (p) return p
  p = (async () => {
    const r = await fetch(`/game${encodeURI(path)}`)
    if (!r.ok) { textureStats.failed++; return null }
    const buf = await r.arrayBuffer()
    textureStats.bytes += buf.byteLength
    let t
    if (new Uint32Array(buf, 0, 1)[0] === 0x20534444) {
      t = parseDDS(buf)
      if (!t) return null
    } else {
      // (the image's row 0 at v = 0: the shapes' v is the engine's 1 - v, so no flip)
      const img = await createImageBitmap(new Blob([buf]), { imageOrientation: 'none', premultiplyAlpha: 'none' })
      t = new THREE.Texture(img)
      t.flipY = false
      t.generateMipmaps = true
      t.minFilter = THREE.LinearMipmapLinearFilter
      t.needsUpdate = true
    }
    t.wrapS = t.wrapT = THREE.RepeatWrapping
    t.anisotropy = 4
    if (srgb) t.colorSpace = THREE.SRGBColorSpace
    t.name = path
    textureStats.loaded++
    return t
  })().catch(e => { console.warn('texture', path, e); textureStats.failed++; return null })
  cache.set(key, p)
  return p
}
