// A PNG decoder for earthd's heightmaps and masks (hm_*.png: 16-bit grayscale; masks: 8-bit grayscale).
// The browser's own decoder gives 8 bits per channel, so the heights are decoded here: IHDR, IDAT
// (zlib via DecompressionStream), the five row filters. Grayscale (colour type 0), 8 or 16 bits, no
// interlace: what the server writes.

async function inflate(bytes) {
  const ds = new DecompressionStream('deflate')
  const stream = new Blob([bytes]).stream().pipeThrough(ds)
  return new Uint8Array(await new Response(stream).arrayBuffer())
}

export async function decodeGray(buf) {
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength)
  const sig = [137, 80, 78, 71, 13, 10, 26, 10]
  for (let i = 0; i < 8; i++) if (b[i] !== sig[i]) throw new Error('png: bad signature')
  let i = 8, width = 0, height = 0, depth = 0, ctype = 0, interlace = 0
  const idat = []
  while (i < b.length) {
    const len = dv.getUint32(i)
    const type = String.fromCharCode(b[i + 4], b[i + 5], b[i + 6], b[i + 7])
    const data = b.subarray(i + 8, i + 8 + len)
    if (type === 'IHDR') {
      width = dv.getUint32(i + 8); height = dv.getUint32(i + 12)
      depth = b[i + 16]; ctype = b[i + 17]; interlace = b[i + 20]
    } else if (type === 'IDAT') idat.push(data)
    else if (type === 'IEND') break
    i += 12 + len
  }
  if (ctype !== 0 || interlace !== 0 || (depth !== 8 && depth !== 16)) {
    throw new Error(`png: colour type ${ctype}, depth ${depth}, interlace ${interlace} not supported`)
  }
  const raw = await inflate(new Uint8Array(await new Blob(idat).arrayBuffer()))
  const bpp = depth / 8, stride = width * bpp
  const out = new Uint8Array(height * stride)
  for (let y = 0; y < height; y++) {
    const f = raw[y * (stride + 1)]
    const src = y * (stride + 1) + 1, dst = y * stride, prev = dst - stride
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? out[dst + x - bpp] : 0
      const up = y > 0 ? out[prev + x] : 0
      const c = x >= bpp && y > 0 ? out[prev + x - bpp] : 0
      let v = raw[src + x]
      if (f === 1) v += a
      else if (f === 2) v += up
      else if (f === 3) v += (a + up) >> 1
      else if (f === 4) {
        const p = a + up - c, pa = Math.abs(p - a), pb = Math.abs(p - up), pc = Math.abs(p - c)
        v += pa <= pb && pa <= pc ? a : pb <= pc ? up : c
      }
      out[dst + x] = v & 255
    }
  }
  let values
  if (depth === 16) {
    values = new Uint16Array(width * height)
    for (let k = 0; k < values.length; k++) values[k] = (out[2 * k] << 8) | out[2 * k + 1]
  } else values = out
  return { width, height, depth, values }
}
