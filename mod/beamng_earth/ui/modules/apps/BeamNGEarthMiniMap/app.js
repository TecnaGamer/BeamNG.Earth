// A live BeamNG.Earth road map in the standard UI-app HUD. M continues to open the full map.
var beamngEarthMiniMapMath = (function () {
  'use strict'
  var TILE = 256
  var MAX_LAT = 85.05112878
  function clampLat(lat) { return Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) }
  function wrapLon(lon) {
    var w = ((lon + 180) % 360 + 360) % 360 - 180
    return w === -180 && lon > 0 ? 180 : w
  }
  function project(lat, lon, z) {
    var s = TILE * Math.pow(2, z)
    var phi = clampLat(lat) * Math.PI / 180
    return {
      x: (lon + 180) / 360 * s,
      y: (1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2 * s,
    }
  }
  function visibleTiles(lat, lon, z, w, h) {
    z = Math.max(0, Math.min(18, Math.floor(Number(z))))
    w = Math.max(1, Number(w) || 1)
    h = Math.max(1, Number(h) || 1)
    var c = project(lat, lon, z)
    var left = c.x - w / 2, top = c.y - h / 2
    var n = 1 << z
    var x0 = Math.floor(left / TILE), x1 = Math.floor((left + w) / TILE)
    var y0 = Math.max(0, Math.floor(top / TILE)), y1 = Math.min(n - 1, Math.floor((top + h) / TILE))
    var out = []
    for (var ty = y0; ty <= y1; ty++) {
      for (var tx = x0; tx <= x1; tx++) {
        out.push({ z: z, x: ((tx % n) + n) % n, y: ty, ux: tx,
          left: tx * TILE - left, top: ty * TILE - top, size: TILE,
          key: z + '/' + tx + '/' + ty })
      }
    }
    return out
  }
  function toScreen(lat, lon, clat, clon, z, w, h) {
    var s = TILE * Math.pow(2, z)
    var p = project(lat, lon, z), c = project(clat, clon, z)
    var dx = p.x - c.x
    if (dx > s / 2) dx -= s
    if (dx < -s / 2) dx += s
    return { x: w / 2 + dx, y: h / 2 + (p.y - c.y) }
  }
  function centreFirst(tiles, w, h) {
    return tiles.slice().sort(function (a, b) {
      var ax = a.left + a.size / 2 - w / 2, ay = a.top + a.size / 2 - h / 2
      var bx = b.left + b.size / 2 - w / 2, by = b.top + b.size / 2 - h / 2
      return ax * ax + ay * ay - (bx * bx + by * by)
    })
  }
  function clipSegment(ax, ay, bx, by, w, h) {
    var dx = bx - ax, dy = by - ay, u0 = 0, u1 = 1
    var p = [-dx, dx, -dy, dy], q = [ax, w - ax, ay, h - ay]
    for (var i = 0; i < 4; i++) {
      if (p[i] === 0) { if (q[i] < 0) return null; continue }
      var t = q[i] / p[i]
      if (p[i] < 0) { if (t > u1) return null; if (t > u0) u0 = t }
      else { if (t < u0) return null; if (t < u1) u1 = t }
    }
    return [[ax + u0 * dx, ay + u0 * dy], [ax + u1 * dx, ay + u1 * dy]]
  }
  function routePath(points, from, to, clat, clon, z, w, h) {
    if (!Array.isArray(points) || points.length < 2) return ''
    from = Math.max(0, Math.floor(Number(from) || 0))
    to = Math.min(points.length - 1, Math.floor(Number(to) || 0))
    var out = [], last = null
    for (var i = from; i < to; i++) {
      var a = points[i], b = points[i + 1]
      if (!a || !b) { last = null; continue }
      var p0 = toScreen(Number(a[0]), Number(a[1]), clat, clon, z, w, h)
      var p1 = toScreen(Number(b[0]), Number(b[1]), clat, clon, z, w, h)
      var clipped = clipSegment(p0.x, p0.y, p1.x, p1.y, w, h)
      if (!clipped) { last = null; continue }
      var start = clipped[0], end = clipped[1]
      if (last && Math.abs(last[0] - start[0]) < 0.5 && Math.abs(last[1] - start[1]) < 0.5) {
        out.push('L' + end[0].toFixed(1) + ' ' + end[1].toFixed(1))
      } else {
        out.push('M' + start[0].toFixed(1) + ' ' + start[1].toFixed(1), 'L' + end[0].toFixed(1) + ' ' + end[1].toFixed(1))
      }
      last = end
    }
    return out.join(' ')
  }
  return { TILE: TILE, MAX_LAT: MAX_LAT, clampLat: clampLat, wrapLon: wrapLon,
    project: project, visibleTiles: visibleTiles, toScreen: toScreen, centreFirst: centreFirst, routePath: routePath }
})()

angular.module('beamng.apps')
.directive('beamngEarthMiniMap', [function () {
  'use strict'
  var STYLE_ID = 'beamng-earth-minimap-style'
  var TILE_DIR = '/temp/beamngEarth/maptiles/'
  var POLL_MS = 250
  var TILE_POLL_MS = 1000

  function addStyles() {
    if (document.getElementById(STYLE_ID)) return
    var style = document.createElement('style')
    style.id = STYLE_ID
    style.textContent = [
      '.earth-minimap { width:100%; height:100%; box-sizing:border-box; padding:6px; color:#f4f6f7; font:12px Arial,sans-serif; }',
      '.earth-minimap-card { position:relative; width:100%; height:100%; box-sizing:border-box; padding:5px; border:1px solid rgba(255,255,255,.32); border-radius:12px; background:rgba(12,16,20,.86); box-shadow:0 2px 12px rgba(0,0,0,.38); }',
      '.earth-minimap-window { position:absolute; inset:5px; overflow:hidden; border:1px solid rgba(255,255,255,.52); border-radius:8px; background:#243039; }',
      '.earth-minimap-tile { position:absolute; display:block; max-width:none; pointer-events:none; }',
      '.earth-minimap-tile.labels { opacity:.96; }',
      '.earth-minimap-routes { position:absolute; z-index:2; inset:0; width:100%; height:100%; overflow:hidden; pointer-events:none; }',
      '.earth-minimap-route-done { fill:none; stroke:rgba(10,18,24,.78); stroke-width:6; stroke-linecap:round; stroke-linejoin:round; }',
      '.earth-minimap-route-ahead { fill:none; stroke:#32a4ff; stroke-width:3.5; stroke-linecap:round; stroke-linejoin:round; filter:drop-shadow(0 1px 2px rgba(0,0,0,.9)); }',
      '.earth-minimap-top { position:absolute; z-index:3; top:4px; left:4px; right:4px; height:21px; display:flex; align-items:center; justify-content:space-between; gap:4px; padding:0 6px; border-radius:4px; background:rgba(10,14,18,.72); text-shadow:0 1px 2px #000; font-weight:700; letter-spacing:.04em; }',
      '.earth-minimap-gps { max-width:72%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#c6e2ff; font-size:10px; font-weight:500; letter-spacing:0; }',
      '.earth-minimap-compass { position:absolute; z-index:3; top:31px; left:7px; width:22px; height:22px; border:1px solid rgba(255,255,255,.72); border-radius:50%; background:rgba(10,14,18,.62); text-align:center; color:#fff; font-size:9px; line-height:10px; }',
      '.earth-minimap-compass:after { content:""; position:absolute; left:9px; top:7px; border-left:2px solid transparent; border-right:2px solid transparent; border-bottom:8px solid #ff655c; }',
      '.earth-minimap-player { position:absolute; z-index:4; left:50%; top:50%; width:18px; height:22px; margin:-11px 0 0 -9px; filter:drop-shadow(0 1px 2px rgba(0,0,0,.85)); }',
      '.earth-minimap-player:before { content:""; position:absolute; left:2px; top:0; border-left:7px solid transparent; border-right:7px solid transparent; border-bottom:18px solid #ffe65b; }',
      '.earth-minimap-player:after { content:""; position:absolute; left:6px; top:8px; width:6px; height:9px; border-radius:3px; background:#1780d2; border:1px solid #fff; }',
      '.earth-minimap-destination { position:absolute; z-index:3; width:12px; height:12px; margin:-6px; border:2px solid #fff; border-radius:50% 50% 50% 0; background:#ed554a; transform:rotate(-45deg); box-shadow:0 1px 3px #000; }',
      '.earth-minimap-empty { position:absolute; z-index:2; inset:35px 14px 27px; display:flex; align-items:center; justify-content:center; padding:12px; color:rgba(255,255,255,.82); background:rgba(10,14,18,.32); text-align:center; line-height:1.35; text-shadow:0 1px 3px #000; }',
      '.earth-minimap-credit { position:absolute; z-index:3; bottom:4px; left:5px; right:5px; overflow:hidden; color:rgba(255,255,255,.78); font-size:8px; line-height:11px; white-space:nowrap; text-overflow:ellipsis; text-shadow:0 1px 2px #000; }',
      '.earth-minimap-unavailable { filter:saturate(.55); }',
    ].join('')
    document.head.appendChild(style)
  }

  function imagePath(layer, t) {
    var path = layer === 'sat' ? '' : layer + '/'
    return TILE_DIR + path + t.z + '/' + t.x + '/' + t.y + (layer === 'labels' ? '.png' : '.jpg')
  }
  function makeVisible(tiles, ready, layer) {
    var base = [], labels = []
    tiles.forEach(function (t) {
      var q = { key:t.key, left:t.left, top:t.top, size:t.size }
      if (ready[layer][t.z + '/' + t.x + '/' + t.y]) base.push(Object.assign({ src:imagePath(layer, t) }, q))
      if (ready.labels[t.z + '/' + t.x + '/' + t.y]) labels.push(Object.assign({ src:imagePath('labels', t) }, q))
    })
    return { base:base, labels:labels }
  }

  return {
    template:
      '<div class="bngApp earth-minimap" ng-show="visible">' +
        '<div class="earth-minimap-card">' +
          '<div class="earth-minimap-window" ng-class="{\'earth-minimap-unavailable\': !connected}">' +
            '<img class="earth-minimap-tile" ng-repeat="t in baseTiles track by t.key" ng-src="{{t.src}}" ng-style="{left:t.left+\'px\',top:t.top+\'px\',width:t.size+\'px\',height:t.size+\'px\'}" draggable="false" />' +
            '<svg class="earth-minimap-routes" aria-hidden="true"><path class="earth-minimap-route-done" ng-if="routeDone" ng-attr-d="{{routeDone}}" /><path class="earth-minimap-route-ahead" ng-if="routeAhead" ng-attr-d="{{routeAhead}}" /></svg>' +
            '<img class="earth-minimap-tile labels" ng-repeat="t in labelTiles track by t.key" ng-src="{{t.src}}" ng-style="{left:t.left+\'px\',top:t.top+\'px\',width:t.size+\'px\',height:t.size+\'px\'}" draggable="false" />' +
            '<div class="earth-minimap-top"><span>EARTH</span><span class="earth-minimap-gps">{{ gpsText }}</span></div>' +
            '<div class="earth-minimap-compass">N</div>' +
            '<div class="earth-minimap-player" ng-style="{transform:\'rotate(\' + (heading || 0) + \'deg)\'}"></div>' +
            '<div class="earth-minimap-destination" ng-if="destination" ng-style="{left:destination.x+\'px\',top:destination.y+\'px\'}"></div>' +
            '<div class="earth-minimap-empty" ng-if="message">{{ message }}</div>' +
            '<div class="earth-minimap-credit">{{ creditText }}</div>' +
          '</div>' +
        '</div>' +
      '</div>',
    replace: true,
    link: function (scope, element) {
      addStyles()
      scope.visible = false
      scope.connected = false
      scope.message = ''
      scope.baseTiles = []
      scope.labelTiles = []
      scope.heading = 0
      scope.destination = null
      scope.gpsText = ''
      scope.routeDone = ''
      scope.routeAhead = ''
      scope.creditText = 'Map: Terrain Light by EOX IT Services GmbH; data © OpenStreetMap contributors and others'

      var destroyed = false, statePending = false, tilesPending = false
      var pollTimer = null, resizeObserver = null
      var lastTileAt = 0, lastTileSignature = '', currentMapPort = null
      var ready = { sat: Object.create(null), map: Object.create(null), labels: Object.create(null) }
      var wanted = []
      var player = null, gps = null, routePoints = [], routeRequestVersion = 0
      var mapOpen = false, mapPort = null, mapZoom = 16, mapLayer = 'map', routeVersion = 0, routeProgress = 1
      var lastPosition = null

      function engineLua(script, cb) {
        if (window.bngApi && window.bngApi.engineLua) window.bngApi.engineLua(script, cb)
      }
      function measure() {
        var win = element[0] && element[0].querySelector('.earth-minimap-window')
        return { w:win ? Math.max(1, win.clientWidth) : 220, h:win ? Math.max(1, win.clientHeight) : 220 }
      }
      function applyPosition(position) {
        if (!element[0] || lastPosition === position) return
        lastPosition = position
        var style = element[0].style
        style.position = 'fixed'
        style.width = '250px'
        style.height = '274px'
        style.zIndex = '20'
        style.top = ''
        style.right = ''
        style.bottom = ''
        style.left = ''
        if (position === 'top-right') { style.top = '100px'; style.right = '10px' }
        else if (position === 'top-left') { style.top = '100px'; style.left = '10px' }
        else if (position === 'bottom-right') { style.bottom = '76px'; style.right = '10px' }
        else { style.bottom = '54px'; style.left = '10px' }
      }
      function coord(p, i) {
        if (Array.isArray(p)) return Number(p[i])
        if (!p || typeof p !== 'object') return NaN
        return Number(p[String(i + 1)] !== undefined ? p[String(i + 1)] : (i === 0 ? p.lat : p.lon))
      }
      function fetchRoute(version) {
        version = Number(version) || 0
        if (!version) {
          routeRequestVersion = 0
          routeVersion = 0
          routePoints = []
          scope.routeDone = ''
          scope.routeAhead = ''
          return
        }
        if (version === routeVersion || version === routeRequestVersion) return
        routeRequestVersion = version
        engineLua('beamngEarth_settings and beamngEarth_settings.uiGpsRoute and beamngEarth_settings.uiGpsRoute() or nil', function (r) {
          if (destroyed) return
          scope.$evalAsync(function () {
            if (routeRequestVersion !== version) return
            routeRequestVersion = 0
            if (!r || Number(r.version) !== version || !r.points) { routePoints = []; routeVersion = version; updateDraw(); return }
            var points = Array.isArray(r.points) ? r.points : Object.keys(r.points).sort(function (a, b) { return Number(a) - Number(b) }).map(function (k) { return r.points[k] })
            routePoints = points.map(function (p) { return [coord(p, 0), coord(p, 1)] }).filter(function (p) { return Number.isFinite(p[0]) && Number.isFinite(p[1]) })
            routeVersion = version
            updateDraw()
          })
        })
      }
      function updateDraw() {
        if (!player) { wanted = []; scope.baseTiles = []; scope.labelTiles = []; scope.destination = null; scope.routeDone = ''; scope.routeAhead = ''; return }
        var size = measure()
        wanted = beamngEarthMiniMapMath.visibleTiles(player.lat, player.lon, mapZoom, size.w, size.h)
        var draw = makeVisible(wanted, ready, mapLayer === 'satellite' ? 'sat' : 'map')
        scope.baseTiles = draw.base
        scope.labelTiles = draw.labels
        var split = Math.max(0, Math.min(routePoints.length - 1, (Number(routeProgress) || 1) - 1))
        scope.routeDone = routePoints.length > 1 ? beamngEarthMiniMapMath.routePath(routePoints, 0, split + 1, player.lat, player.lon, mapZoom, size.w, size.h) : ''
        scope.routeAhead = routePoints.length > 1 ? beamngEarthMiniMapMath.routePath(routePoints, split, routePoints.length - 1, player.lat, player.lon, mapZoom, size.w, size.h) : ''
        if (gps) {
          var p = beamngEarthMiniMapMath.toScreen(gps.lat, gps.lon, player.lat, player.lon, mapZoom, size.w, size.h)
          scope.destination = p.x >= 5 && p.y >= 5 && p.x <= size.w - 5 && p.y <= size.h - 25 ? p : null
        } else scope.destination = null
      }
      function tileSignature(tiles) {
        return beamngEarthMiniMapMath.centreFirst(tiles, measure().w, measure().h).map(function (t) {
          return t.z + '/' + t.x + '/' + t.y
        }).join(',')
      }
      function askTiles() {
        if (destroyed || !scope.visible || tilesPending || !mapPort || !player || mapOpen || !wanted.length) return
        var size = measure()
        var ordered = beamngEarthMiniMapMath.centreFirst(wanted, size.w, size.h)
        var requests = []
        var baseLayer = mapLayer === 'satellite' ? 'sat' : 'map'
        ordered.forEach(function (t) { requests.push([t.z, t.x, t.y, baseLayer], [t.z, t.x, t.y, 'labels']) })
        var sig = tileSignature(wanted) + '|' + mapPort + '|' + mapZoom + '|' + baseLayer
        var now = Date.now()
        if (sig !== lastTileSignature || now - lastTileAt >= TILE_POLL_MS) {
          lastTileSignature = sig
          lastTileAt = now
          tilesPending = true
          var encoded = JSON.stringify(requests)
          engineLua("beamngEarth_settings and beamngEarth_settings.mapTiles and beamngEarth_settings.mapTiles(jsonDecode('" + encoded + "')) or {}", function (result) {
            if (destroyed) return
            scope.$evalAsync(function () {
              tilesPending = false
              ;(Array.isArray(result) ? result : []).forEach(function (key) {
                var parts = String(key).split('/')
                if (parts.length === 4 && ready[parts[0]]) ready[parts[0]][parts[1] + '/' + parts[2] + '/' + parts[3]] = true
              })
              updateDraw()
            })
          })
        }
      }
      function receiveState(s) {
        if (!s) { scope.visible = false; player = null; updateDraw(); return }
        if (currentMapPort !== s.mapPort) {
          currentMapPort = s.mapPort
          ready = { sat:Object.create(null), map:Object.create(null), labels:Object.create(null) }
          lastTileSignature = ''
        }
        scope.visible = !!s.visible
        scope.connected = !!s.connected && !!s.mapPort
        mapPort = s.mapPort || null
        mapOpen = !!s.mapOpen
        mapZoom = Math.max(12, Math.min(18, Math.round(Number(s.zoom) || 16)))
        mapLayer = s.layer === 'satellite' ? 'satellite' : 'map'
        applyPosition(s.position || 'bottom-left')
        scope.creditText = mapLayer === 'satellite'
          ? 'Satellite imagery; road labels: EOX; data © OpenStreetMap contributors and others'
          : 'Map: Terrain Light by EOX IT Services GmbH; data © OpenStreetMap contributors and others'
        player = s.player && Number.isFinite(Number(s.player.lat)) && Number.isFinite(Number(s.player.lon))
          ? { lat:Number(s.player.lat), lon:Number(s.player.lon), heading:s.player.heading == null ? null : Number(s.player.heading) }
          : null
        gps = s.gps && Number.isFinite(Number(s.gps.lat)) && Number.isFinite(Number(s.gps.lon))
          ? { lat:Number(s.gps.lat), lon:Number(s.gps.lon), name:s.gps.name, remaining:s.gps.remaining,
              routeVersion:Number(s.gps.routeVersion) || 0, progress:Number(s.gps.progress) || 1, planning:!!s.gps.planning }
          : null
        routeProgress = gps ? gps.progress : 1
        fetchRoute(gps && gps.routeVersion)
        scope.heading = player && player.heading != null ? player.heading : 0
        scope.gpsText = gps ? ('GPS · ' + (gps.name || (Number(gps.remaining) >= 0 ? (Number(gps.remaining) / 1609.344).toFixed(1) + ' mi' : 'destination')) + (gps.planning ? ' · planning' : '')) : ''
        if (!player) scope.message = !scope.connected ? (s.connected ? 'Map tiles unavailable' : 'Waiting for the world server…') : 'Waiting for player position…'
        else if (!scope.connected) scope.message = 'Map tiles unavailable'
        else scope.message = ''
        updateDraw()
        askTiles()
      }
      function poll() {
        if (destroyed || statePending) return
        statePending = true
        engineLua('beamngEarth_settings and beamngEarth_settings.uiMiniMapState and beamngEarth_settings.uiMiniMapState() or nil', function (s) {
          if (destroyed) return
          scope.$evalAsync(function () { statePending = false; receiveState(s) })
        })
      }

      poll()
      pollTimer = setInterval(poll, POLL_MS)
      if (window.ResizeObserver) {
        resizeObserver = new ResizeObserver(function () { updateDraw(); lastTileAt = 0; askTiles() })
        var win = element[0] && element[0].querySelector('.earth-minimap-window')
        if (win) resizeObserver.observe(win)
      }
      scope.$on('$destroy', function () {
        destroyed = true
        clearInterval(pollTimer)
        if (resizeObserver) resizeObserver.disconnect()
      })
    }
  }
}])
