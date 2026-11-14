// Fly camera in the game's world convention (z up, x east, y north). Drag to look (any button), click
// (no drag) to pick, WASD to move, Q / E (or C / Space) down / up, Shift x5, Ctrl x0.2, wheel = speed.
import * as THREE from 'three'

export class FlyControls {
  constructor(camera, dom, onClick) {
    this.camera = camera
    this.dom = dom
    this.onClick = onClick
    this.yaw = 0          // rad, 0 = north, clockwise toward east
    this.pitch = -0.35    // rad, up positive
    this.speedScale = 1
    this.keys = new Set()
    camera.up.set(0, 0, 1)
    let drag = null
    dom.addEventListener('contextmenu', e => e.preventDefault())
    dom.addEventListener('pointerdown', e => {
      drag = { x: e.clientX, y: e.clientY, moved: false }
      dom.setPointerCapture(e.pointerId)
    })
    dom.addEventListener('pointermove', e => {
      if (!drag) return
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y
      if (!drag.moved && Math.hypot(dx, dy) < 4) return
      drag.moved = true
      this.yaw += dx * 0.004
      this.pitch = Math.max(-1.55, Math.min(1.55, this.pitch - dy * 0.004))
      drag.x = e.clientX; drag.y = e.clientY
    })
    dom.addEventListener('pointerup', e => {
      if (drag && !drag.moved && e.button === 0) this.onClick(e)
      drag = null
    })
    dom.addEventListener('wheel', e => {
      e.preventDefault()
      this.speedScale = Math.max(0.05, Math.min(50, this.speedScale * (e.deltaY < 0 ? 1.25 : 0.8)))
    }, { passive: false })
    const typing = e => e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement
    window.addEventListener('keydown', e => { if (!typing(e)) this.keys.add(e.code) })
    window.addEventListener('keyup', e => this.keys.delete(e.code))
    window.addEventListener('blur', () => this.keys.clear())
    this.apply()
  }

  forward() {
    const cp = Math.cos(this.pitch)
    return new THREE.Vector3(Math.sin(this.yaw) * cp, Math.cos(this.yaw) * cp, Math.sin(this.pitch))
  }

  apply() {
    const p = this.camera.position
    this.camera.lookAt(p.clone().add(this.forward()))
  }

  // heading in degrees from north, clockwise
  get heading() { return ((this.yaw * 180 / Math.PI) % 360 + 360) % 360 }

  update(dt, heightAboveGround) {
    const k = this.keys
    const r = new THREE.Vector3(Math.cos(this.yaw), -Math.sin(this.yaw), 0)
    const v = new THREE.Vector3()
    if (k.has('KeyW') || k.has('ArrowUp')) v.add(this.forward())
    if (k.has('KeyS') || k.has('ArrowDown')) v.sub(this.forward())
    if (k.has('KeyD') || k.has('ArrowRight')) v.add(r)
    if (k.has('KeyA') || k.has('ArrowLeft')) v.sub(r)
    if (k.has('KeyE') || k.has('Space')) v.z += 1
    if (k.has('KeyQ') || k.has('KeyC')) v.z -= 1
    if (v.lengthSq() > 0) {
      // faster high up: 20 m/s near the ground, about the height per second above it
      const base = Math.max(20, Math.min(20000, Math.abs(heightAboveGround ?? 100)))
      let s = base * this.speedScale
      if (k.has('ShiftLeft') || k.has('ShiftRight')) s *= 5
      if (k.has('ControlLeft') || k.has('ControlRight')) s *= 0.2
      this.camera.position.addScaledVector(v.normalize(), s * dt)
    }
    this.apply()
  }

  // look at a point from where the camera is
  lookAt(x, y, z) {
    const p = this.camera.position
    const d = new THREE.Vector3(x - p.x, y - p.y, z - p.z)
    this.yaw = Math.atan2(d.x, d.y)
    this.pitch = Math.atan2(d.z, Math.hypot(d.x, d.y))
    this.apply()
  }
}
