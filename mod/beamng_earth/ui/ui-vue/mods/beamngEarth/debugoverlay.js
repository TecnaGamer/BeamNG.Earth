// Draw each material's merged coverage once. Even-odd fill preserves holes, and
// only explicit appearance envelopes get an outline, never detector/sample rows.
export function paintDebugFeatures(ctx, data, { screen, width, world, alpha }) {
  const colors = new Map((data.legend || []).map(o => [o.id, o.color]))
  for (const feature of data.features || []) {
    const rings = feature.rings || (feature.ring ? [feature.ring] : [])
    ctx.beginPath()
    let visible = false
    for (const ring of rings) {
      if (ring.length < 3) continue
      const points = ring.map(([lon, lat]) => screen(lat, lon))
      // Keep each contour continuous across the date line, including its holes.
      points.forEach((p, i) => {
        if (feature.unwrapped) p.x = points[0].x + (ring[i][0] - ring[0][0]) / 360 * world
        else p.x += Math.round((points[0].x - p.x) / world) * world
      })
      const left = Math.min(...points.map(p => p.x)), right = Math.max(...points.map(p => p.x))
      for (const shift of [-world, 0, world]) {
        if (right + shift < 0 || left + shift > width) continue
        points.forEach((p, i) => i ? ctx.lineTo(p.x + shift, p.y) : ctx.moveTo(p.x + shift, p.y))
        ctx.closePath()
        visible = true
      }
    }
    if (!visible) continue
    ctx.fillStyle = colors.get(feature.type) || "#ffffff"
    ctx.globalAlpha = alpha
    ctx.fill("evenodd")
    if (feature.outline === true) {
      ctx.globalAlpha = 0.9; ctx.strokeStyle = ctx.fillStyle; ctx.lineWidth = 2; ctx.stroke()
    }
  }
  ctx.globalAlpha = 1
}
