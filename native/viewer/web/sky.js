// The sun, the sky and the shadows. With the time of day on, the sun stands where it does at the camera's place on the
// chosen date and time (NOAA's solar position algorithm, refraction included), and the light, the sky's colour and
// night (lighting.js: below NIGHT_ELEV) follow its elevation. Off, it is the fixed day light, or the night by the
// Night switch, as before. Shadows: the sun's, in a box around the camera that grows with the height above ground.
import * as THREE from 'three'

const D2R = Math.PI / 180, R2D = 180 / Math.PI

// NOAA's solar position (the General Solar Position calculations' spreadsheet, Meeus' formulas): the sun at time ms
// (UTC epoch ms) from (lat, lon) degrees: { elevation (apparent, refraction included), azimuth (from north,
// clockwise), declination, eot (equation of time, minutes) }
export function sunPosition(ms, lat, lon) {
  const jd = ms / 86400000 + 2440587.5
  const T = (jd - 2451545) / 36525
  const L0 = ((280.46646 + T * (36000.76983 + T * 0.0003032)) % 360 + 360) % 360
  const M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
  const e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
  const Mr = M * D2R
  const C = Math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T)) + Math.sin(2 * Mr) * (0.019993 - 0.000101 * T)
    + Math.sin(3 * Mr) * 0.000289
  const omega = 125.04 - 1934.136 * T
  const lambda = (L0 + C - 0.00569 - 0.00478 * Math.sin(omega * D2R)) * D2R
  const eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
  const eps = (eps0 + 0.00256 * Math.cos(omega * D2R)) * D2R
  const decl = Math.asin(Math.sin(eps) * Math.sin(lambda))
  const y = Math.tan(eps / 2) ** 2
  const L0r = L0 * D2R
  const eot = 4 * R2D * (y * Math.sin(2 * L0r) - 2 * e * Math.sin(Mr) + 4 * e * y * Math.sin(Mr) * Math.cos(2 * L0r)
    - 0.5 * y * y * Math.sin(4 * L0r) - 1.25 * e * e * Math.sin(2 * Mr))
  const utcMin = ((ms / 60000) % 1440 + 1440) % 1440
  const tst = ((utcMin + eot + 4 * lon) % 1440 + 1440) % 1440
  let ha = tst / 4 - 180
  if (ha < -180) ha += 360
  const latr = lat * D2R, har = ha * D2R
  const cz = Math.max(-1, Math.min(1, Math.sin(latr) * Math.sin(decl) + Math.cos(latr) * Math.cos(decl) * Math.cos(har)))
  const zen = Math.acos(cz)
  let az = (Math.atan2(Math.sin(har), Math.cos(har) * Math.sin(latr) - Math.tan(decl) * Math.cos(latr)) * R2D + 180) % 360
  const el = 90 - zen * R2D
  // (atmospheric refraction, NOAA's approximation)
  let ref = 0
  if (el <= 85) {
    const te = Math.tan(el * D2R)
    if (el > 5) ref = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
    else if (el > -0.575) ref = 1735 + el * (-518.2 + el * (103.4 + el * (-12.79 + el * 0.711)))
    else ref = -20.774 / te
    ref /= 3600
  }
  return { elevation: el + ref, azimuth: az, declination: decl * R2D, eot }
}

// the world direction toward the sun (world frame: +x east, +y north, +z up)
export function sunDirection(el, az, out = new THREE.Vector3()) {
  const e = el * D2R, a = az * D2R
  return out.set(Math.sin(a) * Math.cos(e), Math.cos(a) * Math.cos(e), Math.sin(e))
}

// night (street lamps and lit windows) once the sun is this far under the horizon: between sunset and civil dusk
// (-6 degrees), about when street lights come on
export const NIGHT_ELEV = -4

// the light by the sun's elevation (degrees): [elevation, sky background, hemisphere sky, hemisphere ground,
// hemisphere intensity, sun colour, sun intensity], interpolated between
const KEYS = [
  [-18, 0x0a0f1c, 0x3a4a66, 0x141210, 0.35, 0x8fa3c8, 0.12],
  [-8, 0x141c33, 0x3e4c6a, 0x161412, 0.40, 0x8fa3c8, 0.12],
  [-3, 0x3d4466, 0x6a6f90, 0x221d1a, 0.6, 0xff9a60, 0.0],
  [0, 0xc08a76, 0xb6a7b0, 0x3a3028, 0.85, 0xff8a4a, 0.35],
  [4, 0xd6aa88, 0xd2cfd6, 0x463a2e, 1.0, 0xffb070, 0.9],
  [12, 0xa9c0d6, 0xdbe8f6, 0x4a4030, 1.2, 0xffe2c0, 1.4],
  [25, 0x8fb4d6, 0xdfeeff, 0x4a4030, 1.3, 0xffffff, 1.6],
]
const ca = new THREE.Color(), cb = new THREE.Color()
function lerpHex(out, a, b, t) { return out.copy(ca.setHex(a)).lerp(cb.setHex(b), t) }
function lightAt(el) {
  let k = 0
  while (k < KEYS.length - 2 && el > KEYS[k + 1][0]) k++
  const a = KEYS[k], b = KEYS[k + 1]
  const t = Math.max(0, Math.min(1, (el - a[0]) / (b[0] - a[0])))
  return { t, a, b }
}

// the fixed lights (the time of day off): the day's and the night's
const DAY = { bg: 0x8fb4d6, hs: 0xdfeeff, hg: 0x4a4030, hi: 1.3, sc: 0xffffff, si: 1.6, dir: new THREE.Vector3(0.4, -0.6, 1).normalize() }
const NIGHT = { bg: 0x0a0f1c, hs: 0x3a4a66, hg: 0x141210, hi: 0.35, sc: 0x8fa3c8, si: 0.12 }
const MOON_DIR = new THREE.Vector3(-0.3, 0.5, 1).normalize()

const SHADOW_MAP = 4096
const SHADOW_FAR = 6000

export function today() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export class Sky {
  constructor(scene, root, renderer, hemi, sun) {
    this.scene = scene
    this.renderer = renderer
    this.hemi = hemi
    this.sun = sun
    root.add(sun.target)
    // settings: the time of day on, its date (YYYY-MM-DD) and local mean solar time (minutes; 12:00 the sun's mean
    // noon at the camera's longitude), the Night switch (the time of day off), the shadows
    this.timeOfDay = false
    this.date = today()
    this.minutes = 12 * 60
    this.manualNight = false
    this.shadows = false
    this.state = { night: false, sun: null, ms: null }
    this.dir = new THREE.Vector3()
    this.flagAt = -1e9
    sun.shadow.mapSize.set(SHADOW_MAP, SHADOW_MAP)
    sun.shadow.bias = -0.0003
    sun.shadow.normalBias = 0.4
    sun.shadow.camera.near = 1
    sun.shadow.camera.far = SHADOW_FAR
  }

  // the instant (UTC ms) of the date and local mean solar time at longitude lon
  instant(lon) {
    const [y, m, d] = this.date.split('-').map(Number)
    return Date.UTC(y, (m || 1) - 1, d || 1) + (this.minutes - lon * 4) * 60000
  }

  setShadows(on) {
    this.shadows = !!on
    this.renderer.shadowMap.enabled = this.shadows
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    // (the materials' shaders change with the shadow map: made again)
    this.scene.traverse(o => {
      const ms = Array.isArray(o.material) ? o.material : o.material ? [o.material] : []
      for (const m of ms) m.needsUpdate = true
    })
    this.flagAt = -1e9
  }

  // every mesh casts and receives the sun's shadows (the lamps' glow balls only receive: they are light);
  // looked over every second while the shadows are on
  flagMeshes(now) {
    if (!this.shadows || now - this.flagAt < 1) return
    this.flagAt = now
    this.scene.traverse(o => {
      if (!o.isMesh || o.userData.shadowSet) return
      o.userData.shadowSet = true
      const m = Array.isArray(o.material) ? o.material[0] : o.material
      o.castShadow = !(m && m.isMeshBasicMaterial)
      o.receiveShadow = true
    })
  }

  // every frame: the lights for the camera at (lat, lon), agl its height above the ground (m) and ground its z
  update(now, camera, lat, lon, agl, ground) {
    const { hemi, sun } = this
    let night, up = true
    if (this.timeOfDay && lat != null) {
      const ms = this.instant(lon)
      const s = sunPosition(ms, lat, lon)
      this.state.sun = s
      this.state.ms = ms
      const { t, a, b } = lightAt(s.elevation)
      lerpHex(this.scene.background, a[1], b[1], t)
      lerpHex(hemi.color, a[2], b[2], t)
      lerpHex(hemi.groundColor, a[3], b[3], t)
      hemi.intensity = a[4] + (b[4] - a[4]) * t
      lerpHex(sun.color, a[5], b[5], t)
      sun.intensity = a[6] + (b[6] - a[6]) * t
      up = s.elevation > -0.5
      // (under the horizon the directional light is the moon's, from a fixed high direction)
      if (up) sunDirection(Math.max(s.elevation, 0.5), s.azimuth, this.dir)
      else this.dir.copy(MOON_DIR)
      if (s.elevation < -6) { sun.color.setHex(NIGHT.sc); sun.intensity = NIGHT.si }
      night = s.elevation < NIGHT_ELEV
    } else {
      this.state.sun = null
      night = this.manualNight
      const L = night ? NIGHT : DAY
      this.scene.background.setHex(L.bg)
      hemi.color.setHex(L.hs); hemi.groundColor.setHex(L.hg); hemi.intensity = L.hi
      sun.color.setHex(L.sc); sun.intensity = L.si
      this.dir.copy(night ? MOON_DIR : DAY.dir)
      up = !night
    }
    this.state.night = night

    // the light's place: toward the sun from the shadow box's centre (on the ground, ahead of the camera)
    const h = Math.max(0, agl ?? 0)
    const R = Math.min(1500, Math.max(120, 120 + h * 1.5))
    const f = new THREE.Vector3()
    camera.getWorldDirection(f)
    const fl = Math.hypot(f.x, f.y) || 1
    const c = new THREE.Vector3(camera.position.x + f.x / fl * R * 0.5, camera.position.y + f.y / fl * R * 0.5,
      ground ?? camera.position.z - h)
    const cast = this.shadows && up
    sun.castShadow = cast
    if (cast) {
      const sc = sun.shadow.camera
      if (sc.right !== R) { sc.left = -R; sc.right = R; sc.top = R; sc.bottom = -R; sc.updateProjectionMatrix() }
      // (the centre snapped to the shadow map's texels: less shimmer as the camera moves)
      const texel = 2 * R / SHADOW_MAP
      c.set(Math.round(c.x / texel) * texel, Math.round(c.y / texel) * texel, Math.round(c.z / texel) * texel)
      this.flagMeshes(now)
    }
    sun.target.position.copy(c)
    sun.position.copy(c).addScaledVector(this.dir, SHADOW_FAR / 2)
    sun.target.updateMatrixWorld()
    return night
  }
}
