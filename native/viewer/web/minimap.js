// The minimap: earthd's map tiles (/tiles, maptiles.py) with the camera's position and view cone and the
// physics block's footprint. Wheel zooms, drag pans (the map stops following the camera until "centre"),
// a click (no drag) teleports there. Web Mercator from the mod's own mapmath.js (a copy).
import { visibleTiles, toScreen, fromScreen, pan, zoomAt } from './mapmath.js'

export class Minimap {
  constructor(canvas, onTeleport) {
    this.c = canvas
    this.ctx = canvas.getContext('2d')
    this.onTeleport = onTeleport
    this.lat = 0; this.lon = 0; this.z = 15
    this.follow = true
    this.tiles = new Map()      // 'z/x/y' -> Image (complete / failed)
    this.cam = null             // { lat, lon, heading (deg from north, clockwise), fov }
    this.block = null           // [[lat, lon] x 4] or null
    this.pending = null         // the place a teleport was asked for
    this.noTiles = false
    let drag = null
    canvas.addEventListener('pointerdown', e => {
      drag = { x: e.offsetX, y: e.offsetY, lat: this.lat, lon: this.lon, moved: false }
      canvas.setPointerCapture(e.pointerId)
    })
    canvas.addEventListener('pointermove', e => {
      if (!drag) return
      const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y
      if (Math.hypot(dx, dy) > 4) drag.moved = true
      if (drag.moved) {
        const p = pan(drag.lat, drag.lon, this.z, dx, dy)
        this.lat = p.lat; this.lon = p.lon; this.follow = false
      }
    })
    canvas.addEventListener('pointerup', e => {
      if (drag && !drag.moved) {
        const p = fromScreen(e.offsetX, e.offsetY, this.lat, this.lon, this.z, this.w, this.h)
        this.pending = p
        this.onTeleport(p.lat, p.lon)
      }
      drag = null
    })
    canvas.addEventListener('wheel', e => {
      e.preventDefault()
      // (following: zoom about the camera, which stays in the middle)
      const sx = this.follow ? this.w / 2 : e.offsetX, sy = this.follow ? this.h / 2 : e.offsetY
      const r = zoomAt(this.lat, this.lon, this.z, this.z - Math.sign(e.deltaY) * 0.5, sx, sy, this.w, this.h)
      this.lat = r.lat; this.lon = r.lon; this.z = r.z
    }, { passive: false })
  }

  get w() { return this.c.clientWidth }
  get h() { return this.c.clientHeight }

  centre() { this.follow = true }

  tile(t) {
    const k = `${t.z}/${t.x}/${t.y}`
    let im = this.tiles.get(k)
    if (!im) {
      im = new Image()
      im.onerror = () => {
        im.failed = true
        if (this.tiles.size < 4) this.noTiles = true
        // (503: earthd could not fetch it yet; asked again later)
        setTimeout(() => { if (this.tiles.get(k) === im) this.tiles.delete(k) }, 15000)
      }
      im.onload = () => { this.noTiles = false }
      im.src = `/tiles/${k}.jpg`
      this.tiles.set(k, im)
      if (this.tiles.size > 400) this.tiles.delete(this.tiles.keys().next().value)
    }
    return im
  }

  draw() {
    const dpr = window.devicePixelRatio || 1, w = this.w, h = this.h
    if (this.c.width !== Math.round(w * dpr) || this.c.height !== Math.round(h * dpr)) {
      this.c.width = Math.round(w * dpr); this.c.height = Math.round(h * dpr)
    }
    const ctx = this.ctx
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    if (this.follow && this.cam) { this.lat = this.cam.lat; this.lon = this.cam.lon }
    ctx.fillStyle = '#1d2329'
    ctx.fillRect(0, 0, w, h)
    for (const t of visibleTiles(this.lat, this.lon, this.z, w, h)) {
      const im = this.tile(t)
      if (im.complete && !im.failed && im.naturalWidth) ctx.drawImage(im, t.left, t.top, t.size + 0.5, t.size + 0.5)
    }
    const S = (lat, lon) => toScreen(lat, lon, this.lat, this.lon, this.z, w, h)
    if (this.block) {
      ctx.strokeStyle = 'rgba(255,160,64,0.9)'
      ctx.lineWidth = 1.5
      ctx.beginPath()
      this.block.forEach(([la, lo], k) => { const p = S(la, lo); k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y) })
      ctx.closePath()
      ctx.stroke()
    }
    if (this.cam) {
      const p = S(this.cam.lat, this.cam.lon)
      const hd = this.cam.heading * Math.PI / 180, half = (this.cam.fov || 60) / 2 * Math.PI / 180, r = 60
      ctx.fillStyle = 'rgba(80,180,255,0.28)'
      ctx.strokeStyle = 'rgba(120,200,255,0.9)'
      ctx.beginPath()
      ctx.moveTo(p.x, p.y)
      ctx.arc(p.x, p.y, r, hd - half - Math.PI / 2, hd + half - Math.PI / 2)
      ctx.closePath()
      ctx.fill(); ctx.stroke()
      ctx.fillStyle = '#4fb4ff'
      ctx.beginPath(); ctx.arc(p.x, p.y, 4, 0, 2 * Math.PI); ctx.fill()
    }
    if (this.pending) {
      const p = S(this.pending.lat, this.pending.lon)
      ctx.strokeStyle = '#ffeb3b'; ctx.lineWidth = 2
      ctx.beginPath(); ctx.arc(p.x, p.y, 7, 0, 2 * Math.PI); ctx.stroke()
    }
    if (this.noTiles) {
      ctx.fillStyle = 'rgba(255,255,255,0.75)'
      ctx.font = '11px system-ui, sans-serif'
      ctx.fillText('No map tiles (earthd --map-tiles)', 8, h - 8)
    }
  }
}
