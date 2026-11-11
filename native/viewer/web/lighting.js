// Night lighting, as the mod's nightlights.lua: after dusk the buildings' windows and the street lamps' lenses glow
// (materials.js NIGHT_MATERIALS), the lamps' far glow shows (props.js group lampglow, off by day), and a pool of spot
// lights (no shadows) moves to the lamps nearest the camera every LIGHTS_S: within AROUND_M all round, and out to
// NEAR_M within VIEW_COS of the camera's heading, each pointing down, tilted toward the road along its lens row's yaw.
// The game follows its own time of day; the viewer's night comes from its time of day or Night switch (sky.js, which
// also sets the sky and the sun or moon light).
import * as THREE from 'three'
import { setNight } from './materials.js'

export const LIGHTS_S = 0.25
export const NEAR_M = 900
export const AROUND_M = 60
export const VIEW_COS = 0.42
// nightlights.lua M.LIGHT (the engine's SpotLight fields: range m, inner / outer cone angles in degrees, brightness)
export const LIGHT = { range: 36, inner: 50, outer: 130, brightness: 8.0, color: [1, 0.66, 0.33], drop: 0.12, tilt: 12 }
// three.js candela per unit of the engine's brightness (chosen by eye: the pool under a lamp about as bright as the
// day's ground; the engine's brightness has no physical unit)
const CANDELA_PER_BRIGHTNESS = 16
// the pool's sizes: the mod's 48 / 128 / 256 ("Street lamp lights": few / normal / many) are more spot lights than a
// browser's forward renderer takes (each one a loop in every lit pixel, and the uniforms a light needs), so fewer here
export const POOL_SIZES = [0, 16, 32, 48, 64]
export const POOL_DEFAULT = 32

// The lamps' far glow (tools/build_lamp_glow.py, bne_lamp_glow.dae): a ball at the lens growing with distance, in the
// engine's detail levels. Its finest level is a speck (the lens shows near the camera), so the viewer draws the levels
// itself: the same balls, switched at the same distances (scaled to this screen, as the engine's pixel size is).
// (The mod's balls each have their brightness, nightlights.lua M.GLOW_NITS, the far ones under the windows' 200 nits;
// all at full here.)
const GLOW_R = [0.1, 0.16, 0.26, 0.42, 0.67, 1.1, 1.75, 2.8]
const END_PX = [2.5, 2.3, 2.1, 1.9, 1.7, 1.5, 1.3]   // (build_lamp_glow.py: each ball's width where the next takes over)
const GLOW_NEAR_M = 60, SCREEN_PX = 1130, ENGINE_PX = 4100, LAST_PX = 2
const GLOW_SHAPE_R = GLOW_R[GLOW_R.length - 1] * Math.sqrt(3)
// the distances (on the game's 1440-pixel-high, 65-degree screen) where each level ends: the speck, then each ball
export const GLOW_ENDS = [GLOW_NEAR_M, ...GLOW_R.slice(0, -1).map((r, k) => 2 * r * SCREEN_PX / END_PX[k]), GLOW_SHAPE_R * ENGINE_PX / LAST_PX]
export const GLOW_SHAPE = '/levels/beamng_earth/art/shapes/beamngEarth/bne_lamp_glow'

const glowScale = { value: 1 }       // this screen's pixels per radian over the game's (SCREEN_PX)
let glowParts = null
export function glowShape() {
  if (glowParts) return glowParts
  const g = new THREE.SphereGeometry(1, 8, 5)
  g.boundingSphere = new THREE.Sphere(new THREE.Vector3(), GLOW_R[GLOW_R.length - 1])
  const [r, gr, b] = LIGHT.color
  const m = new THREE.MeshBasicMaterial({ color: new THREE.Color().setRGB(r, gr, b, THREE.LinearSRGBColorSpace) })
  m.userData.shared = true
  const steps = GLOW_ENDS.map((e, k) => k === 0 ? `d < ${e.toFixed(1)} ? 0.0` : `d < ${e.toFixed(1)} ? ${GLOW_R[k - 1].toFixed(2)}`).join(' : ')
  m.onBeforeCompile = sh => {
    sh.uniforms.uGlowScale = glowScale
    sh.vertexShader = 'uniform float uGlowScale;\n' + sh.vertexShader.replace('#include <begin_vertex>', `
      #ifdef USE_INSTANCING
        vec3 glowAt = (modelMatrix * instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
      #else
        vec3 glowAt = (modelMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
      #endif
      float d = distance(cameraPosition, glowAt) / uGlowScale;
      vec3 transformed = position * (${steps} : 0.0);`)
  }
  m.customProgramCacheKey = () => 'bne_lamp_glow'
  glowParts = [{ geometry: g, materials: [m] }]
  return glowParts
}

export class NightLights {
  constructor(root, props) {
    this.root = root            // (the world's coordinates)
    this.props = props
    this.night = false
    this.size = POOL_DEFAULT
    this.pool = []
    this.last = -1e9
    this.stats = { lenses: 0, lit: 0 }
  }

  setNight(on) {
    if (!!on === this.night) return
    this.night = !!on
    setNight(this.night)
    this.props.setGroupEnabled('lampglow', this.night)
    this.resize()
  }

  setSize(n) {
    this.size = n
    this.resize()
  }

  // the pool made for the night, removed by day (its lights in every lit material's shader: one change of each)
  resize() {
    const want = this.night ? this.size : 0
    if (want === this.pool.length) return
    for (const e of this.pool) { e.light.removeFromParent(); e.light.target.removeFromParent(); e.light.dispose() }
    this.pool = []
    const [r, g, b] = LIGHT.color
    for (let i = 0; i < want; i++) {
      const l = new THREE.SpotLight(new THREE.Color().setRGB(r, g, b, THREE.LinearSRGBColorSpace), 0, LIGHT.range,
        LIGHT.outer / 2 * Math.PI / 180, 1 - LIGHT.inner / LIGHT.outer, 2)
      l.castShadow = false
      this.root.add(l, l.target)
      this.pool.push({ light: l, at: null })
    }
    this.last = -1e9
  }

  // the street lamps' lenses in the current world: [x, y, z, yaw] (the road tiles' lamplens rows, placed)
  lenses() {
    const out = []
    for (const t of this.props.tiles.values()) {
      for (const it of t.items) {
        if (it.g !== 'lamplens' || !it.m) continue
        const e = it.m.elements
        out.push([e[12], e[13], e[14], it.yaw + (t.yawAdd || 0)])
      }
    }
    return out
  }

  // nightlights.lua M.nearest: the n nearest lenses to (x, y) within r; with a heading (fx, fy), those beyond `around`
  // only within VIEW_COS of it
  static nearest(lenses, x, y, r, n, fx, fy, around) {
    const cand = [], r2 = r * r, a2 = (around ?? r) ** 2
    for (const l of lenses) {
      const dx = l[0] - x, dy = l[1] - y, d = dx * dx + dy * dy
      if (d <= r2 && (d <= a2 || fx == null || dx * fx + dy * fy >= VIEW_COS * Math.sqrt(d))) cand.push([l, d])
    }
    cand.sort((a, b) => a[1] - b[1])
    return cand.slice(0, n).map(c => c[0])
  }

  update(now, camera, height) {
    glowScale.value = height / (2 * Math.tan(camera.fov * Math.PI / 360)) / SCREEN_PX
    if (!this.pool.length || now - this.last < LIGHTS_S) return
    this.last = now
    const p = camera.position
    const f = new THREE.Vector3()
    camera.getWorldDirection(f)
    const h = Math.hypot(f.x, f.y)
    const [fx, fy] = h > 0.3 ? [f.x / h, f.y / h] : [null, null]    // (looking nearly straight down: all round)
    const all = this.lenses()
    const want = NightLights.nearest(all, p.x, p.y, NEAR_M, this.pool.length, fx, fy, AROUND_M)
    const t = LIGHT.tilt * Math.PI / 180
    want.forEach((l, i) => {
      const e = this.pool[i], L = e.light
      const a = l[3] * Math.PI / 180
      L.position.set(l[0], l[1], l[2] - LIGHT.drop)
      // (its forward: down, tilted toward the road along the lens row's yaw, the lamp's arm)
      L.target.position.set(l[0] + Math.cos(a) * Math.sin(t), l[1] + Math.sin(a) * Math.sin(t), l[2] - LIGHT.drop - Math.cos(t))
      L.intensity = LIGHT.brightness * CANDELA_PER_BRIGHTNESS
      e.at = l
    })
    for (let i = want.length; i < this.pool.length; i++) { this.pool[i].light.intensity = 0; this.pool[i].at = null }
    this.stats.lenses = all.length
    this.stats.lit = want.length
  }
}
