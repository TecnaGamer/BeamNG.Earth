// BeamNG.Earth GPS (the user, 2026-10-25: "a large rectangle box with the player arrow, gps line, arrow indicators for
// turns, and two sets of distance, distance to next turn and distance to destination that has an estimated arrival
// time under it", a GTA-style minimap at the bottom left). The satellite photo from the world map's tiles
// (settings.mapTiles), turned so the car points up; the GPS route (settings.uiGpsRoute) with its next turn
// (gps.lua: the server's OSRM turn-by-turn); the speed limit under the car. Everything it draws comes from
// settings.uiGpsHud, polled POLL_MS. The math is in beamngEarthGpsMath (pure; mod/tests/test_gpsapp.mjs).
var beamngEarthGpsMath = (function () {
  'use strict'
  var TILE = 256
  var MAX_LAT = 85.05112878
  var M_FT = 3.280839895, M_MI = 1609.344

  function project(lat, lon, z) {
    var s = TILE * Math.pow(2, z)
    var phi = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * Math.PI / 180
    return { x: (lon + 180) / 360 * s, y: (1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2 * s }
  }

  // The zoom for a speed (m/s): close in town, farther out on a highway; prev keeps it until the speed is clearly in
  // another band (no flicker at a band's edge).
  var BANDS = [{ z: 17, upTo: 14 }, { z: 16, upTo: 28 }, { z: 15, upTo: Infinity }]
  var HYST = 2
  function zoomFor(speed, prev) {
    var v = Math.max(0, Number(speed) || 0)
    var want = BANDS[BANDS.length - 1].z
    for (var i = 0; i < BANDS.length; i++) { if (v < BANDS[i].upTo) { want = BANDS[i].z; break } }
    if (prev == null || prev === want) return want
    for (var j = 0; j < BANDS.length; j++) {
      if (BANDS[j].z !== prev) continue
      var lo = j > 0 ? BANDS[j - 1].upTo : 0, hi = BANDS[j].upTo
      if (v >= lo - HYST && v < hi + HYST) return prev      // still near the band it was in
    }
    return want
  }

  // The tiles at zoom z within radius r (px) of the player at (lat, lon): { z, x, y, left, top } with left / top
  // relative to the player's pixel, nearest first.
  function tilesAround(lat, lon, z, r) {
    var p = project(lat, lon, z), n = Math.pow(2, z), out = []
    var x0 = Math.floor((p.x - r) / TILE), x1 = Math.floor((p.x + r) / TILE)
    var y0 = Math.max(0, Math.floor((p.y - r) / TILE)), y1 = Math.min(n - 1, Math.floor((p.y + r) / TILE))
    for (var x = x0; x <= x1; x++) {
      for (var y = y0; y <= y1; y++) {
        var left = x * TILE - p.x, top = y * TILE - p.y
        var cx = Math.max(left, Math.min(0, left + TILE)), cy = Math.max(top, Math.min(0, top + TILE))
        if (cx * cx + cy * cy > r * r) continue                  // the square's nearest point is outside the circle
        out.push({ z: z, x: ((x % n) + n) % n, y: y, left: left, top: top, d: (left + TILE / 2) * (left + TILE / 2) + (top + TILE / 2) * (top + TILE / 2) })
      }
    }
    out.sort(function (a, b) { return a.d - b.d })
    return out
  }

  // Points [lat, lon] from index `from` on as pixels relative to the player (pz: the player's projected pixel),
  // only the stretches within r of it: runs [[{x, y}...]...] (a run breaks where the route leaves the circle).
  function routeRuns(points, from, pz, z, r) {
    var runs = [], run = [], prev = null, r2 = 4 * r * r
    for (var i = Math.max(0, from); i < points.length; i++) {
      var q = project(points[i][0], points[i][1], z)
      var pt = { x: q.x - pz.x, y: q.y - pz.y }
      var inside = pt.x * pt.x + pt.y * pt.y <= r2
      if (inside || (prev && prev.inside)) {
        if (!run.length && prev) run.push(prev.pt)
        run.push(pt)
      } else if (run.length) {
        runs.push(run)
        run = []
      }
      prev = { pt: pt, inside: inside }
    }
    if (run.length > 1) runs.push(run)
    return runs.filter(function (rn) { return rn.length > 1 })
  }

  function pathOf(runs) {
    return runs.map(function (rn) {
      return 'M' + rn.map(function (p) { return p.x.toFixed(1) + ' ' + p.y.toFixed(1) }).join(' L')
    }).join(' ')
  }

  // Chevrons along the runs every `step` px: { x, y, deg } (deg: the travel direction, 0 = east, clockwise on screen).
  function chevrons(runs, step, max) {
    var out = []
    runs.forEach(function (rn) {
      var carry = step * 0.5
      for (var i = 1; i < rn.length && out.length < max; i++) {
        var a = rn[i - 1], b = rn[i], dx = b.x - a.x, dy = b.y - a.y, L = Math.sqrt(dx * dx + dy * dy)
        if (L < 1e-6) continue
        var s = carry
        while (s <= L && out.length < max) {
          out.push({ x: a.x + dx * s / L, y: a.y + dy * s / L, deg: Math.atan2(dy, dx) * 180 / Math.PI })
          s += step
        }
        carry = s - L
      }
    })
    return out
  }

  // A distance as the GPS card says it: feet then miles (mph countries), metres then kilometres.
  function fmtDist(m, mph) {
    m = Math.max(0, Number(m) || 0)
    if (mph) {
      if (m < 0.1 * M_MI) return Math.round(m * M_FT / 50) * 50 + ' ft'
      var mi = m / M_MI
      return (mi < 100 ? mi.toFixed(1) : String(Math.round(mi))) + ' mi'
    }
    if (m < 1000) return Math.round(m / 10) * 10 + ' m'
    var km = m / 1000
    return (km < 100 ? km.toFixed(1) : String(Math.round(km))) + ' km'
  }

  // "2:14 PM" (12-hour where mph is posted, else "14:14") of minutes after midnight.
  function fmtClock(mins, mph) {
    mins = ((Math.floor(Number(mins) || 0) % 1440) + 1440) % 1440
    var h = Math.floor(mins / 60), mm = mins % 60
    return mph ? ((h % 12) || 12) + ':' + (mm < 10 ? '0' : '') + mm + (h < 12 ? ' AM' : ' PM')
      : (h < 10 ? '0' : '') + h + ':' + (mm < 10 ? '0' : '') + mm
  }

  // "Arrive 2:14 AM · 18 min" (12-hour clock where mph is posted, else 24-hour), from eta seconds and now (ms).
  // gameMins: the game's clock (minutes after midnight): the arrival on it; without it, the PC's clock.
  function fmtArrive(eta, nowMs, mph, gameMins) {
    eta = Math.max(0, Number(eta) || 0)
    var clock
    if (gameMins != null && isFinite(gameMins)) {
      clock = fmtClock(Number(gameMins) + eta / 60, mph)
    } else {
      var d = new Date((Number(nowMs) || 0) + eta * 1000)
      clock = fmtClock(d.getHours() * 60 + d.getMinutes(), mph)
    }
    var mins = Math.round(eta / 60)
    var left = mins < 60 ? mins + ' min' : Math.floor(mins / 60) + ' h ' + (mins % 60 < 10 ? '0' : '') + (mins % 60) + ' min'
    return 'Arrive ' + clock + ' · ' + left
  }

  // The turn card's arrow (a 34 x 34 box, stroked) for routing.py's arrow names.
  var ARROWS = {
    'straight': 'M17 30 L17 6 M10 13 L17 6 L24 13',
    'slight-left': 'M20 30 L20 18 Q20 13 15 9 L9 4 M8 12 L9 4 L17 5',
    'slight-right': 'M14 30 L14 18 Q14 13 19 9 L25 4 M26 12 L25 4 L17 5',
    'left': 'M24 30 L24 15 Q24 11 20 11 L7 11 M13 5 L7 11 L13 17',
    'right': 'M10 30 L10 15 Q10 11 14 11 L27 11 M21 5 L27 11 L21 17',
    'sharp-left': 'M24 30 L24 8 L9 23 M9 15 L9 23 L17 23',
    'sharp-right': 'M10 30 L10 8 L25 23 M25 15 L25 23 L17 23',
    'uturn': 'M22 30 L22 13 Q22 5 15 5 Q8 5 8 13 L8 22 M3 17 L8 22 L13 17',
    'roundabout': 'M17 30 L17 22 M17 22 A6 6 0 1 1 21 12 L27 6 M21 6 L27 6 L27 12',
    'arrive': 'M17 31 L17 4 M17 5 L28 9 L17 13'
  }
  function arrowPath(name) { return ARROWS[name] || ARROWS.straight }

  return { TILE: TILE, project: project, zoomFor: zoomFor, tilesAround: tilesAround, routeRuns: routeRuns, pathOf: pathOf,
           chevrons: chevrons, fmtDist: fmtDist, fmtArrive: fmtArrive, fmtClock: fmtClock, arrowPath: arrowPath }
})()

angular.module('beamng.apps')
.directive('beamngEarthGps', [function () {
  'use strict'
  var GM = beamngEarthGpsMath
  var TILE_DIR = '/temp/beamngEarth/maptiles/'
  var POLL_MS = 250, POSE_MS = 50, TILES_MS = 500, ASK_MAX = 24
  var MAX_LEAD_S = 0.3                   // the map moves on from the last pose for at most this long
  var ROUTE = '#c43cff'                  // the canvas design's magenta (the user, 2026-10-25: "keep our design")
  var PLAYER_Y = 0.70                    // the car's spot, down the box (more road ahead than behind)

  function addStyles() {
    if (document.getElementById('beamng-earth-gps-styles')) return
    var st = document.createElement('style')
    st.id = 'beamng-earth-gps-styles'
    st.textContent = [
      '.bne-gps { position:relative; width:100%; height:100%; overflow:hidden; border-radius:14px; box-sizing:border-box;',
      '  border:2px solid rgba(255,255,255,.22); background:#2c3527; box-shadow:0 8px 24px rgba(0,0,0,.5);',
      "  font-family:'Inter Tight','Segoe UI',system-ui,sans-serif; color:#f4f6f8; pointer-events:none; }",
      '.bne-gps-rot { position:absolute; left:0; top:0; width:0; height:0; transform-origin:0 0; will-change:transform; }',
      '.bne-gps-tile { position:absolute; width:256px; height:256px; }',
      '.bne-gps-svg { position:absolute; overflow:visible; }',
      '.bne-gps-glass { position:absolute; background:rgba(12,14,18,.82); border-radius:12px; }',
      '.bne-gps-card { left:10px; top:10px; display:flex; align-items:center; gap:10px; padding:8px 12px 8px 8px; max-width:calc(100% - 76px); }',
      '.bne-gps-icon { width:44px; height:44px; flex:none; border-radius:9px; display:flex; align-items:center; justify-content:center; }',
      '.bne-gps-turnd { font-size:24px; font-weight:800; line-height:1; letter-spacing:-.4px; }',
      '.bne-gps-turnt { font-size:12px; font-weight:600; color:#c9ced6; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:3px; }',
      '.bne-gps-compass { right:10px; top:10px; width:36px; height:36px; border-radius:18px; }',
      '.bne-gps-clock { right:10px; top:52px; padding:4px 9px; font-size:14px; font-weight:800; letter-spacing:.2px; border-radius:9px; }',
      '.bne-gps-ring { position:absolute; inset:0; }',
      '.bne-gps-n { position:absolute; left:50%; top:1px; width:14px; margin-left:-7px; text-align:center; font-size:12px; font-weight:800; }',
      '.bne-gps-limit { left:10px; bottom:10px; display:flex; align-items:center; gap:8px; padding:6px 10px 6px 6px; max-width:55%; }',
      '.bne-gps-us { width:28px; height:34px; box-sizing:border-box; border:2px solid #111; border-radius:3px; background:#fff; color:#111;',
      '  display:flex; flex-direction:column; align-items:center; justify-content:center; line-height:1; flex:none; }',
      '.bne-gps-us b { font-size:5.5px; font-weight:800; } .bne-gps-us i { font-style:normal; font-size:14px; font-weight:900; }',
      '.bne-gps-eu { width:32px; height:32px; box-sizing:border-box; border:4px solid #c8102e; border-radius:16px; background:#fff; color:#111;',
      '  display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:900; flex:none; }',
      '.bne-gps-street { font-size:13px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }',
      '.bne-gps-dest { right:10px; bottom:10px; padding:8px 12px; text-align:right; }',
      '.bne-gps-destd { font-size:20px; font-weight:800; line-height:1; letter-spacing:-.3px; }',
      '.bne-gps-desta { font-size:11px; font-weight:600; color:#c9ced6; margin-top:3px; white-space:nowrap; }',
      '.bne-gps-off { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:13px; color:#c9ced6; }'
    ].join('\n')
    document.head.appendChild(st)
  }

  return {
    template:
      '<div class="bngApp bne-gps" ng-show="hud.visible" style="background-color:#2c3527;">' +
        '<div class="bne-gps-rot">' +
          '<img class="bne-gps-tile" ng-repeat="t in tiles track by t.key" ng-src="{{t.src}}" ng-style="{left: t.left + \'px\', top: t.top + \'px\'}" />' +
          '<svg class="bne-gps-svg" ng-attr-style="left:{{S.x - R}}px; top:{{S.y - R}}px" ng-attr-width="{{2 * R}}" ng-attr-height="{{2 * R}}" ng-attr-view_box="{{S.x - R}} {{S.y - R}} {{2 * R}} {{2 * R}}" aria-hidden="true">' +
            '<path ng-if="routeD" ng-attr-d="{{routeD}}" fill="none" stroke="#120818" stroke-width="13" stroke-linecap="round" stroke-linejoin="round" opacity="0.75" />' +
            '<path ng-if="routeD" ng-attr-d="{{routeD}}" fill="none" stroke="' + ROUTE + '" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" />' +
            '<path ng-repeat="c in chev" ng-attr-transform="translate({{c.x}} {{c.y}}) rotate({{c.deg}})" d="M-4 -5 L2 0 L-4 5" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" opacity="0.9" />' +
            '<g ng-if="turnAt" ng-attr-transform="translate({{turnAt.x}} {{turnAt.y}})"><circle r="10" fill="#fff" stroke="#120818" stroke-width="2" /><circle r="3.5" fill="' + ROUTE + '" /></g>' +
            '<g ng-if="destAt" class="bne-gps-pin" ng-attr-transform="translate({{destAt.x}} {{destAt.y}})"><path d="M0 -26 C-8 -26 -13 -20 -13 -13 C-13 -4 0 8 0 8 C0 8 13 -4 13 -13 C13 -20 8 -26 0 -26 Z" fill="' + ROUTE + '" stroke="#fff" stroke-width="2.5" /><circle cy="-13" r="4.5" fill="#fff" /></g>' +
          '</svg>' +
        '</div>' +
        '<svg class="bne-gps-svg" ng-attr-style="left:{{P.x - 16}}px; top:{{P.y - 20}}px" width="32" height="40" viewBox="-16 -20 32 40" aria-hidden="true">' +
          '<path d="M0 -15 L12 15 L0 8 L-12 15 Z" fill="#fff" stroke="#0b0d10" stroke-width="2.5" stroke-linejoin="round" />' +
        '</svg>' +
        '<div class="bne-gps-off" ng-if="!hud.mapPort">Map not connected</div>' +
        '<div class="bne-gps-glass bne-gps-card" ng-if="next">' +
          '<div class="bne-gps-icon" style="background:' + ROUTE + '"><svg width="30" height="30" viewBox="0 0 34 34" aria-hidden="true"><path ng-attr-d="{{nextArrow}}" fill="none" stroke="#fff" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round" /></svg></div>' +
          '<div style="min-width:0"><div class="bne-gps-turnd">{{nextDist}}</div><div class="bne-gps-turnt">{{next.text}}</div></div>' +
        '</div>' +
        '<div class="bne-gps-glass bne-gps-compass"><div class="bne-gps-ring"><div class="bne-gps-n">N</div></div></div>' +
        '<div class="bne-gps-glass bne-gps-clock" ng-if="hud.clock != null">{{clockText}}</div>' +
        '<div class="bne-gps-glass bne-gps-limit" ng-if="hud.limit || hud.street">' +
          '<div class="bne-gps-us" ng-if="hud.limit && hud.limit.unit === \'mph\'"><b>SPEED</b><b>LIMIT</b><i>{{hud.limit.value}}</i></div>' +
          '<div class="bne-gps-eu" ng-if="hud.limit && hud.limit.unit !== \'mph\'">{{hud.limit.value}}</div>' +
          '<div class="bne-gps-street" ng-if="hud.street">{{hud.street}}</div>' +
        '</div>' +
        '<div class="bne-gps-glass bne-gps-dest" ng-if="hud.gps">' +
          '<div class="bne-gps-destd">{{destDist}}</div>' +
          '<div class="bne-gps-desta">{{destLine}}</div>' +
        '</div>' +
      '</div>',
    replace: true,
    link: function (scope, element) {
      addStyles()
      scope.hud = { visible: false }
      scope.tiles = []
      scope.chev = []
      scope.heading = 0
      scope.R = 300
      scope.P = { x: 0, y: 0 }
      var zoom = null, routeVer = 0, routeAsked = 0, points = [], ready = {}, lastAsk = 0, alive = true
      var origin = null                  // { x, y, z }: the fixed pixel the map layer is drawn from (re-set on a zoom change or far away)
      var pose = null                    // { x, y (px at zoom), h, v (px/s east, north-down), t }: the last fast pose
      var shown = null                   // { x, y, h }: where the map is drawn now (moved on every frame)
      scope.S = { x: 0, y: 0 }
      var rotEl = element[0].querySelector('.bne-gps-rot'), ringEl = element[0].querySelector('.bne-gps-ring'),
        nEl = element[0].querySelector('.bne-gps-n')

      function engineLua(script, cb) { if (window.bngApi && window.bngApi.engineLua) window.bngApi.engineLua(script, cb) }

      function size() {
        var el = element[0]
        return { w: el.clientWidth || 440, h: el.clientHeight || 260 }
      }

      // a route point as the game hands it over: [lat, lon], or a table keyed "1" / "2" (or lat / lon)
      function coord(p, i) {
        if (Array.isArray(p)) return Number(p[i])
        if (!p || typeof p !== 'object') return NaN
        return Number(p[String(i + 1)] !== undefined ? p[String(i + 1)] : (i === 0 ? p.lat : p.lon))
      }

      function fetchRoute(version) {
        if (!version) { routeVer = 0; points = []; return }
        if (version === routeVer || version === routeAsked) return
        routeAsked = version
        engineLua('beamngEarth_settings and beamngEarth_settings.uiGpsRoute and beamngEarth_settings.uiGpsRoute() or nil', function (r) {
          scope.$evalAsync(function () {
            routeAsked = 0
            if (!r || Number(r.version) !== version || !r.points) { points = []; routeVer = version; return }
            var pts = Array.isArray(r.points) ? r.points : Object.keys(r.points).sort(function (a, b) { return a - b }).map(function (k) { return r.points[k] })
            points = pts.map(function (p) { return [coord(p, 0), coord(p, 1)] })
              .filter(function (p) { return isFinite(p[0]) && isFinite(p[1]) })
            routeVer = version
          })
        })
      }

      function askTiles(wanted) {
        var now = Date.now()
        if (now - lastAsk < TILES_MS || scope.hud.mapOpen || !scope.hud.mapPort) return
        lastAsk = now
        var missing = wanted.filter(function (t) { return !ready[t.z + '/' + t.x + '/' + t.y] }).slice(0, ASK_MAX)
          .map(function (t) { return [t.z, t.x, t.y, 'sat'] })
        if (!missing.length) return
        engineLua("beamngEarth_settings and beamngEarth_settings.mapTiles and beamngEarth_settings.mapTiles(jsonDecode('" + JSON.stringify(missing) + "')) or {}", function (r) {
          if (!Array.isArray(r)) return
          scope.$evalAsync(function () {
            r.forEach(function (k) { if (k.indexOf('sat/') === 0) ready[k.slice(4)] = true })
          })
        })
      }

      function draw() {
        var h = scope.hud, pl = h.player
        var sz = size()
        scope.P = { x: sz.w / 2, y: sz.h * PLAYER_Y }
        scope.R = Math.ceil(Math.sqrt(sz.w * sz.w + sz.h * sz.h))
        if (!pl) { scope.tiles = []; scope.routeD = ''; scope.chev = []; scope.turnAt = null; scope.destAt = null; return }
        zoom = GM.zoomFor(pl.speed, zoom)
        var pz0 = GM.project(pl.lat, pl.lon, zoom)
        if (!origin || origin.z !== zoom || Math.abs(pz0.x - origin.x) > 4000 || Math.abs(pz0.y - origin.y) > 4000) {
          origin = { x: Math.round(pz0.x), y: Math.round(pz0.y), z: zoom }
          shown = null
          pose = null                    // (in the old zoom's pixels)
        }
        var ox = pz0.x - origin.x, oy = pz0.y - origin.y
        scope.S = { x: ox, y: oy }
        if (!pose && rotEl) {            // until the first fast pose: placed here
          var h0 = Number(pl.heading) || 0
          rotEl.style.transform = 'translate(' + scope.P.x + 'px,' + scope.P.y + 'px) rotate(' + (-h0) + 'deg) translate(' + (-ox) + 'px,' + (-oy) + 'px)'
        }
        var wanted = GM.tilesAround(pl.lat, pl.lon, zoom, scope.R)
        askTiles(wanted)
        scope.tiles = wanted.filter(function (t) { return ready[t.z + '/' + t.x + '/' + t.y] }).map(function (t) {
          return { key: t.z + '/' + t.x + '/' + t.y, left: t.left + ox, top: t.top + oy, src: TILE_DIR + t.z + '/' + t.x + '/' + t.y + '.jpg' }
        })
        var pz = GM.project(pl.lat, pl.lon, zoom)
        var g = h.gps
        if (g && points.length > 1) {
          var runs = GM.routeRuns(points, Math.max(0, (Number(g.progress) || 1) - 1), pz, zoom, scope.R)
            .map(function (rn) { return rn.map(function (q) { return { x: q.x + ox, y: q.y + oy } }) })
          scope.routeD = GM.pathOf(runs)
          scope.chev = GM.chevrons(runs, 70, 40)
        } else {
          scope.routeD = ''
          scope.chev = []
        }
        function rel(lat, lon) { var q = GM.project(lat, lon, zoom); return { x: q.x - pz.x, y: q.y - pz.y } }
        function abs(q) { return q ? { x: q.x + ox, y: q.y + oy } : null }
        scope.turnAt = (g && g.next && isFinite(g.next.lat)) ? rel(g.next.lat, g.next.lon) : null
        scope.destAt = (g && isFinite(g.lat)) ? rel(g.lat, g.lon) : null
        if (scope.destAt) {      // off the box: kept at its edge, in its direction (a pin you can still follow)
          var d = Math.sqrt(scope.destAt.x * scope.destAt.x + scope.destAt.y * scope.destAt.y), lim = scope.R * 0.42
          if (d > lim) { scope.destAt = { x: scope.destAt.x * lim / d, y: scope.destAt.y * lim / d } }
        }
        scope.turnAt = abs(scope.turnAt)
        scope.destAt = abs(scope.destAt)
        scope.next = g && g.next ? g.next : null
        scope.nextArrow = scope.next ? GM.arrowPath(scope.next.arrow) : ''
        scope.nextDist = scope.next ? GM.fmtDist(scope.next.dist, h.mph) : ''
        if (g) {
          scope.destDist = g.remaining != null ? GM.fmtDist(g.remaining, h.mph) : ''
          scope.destLine = g.planning && !g.routeVersion ? 'Planning route…'
            : (g.eta != null ? GM.fmtArrive(g.eta, Date.now(), h.mph, h.clock) : (g.name || ''))
        }
      }

      function poll() {
        if (!alive) return
        engineLua('beamngEarth_settings and beamngEarth_settings.uiGpsHud and beamngEarth_settings.uiGpsHud() or nil', function (s) {
          scope.$evalAsync(function () {
            scope.hud = s || { visible: false }
            scope.clockText = s && s.clock != null ? GM.fmtClock(s.clock, s.mph !== false) : ''
            fetchRoute(s && s.gps ? Number(s.gps.routeVersion) || 0 : 0)
            draw()
          })
        })
      }
      function pollPose() {
        if (!alive || !scope.hud.visible) return
        engineLua('beamngEarth_settings and beamngEarth_settings.uiGpsPose and beamngEarth_settings.uiGpsPose() or nil', function (q) {
          if (!q || !origin || !isFinite(q.lat)) return
          var p = GM.project(q.lat, q.lon, origin.z)
          var h = Number(q.heading) || 0, mpp = 156543.03392 * Math.cos(q.lat * Math.PI / 180) / Math.pow(2, origin.z)
          var v = (Number(q.speed) || 0) / mpp
          pose = { x: p.x - origin.x, y: p.y - origin.y, h: h, vx: v * Math.sin(h * Math.PI / 180), vy: -v * Math.cos(h * Math.PI / 180), t: performance.now() }
        })
      }
      function frame() {
        if (!alive) return
        requestAnimationFrame(frame)
        if (!pose || !rotEl) return
        var lead = Math.min(MAX_LEAD_S, (performance.now() - pose.t) / 1000)
        var tx = pose.x + pose.vx * lead, ty = pose.y + pose.vy * lead
        if (!shown) shown = { x: tx, y: ty, h: pose.h }
        var dh = ((pose.h - shown.h + 540) % 360) - 180            // the short way round
        shown = { x: shown.x + (tx - shown.x) * 0.5, y: shown.y + (ty - shown.y) * 0.5, h: shown.h + dh * 0.25 }
        rotEl.style.transform = 'translate(' + scope.P.x + 'px,' + scope.P.y + 'px) rotate(' + (-shown.h) + 'deg) translate(' + (-shown.x) + 'px,' + (-shown.y) + 'px)'
        if (ringEl) ringEl.style.transform = 'rotate(' + (-shown.h) + 'deg)'
        if (nEl) nEl.style.transform = 'rotate(' + shown.h + 'deg)'
        var pins = rotEl.querySelectorAll('.bne-gps-pin')
        for (var i = 0; i < pins.length; i++) {
          var base = pins[i].getAttribute('transform').replace(/ rotate\([^)]*\)/, '')
          pins[i].setAttribute('transform', base + ' rotate(' + shown.h + ')')
        }
      }
      var timer = setInterval(poll, POLL_MS), poseTimer = setInterval(pollPose, POSE_MS)
      poll()
      requestAnimationFrame(frame)
      scope.$on('$destroy', function () { alive = false; clearInterval(timer); clearInterval(poseTimer) })
    }
  }
}]);
