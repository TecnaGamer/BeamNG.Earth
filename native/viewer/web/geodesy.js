// WGS84 geodesy and the geotile grid: a line-for-line port of the mod's geodesy.lua and geotile.lua
// (mod/beamng_earth/lua/ge/extensions/beamngEarth/), so the viewer places tiles exactly as the game.
// World frame: ENU of the anchor (lat0, lon0, h = 0), +x east, +y north, +z up, metres.

const A = 6378137.0
const F = 1 / 298.257223563
const E2 = F * (2 - F)
const { sin, cos, sqrt, atan2, PI } = Math
const rad = d => d * PI / 180
const deg = r => r * 180 / PI

export function geodeticToEcef(latDeg, lonDeg, h = 0) {
  const lat = rad(latDeg), lon = rad(lonDeg)
  const sl = sin(lat), cl = cos(lat)
  const n = A / sqrt(1 - E2 * sl * sl)
  const r = (n + h) * cl
  return [r * cos(lon), r * sin(lon), (n * (1 - E2) + h) * sl]
}

export function ecefToGeodetic(x, y, z) {
  const p = sqrt(x * x + y * y)
  const lon = atan2(y, x)
  let lat = atan2(z, p * (1 - E2))
  for (let i = 0; i < 6; i++) {
    const sl = sin(lat)
    const n = A / sqrt(1 - E2 * sl * sl)
    lat = atan2(z + E2 * n * sl, p)
  }
  const sl = sin(lat), cl = cos(lat)
  const h = p * cl + z * sl - A * sqrt(1 - E2 * sl * sl)
  return [deg(lat), deg(lon), h]
}

export class Transform {
  constructor(r, t) { this.r = r; this.t = t }
  apply(x, y, z) {
    const r = this.r, t = this.t
    return [r[0] * x + r[1] * y + r[2] * z + t[0],
            r[3] * x + r[4] * y + r[5] * z + t[1],
            r[6] * x + r[7] * y + r[8] * z + t[2]]
  }
  // column-major 4x4 for THREE.Matrix4.fromArray / elements
  matrix4() {
    const r = this.r, t = this.t
    return [r[0], r[3], r[6], 0, r[1], r[4], r[7], 0, r[2], r[5], r[8], 0, t[0], t[1], t[2], 1]
  }
}

export class Anchor {
  constructor(latDeg, lonDeg) {
    this.lat = latDeg; this.lon = lonDeg
    const lat = rad(latDeg), lon = rad(lonDeg)
    const sl = sin(lat), cl = cos(lat), so = sin(lon), co = cos(lon);
    [this.ox, this.oy, this.oz] = geodeticToEcef(latDeg, lonDeg, 0)
    this.ex = -so; this.ey = co; this.ez = 0
    this.nx = -sl * co; this.ny = -sl * so; this.nz = cl
    this.ux = cl * co; this.uy = cl * so; this.uz = sl
  }
  ecefToEnu(x, y, z) {
    const dx = x - this.ox, dy = y - this.oy, dz = z - this.oz
    return [this.ex * dx + this.ey * dy + this.ez * dz,
            this.nx * dx + this.ny * dy + this.nz * dz,
            this.ux * dx + this.uy * dy + this.uz * dz]
  }
  enuToEcef(e, n, u) {
    return [this.ox + this.ex * e + this.nx * n + this.ux * u,
            this.oy + this.ey * e + this.ny * n + this.uy * u,
            this.oz + this.ez * e + this.nz * n + this.uz * u]
  }
  enuToGeodetic(e, n, u) { return ecefToGeodetic(...this.enuToEcef(e, n, u)) }
  geodeticToEnu(lat, lon, h) { return this.ecefToEnu(...geodeticToEcef(lat, lon, h)) }

  // the world convention: x, y of the point at h = 0 under it, z its height above it (geodesy.lua worldToGeodetic)
  worldToGeodetic(x, y, z = 0) {
    let qx = x, qy = y
    let [lat, lon] = this.enuToGeodetic(qx, qy, 0)
    for (let i = 0; i < 2; i++) {
      const [ex, ey] = this.geodeticToEnu(lat, lon, 0)
      qx += x - ex; qy += y - ey;
      [lat, lon] = this.enuToGeodetic(qx, qy, 0)
    }
    let h = this.enuToGeodetic(x, y, z)[2]
    const sl = sin(rad(lat)), cl = cos(rad(lat))
    const rate = this.ux * cl * cos(rad(lon)) + this.uy * cl * sin(rad(lon)) + this.uz * sl
    for (let i = 0; i < 2; i++) {
      const u = this.geodeticToEnu(lat, lon, h)[2]
      h += (z - u) / rate
    }
    return [lat, lon, h]
  }
  geodeticToWorld(lat, lon, h = 0) {
    const [x, y] = this.geodeticToEnu(lat, lon, 0)
    const z = this.geodeticToEnu(lat, lon, h)[2]
    return [x, y, z]
  }
  transformTo(other) {
    const a = this, b = other
    const ea = [[a.ex, a.ey, a.ez], [a.nx, a.ny, a.nz], [a.ux, a.uy, a.uz]]
    const eb = [[b.ex, b.ey, b.ez], [b.nx, b.ny, b.nz], [b.ux, b.uy, b.uz]]
    const r = []
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++)
      r[i * 3 + j] = eb[i][0] * ea[j][0] + eb[i][1] * ea[j][1] + eb[i][2] * ea[j][2]
    const dx = a.ox - b.ox, dy = a.oy - b.oy, dz = a.oz - b.oz
    const t = [0, 1, 2].map(i => eb[i][0] * dx + eb[i][1] * dy + eb[i][2] * dz)
    return new Transform(r, t)
  }
  // the rigid move exact at (x, y, z) of this world (geodesy.lua worldTransformTo)
  worldTransformTo(other, x = 0, y = 0, z = 0) {
    const xf = this.transformTo(other)
    if (this.lat === other.lat && this.lon === other.lon) return xf
    const [rx, ry, rz] = xf.apply(x, y, z)
    const [wx, wy, wz] = other.geodeticToWorld(...this.worldToGeodetic(x, y, z))
    xf.t = [xf.t[0] + wx - rx, xf.t[1] + wy - ry, xf.t[2] + wz - rz]
    return xf
  }
}

// geodesy.lua snapAnchor: the anchor lattice (so a place always gets the same frame and cache)
export function snapAnchor(latDeg, lonDeg) {
  if (Math.abs(latDeg) > 85.0) return [latDeg, lonDeg]
  const SNAP = 0.01
  const lat = Math.floor(latDeg / SNAP + 0.5) * SNAP
  const c = cos(rad(lat))
  let k = 1
  while (c * k * 2 <= 1.0 + 1e-12 && k < 128) k *= 2
  const step = SNAP * k
  let lon = Math.floor(lonDeg / step + 0.5) * step
  if (lon >= 180) lon -= 360; else if (lon < -180) lon += 360
  return [Number(lat.toFixed(6)), Number(lon.toFixed(6))]
}

// --- geotiles (geotile.lua) -----------------------------------------------------------------
export const GEO_Z = 17
const MAX_LAT = 85.0511287798066
const SHEAR_DZ = 50.0

export function tileOf(lat, lon, z = GEO_Z) {
  const n = 2 ** z
  const la = rad(Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)))
  const xf = (lon + 180.0) / 360.0 * n
  const yf = (1.0 - Math.log(Math.tan(la) + 1.0 / cos(la)) / PI) / 2.0 * n
  const top = n - 1
  return [Math.max(0, Math.min(top, Math.floor(xf))), Math.max(0, Math.min(top, Math.floor(yf)))]
}
export const latOf = (yf, z = GEO_Z) => deg(Math.atan(Math.sinh(PI * (1.0 - 2.0 * yf / 2 ** z))))
export const lonOf = (xf, z = GEO_Z) => xf / 2 ** z * 360.0 - 180.0
export const centre = (z, x, y) => [latOf(y + 0.5, z), lonOf(x + 0.5, z)]
export const bounds = (z, x, y) => [latOf(y + 1, z), lonOf(x, z), latOf(y, z), lonOf(x + 1, z)]

// (z, x, y) of 'g17_x_y' (or a global far key 'm<z>_x_y'), or null
export function parseKey(key) {
  const m = /^[gm](\d+)_(-?\d+)_(-?\d+)$/.exec(String(key))
  return m ? [+m[1], +m[2], +m[3]] : null
}

// the frame (an anchor) of a geotile from its message's frame [lat, lon], else its key's centre
export function frameOf(frame, key) {
  if (Array.isArray(frame) && frame.length >= 2) return new Anchor(+frame[0], +frame[1])
  const k = parseKey(key)
  return k ? new Anchor(...centre(...k)) : null
}

// geotile.placement: src's content in dst's world at pivot (px, py, pz): p' = R p + t + c (p.z - pz)
export function placement(src, dst, px = 0, py = 0, pz = 0) {
  const xf = src.worldTransformTo(dst, px, py, pz)
  const r = xf.r
  const map = (x, y, z) => dst.geodeticToWorld(...src.worldToGeodetic(x, y, z))
  const [ux, uy, uz] = map(px, py, pz + SHEAR_DZ)
  const [dx, dy, dz] = map(px, py, pz - SHEAR_DZ)
  const d2 = 2 * SHEAR_DZ
  return { r, t: xf.t, xf, c: [(ux - dx) / d2 - r[2], (uy - dy) / d2 - r[5], (uz - dz) / d2 - r[8]], pz }
}

export function applyPlacement(pl, x, y, z) {
  const r = pl.r, t = pl.t, c = pl.c, s = z - pl.pz
  return [r[0] * x + r[1] * y + r[2] * z + t[0] + c[0] * s,
          r[3] * x + r[4] * y + r[5] * z + t[1] + c[1] * s,
          r[6] * x + r[7] * y + r[8] * z + t[2] + c[2] * s]
}
