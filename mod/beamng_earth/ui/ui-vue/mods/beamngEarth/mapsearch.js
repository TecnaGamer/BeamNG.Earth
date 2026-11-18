// Search presentation only. No timers, polling, dependencies or network assets.
const track = 'M7 4h10a5 5 0 0 1 5 5v6a5 5 0 0 1-5 5H7a5 5 0 0 1-5-5V9a5 5 0 0 1 5-5ZM8 8h8a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2Z'
const mountain = 'm2 20 7-14 4 8 3-5 6 11ZM7 10l2 2 2-2'
const road = 'M6 22 9 2m6 0 3 20M12 3v4m0 3v4m0 3v4'
const paths = {
  'race track': track, karting: track, 'drag strip': 'M7 22V2m10 20V2M7 6h10m-10 4h10m-10 4h10',
  rally: 'm4 22 7-6-5-6 11-8M15 3l3-1-1 3M3 8h1m15 10h1m-6 3h1',
  'off-road': 'M3 16v-5l3-1 3-5h7l3 6 2 1v4ZM7 10h9M8 5v5m5-5v5M5 16v3h3v-3m9 0v3h3v-3',
  motocross: 'M6 15a4 4 0 1 0 0 .1Zm12 0a4 4 0 1 0 0 .1ZM6 15l4-7 8 7m-8-7 3 7H6m3-10h4m2 0h3l1 3',
  'rock crawling': 'm2 20 2-8 7-2 4 4 5-1 2 7ZM4 12l5 4 6-2m-6 2 2 4M6 9l3-6 6 1 3 6',
  'mountain peak': mountain, 'mountain pass': 'm1 20 6-13 5 7 5-7 6 13M9 22l3-5 3 5',
  forest: 'm7 2-5 8h3l-4 6h5v6h2v-6h5l-4-6h3ZM17 6l-3 6h2l-3 6h4v4m1-4h5l-4-6h2Z',
  lake: 'M2 15q3-3 6 0t6 0 8 0M2 20q3-3 6 0t6 0 8 0M3 10l5-7 5 7m0-3 3-5 5 8',
  beach: 'M3 21h18M12 20l3-12M4 9a8 8 0 0 1 16 3ZM12 4l3-2M7 20l2-3h5',
  desert: 'M1 20q7-10 13-3l9 3M3 15l4-3 4 2m6-9a3 3 0 1 0 0 .1Z',
  airport: 'm12 2 2 8 8 5v2l-8-2v5l3 2H7l3-2v-5l-8 2v-2l8-5Z',
  bridge: 'M2 19h20M3 19V5m18 14V5M3 7q9 15 18 0M7 13v6m5-4v4m5-6v6',
  tunnel: 'M2 21V12a10 10 0 0 1 20 0v9M6 21v-9a6 6 0 0 1 12 0v9M12 21v-3m0-3v-2',
  dam: 'M7 2h10l4 20H3ZM7 7h10M6 12h12M2 18h4m12 0h4M10 3v18m4-18v18',
  canyon: 'm2 2 5 3-2 5 4 4-2 8m15-20-5 3 2 5-4 4 2 8M12 2v3m0 3v3m0 3v3m0 3v2',
  'scenic drive': road,
}
export const CATEGORIES = Object.entries(paths).map(([kind, path]) => ({ kind, path,
  label: ({ 'race track': 'Race tracks', rally: 'Rally / gravel', 'off-road': 'Off-road',
    'mountain peak': 'Peaks', forest: 'Forests', 'rock crawling': 'Rocks' })[kind] || kind[0].toUpperCase() + kind.slice(1),
  query: kind }))
export const QUICK = ['race track', 'rally', 'off-road', 'mountain peak', 'forest'].map(k => CATEGORIES.find(c => c.kind === k))
export const GROUPS = { saved: 'Saved / recent', category: 'Category places', name: 'Name match', address: 'Address' }
const addressKinds = new Set(['house', 'road', 'street', 'residential', 'pedestrian', 'service', 'living_street', 'tertiary', 'secondary', 'primary', 'unclassified'])
const pin = 'M12 22S4 14 4 9a8 8 0 1 1 16 0c0 5-8 13-8 13ZM12 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z'
const aliases = { peak: 'mountain peak', saddle: 'mountain pass', wood: 'forest', raceway: 'race track', aerodrome: 'airport', track: 'off-road' }
export function icon(kind) { return paths[aliases[kind] || kind] || pin }
export function validPoint(p) { return !!p && Number.isFinite(p.lat) && Number.isFinite(p.lon) && Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180 }
export function miles(a, b) {
  if (!validPoint(a) || !validPoint(b)) return null
  const rad = Math.PI / 180, x = Math.sin((b.lat - a.lat) * rad / 2), y = Math.sin((b.lon - a.lon) * rad / 2)
  const h = Math.min(1, Math.max(0, x * x + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * y * y))
  return 7917.5 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h))
}
export function present(r, player) {
  const group = r.saved ? 'saved' : r.result_type || (addressKinds.has(r.kind) ? 'address' : 'name')
  // Distances for category queries are relative to the resolved near/in origin, supplied by the server.
  const distance = Number.isFinite(r.distance_mi) ? r.distance_mi : miles(player, r)
  const raw = (r.kind || '').replaceAll('_', ' ')
  const label = !raw || ['wikipedia', 'wikidata'].includes(raw) ? 'Place' : raw[0].toUpperCase() + raw.slice(1)
  const kind = CATEGORIES.find(c => c.kind === (aliases[r.kind] || r.kind))?.label || (group === 'address' ? 'Address' : label)
  const meta = [kind, distance == null ? '' : `${distance.toFixed(1)} mi`, r.country].filter(Boolean).join(' · ')
  return { ...r, group, groupLabel: GROUPS[group] || GROUPS.name, icon: icon(r.kind), meta,
    // Category detail already contains these three fields; ordinary descriptions and addresses remain useful.
    description: group === 'category' ? '' : r.detail || (validPoint(r) ? `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}` : '') }
}
export function moveIndex(index, delta, length) {
  if (!length) return -1
  if (index < 0) return delta < 0 ? length - 1 : 0
  return (index + delta + length) % length
}
export function previewPosition(p, width, height) {
  const x = Math.max(24, Math.min(width - 24, p.x)), y = Math.max(142, Math.min(height - 70, p.y))
  return { x, y, offscreen: x !== p.x || y !== p.y }
}
