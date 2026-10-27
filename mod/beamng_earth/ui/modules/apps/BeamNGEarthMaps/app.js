// The BeamNG.Earth phone app. State comes in a 'BeamNGEarthMaps' hook from earthPhone.lua (5 a second, no vehicle
// speed). The map is the companion server's tile files, asked for through settings.mapTiles (the game's UI cannot
// fetch http); the route, the car and the dropped pin are drawn on a canvas over them.
//
// engineLua note: a string without a callback is compiled as a Lua STATEMENT ("beamngEarth_earthPhone and ..." is
// not one: it failed with "'=' expected near 'and'"), so side-effect calls go in "if ... then ... end" and only the
// calls that want a value (with a callback) are expressions.
angular.module('beamng.apps')
.directive('beamngEarthMaps', [function () {
  return {
    templateUrl: '/ui/modules/apps/BeamNGEarthMaps/app.html',
    replace: true,
    link: function (scope, element) {
      'use strict'
      var BASE_W = 340, BASE_H = 737          // the frame's design size; fit() scales it to the app's box
      var TILE = 256
      var TILE_DIR = '/temp/beamngEarth/maptiles/'
      // street: the Apple-style map, dark: the same in the dark palette, satellite: the imagery with its labels
      var LAYERS = {
        street: [{ id: 'apple', ext: 'jpg' }, { id: 'labels', ext: 'png' }],
        dark: [{ id: 'appledark', ext: 'jpg' }, { id: 'labelsdark', ext: 'png' }],
        satellite: [{ id: 'sathd', ext: 'jpg' }, { id: 'labels', ext: 'png' }]
      }
      var LAYER_NAMES = { street: 'Map', dark: 'Dark', satellite: 'Satellite' }
      var Z_MIN = 5, Z_MAX = 16
      var ASK_MS = 400, TYPE_MS = 260, MIN_CHARS = 3
      var WHEEL_SENS = 0.0022                  // wheel px -> zoom levels: a notch is a quarter step, a trackpad finer
      var ROUTE_BLUE = '#0a84ff'
      var LAYER_KEY = 'beamngEarth.phone.layer'
      var RECENT_KEY = 'beamngEarth.phone.recent'
      var RECENT_MAX = 12                      // kept; the drawer shows 4 before it scrolls inside
      var RECENT_SHOWN = 4
      var LATLON_RE = /^[-+]?\d{1,3}(\.\d+)?[°\s]*[NSns]?[,\s;]+[-+]?\d{1,3}(\.\d+)?/   // (the big map's own rule)
      var NOW_M = 35                           // metres to the maneuver at which the caption says "Now"
      var BUSY_MS = 2500                       // the pin card holds with "Planning…" this long at most
      var busyTimer = null

      // one path set per maneuver; turns are that one mirrored / rotated (24 x 24, base at 12,21)
      var GLYPHS = {
        right: { paths: ['M12 21v-9.5a4 4 0 014-4h3.6', 'M16.4 4.5L20 8l-3.6 3.5'], rot: 0, flip: false },
        left: { paths: ['M12 21v-9.5a4 4 0 014-4h3.6', 'M16.4 4.5L20 8l-3.6 3.5'], rot: 0, flip: true },
        'slight-right': { paths: ['M12 21v-9.5a4 4 0 014-4h3.6', 'M16.4 4.5L20 8l-3.6 3.5'], rot: -35, flip: false },
        'slight-left': { paths: ['M12 21v-9.5a4 4 0 014-4h3.6', 'M16.4 4.5L20 8l-3.6 3.5'], rot: -35, flip: true },
        'sharp-right': { paths: ['M12 21v-9.5a4 4 0 014-4h3.6', 'M16.4 4.5L20 8l-3.6 3.5'], rot: 35, flip: false },
        'sharp-left': { paths: ['M12 21v-9.5a4 4 0 014-4h3.6', 'M16.4 4.5L20 8l-3.6 3.5'], rot: 35, flip: true },
        straight: { paths: ['M12 21V5', 'M7.5 9.5L12 5l4.5 4.5'], rot: 0, flip: false },
        merge: { paths: ['M12 21v-8', 'M12 13L7.5 8.5h9z'], rot: 0, flip: false },
        ramp: { paths: ['M12 21v-7a4 4 0 014-4h3.6', 'M16.4 6.5L20 10l-3.6 3.5'], rot: 0, flip: false },
        uturn: { paths: ['M8 21V11a4.5 4.5 0 019 0v3.5', 'M13.5 11L17 14.5l3.5-3.5'], rot: 0, flip: false },
        roundabout: { paths: ['M12 3.5a8.5 8.5 0 018.5 8.5', 'M18 8.5L20.5 12l3.5-3.5'], rot: 0, flip: false },
        arrive: { paths: ['M12 21V4', 'M12 4.5h7.5l-2.2 3.2 2.2 3.2H12'], rot: 0, flip: false },
        depart: { paths: ['M12 21V8', 'M8 11.5L12 7.5l4 4'], rot: 0, flip: false }
      }

      var ui = { visible: false, connected: false, player: null, limit: null, search: null, route: null, pin: null }
      var view = { lat: 39.2904, lon: -76.6122, zoom: 15, z: 15 }   // zoom is continuous, z the tile level it uses
      var follow = true
      var width = 0, height = 0, origin = { left: 0, top: 0 }, scale = 1
      var layer = readLayer()
      var recents = readRecents()
      var ready = {}
      var routePoints = null, routeVersion = -1
      var typeTimer = null
      var hostEl, fitEl, phoneEl, mapEl, canvas, ctx, zoomEl

      scope.ui = ui
      scope.q = ''
      scope.searchOpen = true
      scope.layersOpen = false
      scope.tiles = []
      scope.glyph = { show: false, paths: [], style: {}, road: '' }
      scope.eta = { value: '', unit: '' }
      scope.remaining = ''
      scope.distance = ''
      scope.caption = ''
      scope.captionNow = false
      scope.suggestions = []
      scope.layer = layer
      scope.layerName = LAYER_NAMES[layer]
      scope.layerOptions = ['street', 'dark', 'satellite'].map(function (k) {
        return { id: k, name: LAYER_NAMES[k] }
      })
      scope.pinCard = null
      scope.pinBusy = false
      scope.coord = null                       // a typed "lat, lon" (places.lua parses it, as the big map does)

      function readLayer() {
        try {
          var v = localStorage.getItem(LAYER_KEY)
          if (v === 'terrain') v = 'satellite'          // (before the rename)
          return LAYERS[v] ? v : 'street'
        } catch (e) { return 'street' }
      }
      function saveLayer(v) {
        try { localStorage.setItem(LAYER_KEY, v) } catch (e) { /* storage blocked: not kept */ }
      }
      function readRecents() {
        try {
          var v = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
          return angular.isArray(v) ? v.slice(0, RECENT_MAX) : []
        } catch (e) { return [] }
      }
      function remember(r) {
        if (!r || !r.name || typeof r.lat !== 'number' || typeof r.lon !== 'number') return
        recents = [{ name: r.name, lat: r.lat, lon: r.lon, detail: r.detail || '' }]
          .concat(recents.filter(function (x) { return x.name !== r.name })).slice(0, RECENT_MAX)
        try { localStorage.setItem(RECENT_KEY, JSON.stringify(recents)) } catch (e) { /* not kept */ }
      }
      // the X on a recent: forgotten for good (the drawer's list and the stored one)
      function forget(r) {
        recents = recents.filter(function (x) { return !(x.name === r.name && x.lat === r.lat && x.lon === r.lon) })
        try { localStorage.setItem(RECENT_KEY, JSON.stringify(recents)) } catch (e) { /* not kept */ }
        suggest((scope.q || '').trim())
        scope.$applyAsync()
      }
      scope.forget = function (r, ev) {
        if (ev) ev.stopPropagation()
        forget(r)
      }

      // ---- Lua ---------------------------------------------------------------------------------
      function lua(script, cb) {
        if (!window.bngApi || !window.bngApi.engineLua) return
        if (cb) window.bngApi.engineLua(script, cb)
        else window.bngApi.engineLua(script)
      }
      function ser(v) {
        return (window.bngApi && window.bngApi.serializeToLua) ? window.bngApi.serializeToLua(v) : JSON.stringify(v)
      }
      // a side-effect call: a statement, never a bare expression
      function call(name) {
        var args = Array.prototype.slice.call(arguments, 1).map(ser)
        lua('if beamngEarth_earthPhone then beamngEarth_earthPhone.' + name + '(' + args.join(',') + ') end')
      }

      // ---- size: the frame is drawn at 340 x 737 and scaled to the app's box, whatever the player resizes it to
      // If the app box itself runs off the top of the screen (a short display), it is first shrunk to fit: the
      // bottom stays above the speedometer and the frame scales back into the new box.
      function clampToScreen() {
        var box = element[0].parentElement
        if (!box) return
        var r = box.getBoundingClientRect()
        if (!r.height || r.top >= 6) return
        var keepBottom = Math.max(0, window.innerHeight - r.bottom)
        var h = Math.max(300, window.innerHeight - keepBottom - 8)
        if (h >= r.height) return
        box.style.height = Math.round(h) + 'px'
        box.style.width = Math.round(h * (BASE_W / BASE_H)) + 'px'
      }

      function fit() {
        if (!hostEl || !fitEl) return
        clampToScreen()
        var w = hostEl.clientWidth, h = hostEl.clientHeight
        if (!w || !h) return
        scale = Math.min(w / BASE_W, h / BASE_H)
        fitEl.style.transform = 'scale(' + scale + ')'
        fitEl.style.left = Math.max(0, (w - BASE_W * scale) / 2) + 'px'
        fitEl.style.top = Math.max(0, (h - BASE_H * scale) / 2) + 'px'
      }

      // ---- the projection (Web Mercator, 256 px tiles) -----------------------------------------
      function size(z) { return TILE * Math.pow(2, z) }
      // the zoom is continuous: the tiles are built at a whole level and the layer is scaled the rest of the way,
      // so the wheel moves the map a little at a time instead of jumping a level
      function tileZ() { return Math.max(Z_MIN, Math.min(Z_MAX, Math.round(view.zoom))) }
      function zoomScale() { return Math.pow(2, view.zoom - view.z) }
      function toX(lon, z) { return (lon + 180) / 360 * size(z) }
      function toY(lat, z) {
        var r = lat * Math.PI / 180
        return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * size(z)
      }
      function toLon(x, z) { return x / size(z) * 360 - 180 }
      function latOf(y, z) {
        var n = Math.PI - 2 * Math.PI * y / size(z)
        return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)))
      }

      function visibleTiles() {
        var x0 = Math.floor(origin.left / TILE), x1 = Math.floor((origin.left + width) / TILE)
        var y0 = Math.floor(origin.top / TILE), y1 = Math.floor((origin.top + height) / TILE)
        var n = Math.pow(2, view.z), out = []
        for (var x = x0; x <= x1; x++) {
          for (var y = y0; y <= y1; y++) {
            if (x >= 0 && y >= 0 && x < n && y < n) out.push({ z: view.z, x: x, y: y })
          }
        }
        return out
      }

      function measure() {
        if (!mapEl) return
        width = mapEl.clientWidth
        height = mapEl.clientHeight
      }

      function originOf() {
        origin = { left: toX(view.lon, view.z) - width / 2, top: toY(view.lat, view.z) - height / 2 }
      }

      function toScreen(lat, lon) {
        return { x: toX(lon, view.z) - origin.left, y: toY(lat, view.z) - origin.top }
      }

      // ---- tiles -------------------------------------------------------------------------------
      function tileSrc(L, t) {
        var base = TILE_DIR + (L.id === 'sat' ? '' : L.id + '/')
        return base + t.z + '/' + t.x + '/' + t.y + '.' + L.ext
      }

      function askTiles() {
        fit()
        if (!ui.visible) return
        measure()
        if (!width) return
        view.z = tileZ()
        originOf()
        var layers = LAYERS[layer], list = [], tiles = visibleTiles()
        for (var i = 0; i < tiles.length; i++) {
          for (var j = 0; j < layers.length; j++) {
            var key = layers[j].id + '/' + tiles[i].z + '/' + tiles[i].x + '/' + tiles[i].y
            if (!ready[key]) list.push([tiles[i].z, tiles[i].x, tiles[i].y, layers[j].id])
          }
        }
        if (!list.length) return
        lua("beamngEarth_settings and beamngEarth_settings.mapTiles and beamngEarth_settings.mapTiles(jsonDecode('" +
          JSON.stringify(list) + "')) or {}", function (keys) {
          if (!keys || !keys.length) return
          for (var k = 0; k < keys.length; k++) ready[keys[k]] = true
          scope.$applyAsync(render)
        })
      }

      function tileList() {
        var out = [], layers = LAYERS[layer], tiles = visibleTiles()
        for (var i = 0; i < tiles.length; i++) {
          for (var j = 0; j < layers.length; j++) {
            var L = layers[j]
            var key = L.id + '/' + tiles[i].z + '/' + tiles[i].x + '/' + tiles[i].y
            if (!ready[key]) continue
            out.push({
              key: key,
              src: tileSrc(L, tiles[i]),
              left: Math.round(tiles[i].x * TILE - origin.left),
              top: Math.round(tiles[i].y * TILE - origin.top)
            })
          }
        }
        return out
      }

      // ---- the route, the pin and the car ------------------------------------------------------
      // The route is drawn from where the car is *along* it, so the part already driven is eaten away; turning round
      // moves that point back and the line grows again. The point is projected onto the segment (not just the nearest
      // vertex) and eased there, so the head slides instead of hopping from vertex to vertex.
      var routePos = 0, routeAt = 0, routeRefind = true
      var MATCH_AHEAD = 90, MATCH_TIE_DEG = 0.00018      // ~360 m of route; ~20 m of tolerance
      var EASE = 0.45, SNAP = 20                          // per render; a jump this big is a re-route: no easing

      function segProject(a, b, pl, k) {
        var ax = (a[1] - pl.lon) * k, ay = a[0] - pl.lat
        var bx = (b[1] - pl.lon) * k, by = b[0] - pl.lat
        var dx = bx - ax, dy = by - ay
        var L = dx * dx + dy * dy
        var t = L > 0 ? -(ax * dx + ay * dy) / L : 0
        t = t < 0 ? 0 : (t > 1 ? 1 : t)
        var px = ax + dx * t, py = ay + dy * t
        return { t: t, d: px * px + py * py }
      }

      // the car's position as a float index along the polyline (12.4 = segment 12, 40% of the way along it)
      function routeMatch(pl) {
        var n = routePoints.length
        var k = Math.cos(pl.lat * Math.PI / 180)
        var best = 0, bestD = Infinity
        for (var i = 0; i < n; i++) {
          var p = routePoints[i]
          var dx = (p[1] - pl.lon) * k, dy = p[0] - pl.lat
          var d = dx * dx + dy * dy
          if (d < bestD) { bestD = d; best = i }
        }
        if (!routeRefind && best > routeAt + MATCH_AHEAD) {
          var q = routePoints[routeAt]
          var qx = (q[1] - pl.lon) * k, qy = q[0] - pl.lat
          if (Math.sqrt(qx * qx + qy * qy) <= Math.sqrt(bestD) + MATCH_TIE_DEG) best = routeAt
        }
        routeRefind = false
        routeAt = best
        var f = best
        if (best > 0) {                       // inside the segment before, or the one after
          var back = segProject(routePoints[best - 1], routePoints[best], pl, k)
          f = best - 1 + back.t
          if (back.d > 0 && best < n - 1) {
            var fwd = segProject(routePoints[best], routePoints[best + 1], pl, k)
            if (fwd.d < back.d) f = best + fwd.t
          }
        } else if (n > 1) {
          f = segProject(routePoints[0], routePoints[1], pl, k).t
        }
        return f
      }

      function easing(target) {
        var d = target - routePos
        routePos = (Math.abs(d) > SNAP) ? target : routePos + d * EASE
        if (routePos < 0) routePos = 0
        if (routePos > routePoints.length - 1) routePos = routePoints.length - 1
        return routePos
      }

      function drawRoute() {
        if (!routePoints || routePoints.length < 2) return
        var n = routePoints.length
        var pos = ui.player ? easing(routeMatch(ui.player)) : routePos
        var i = Math.floor(pos), f = pos - i
        if (i >= n - 1) return                 // arrived: nothing left to draw
        var head = {
          lat: routePoints[i][0] + (routePoints[i + 1][0] - routePoints[i][0]) * f,
          lon: routePoints[i][1] + (routePoints[i + 1][1] - routePoints[i][1]) * f
        }
        ctx.lineJoin = 'round'
        ctx.lineCap = 'round'
        ctx.beginPath()
        var h = toScreen(head.lat, head.lon)
        ctx.moveTo(h.x, h.y)
        for (var j = i + 1; j < n; j++) {
          var p = toScreen(routePoints[j][0], routePoints[j][1])
          ctx.lineTo(p.x, p.y)
        }
        ctx.strokeStyle = 'rgba(0,0,0,0.30)'      // a dark outline: the route reads on white and on imagery
        ctx.lineWidth = 11
        ctx.stroke()
        ctx.strokeStyle = 'rgba(255,255,255,0.96)'
        ctx.lineWidth = 8
        ctx.stroke()
        ctx.strokeStyle = ROUTE_BLUE
        ctx.lineWidth = 5.5
        ctx.stroke()
      }

      function drawPin() {
        if (!routePoints || !routePoints.length) return
        var p = toScreen(routePoints[routePoints.length - 1][0], routePoints[routePoints.length - 1][1])
        if (p.x < -30 || p.y < -30 || p.x > width + 30 || p.y > height + 30) return
        ctx.beginPath()
        ctx.moveTo(p.x, p.y)
        ctx.lineTo(p.x - 5, p.y - 11)
        ctx.lineTo(p.x + 5, p.y - 11)
        ctx.closePath()
        ctx.fillStyle = '#ff3b30'
        ctx.fill()
        ctx.beginPath()
        ctx.arc(p.x, p.y - 16, 8.5, 0, Math.PI * 2)
        ctx.fillStyle = '#ff3b30'
        ctx.fill()
        ctx.lineWidth = 3
        ctx.strokeStyle = '#fff'
        ctx.stroke()
      }

      // a double tap's pin: a dark teardrop with a white ring, so it reads on the light map and the imagery alike
      function drawDroppedPin() {
        var d = ui.pin
        if (!d || ui.route) return
        var p = toScreen(d.lat, d.lon)
        if (p.x < -30 || p.y < -30 || p.x > width + 30 || p.y > height + 30) return
        ctx.save()
        ctx.shadowColor = 'rgba(0,0,0,0.45)'
        ctx.shadowBlur = 6
        ctx.shadowOffsetY = 2
        ctx.beginPath()
        ctx.moveTo(p.x, p.y)
        ctx.lineTo(p.x - 6, p.y - 13)
        ctx.lineTo(p.x + 6, p.y - 13)
        ctx.closePath()
        ctx.fillStyle = '#2c2c2e'
        ctx.fill()
        ctx.beginPath()
        ctx.arc(p.x, p.y - 19, 9.5, 0, Math.PI * 2)
        ctx.fillStyle = '#2c2c2e'
        ctx.fill()
        ctx.restore()
        ctx.beginPath()
        ctx.arc(p.x, p.y - 19, 9.5, 0, Math.PI * 2)
        ctx.lineWidth = 2.5
        ctx.strokeStyle = '#fff'
        ctx.stroke()
        ctx.beginPath()
        ctx.arc(p.x, p.y - 19, 3.4, 0, Math.PI * 2)
        ctx.fillStyle = '#fff'
        ctx.fill()
      }

      function drawPlayer() {
        var pl = ui.player
        if (!pl) return
        var p = toScreen(pl.lat, pl.lon)
        if (pl.heading !== null && pl.heading !== undefined) {
          var a = pl.heading * Math.PI / 180
          ctx.beginPath()
          ctx.moveTo(p.x, p.y)
          ctx.arc(p.x, p.y, 26, a - Math.PI / 2 - 0.55, a - Math.PI / 2 + 0.55)
          ctx.closePath()
          ctx.fillStyle = 'rgba(10,132,255,0.18)'
          ctx.fill()
        }
        // a dark ring keeps the puck readable on pale streets, snow and bright terrain
        ctx.beginPath()
        ctx.arc(p.x, p.y, 14, 0, Math.PI * 2)
        ctx.fillStyle = 'rgba(0,0,0,0.85)'
        ctx.fill()
        ctx.beginPath()
        ctx.arc(p.x, p.y, 11.5, 0, Math.PI * 2)
        ctx.fillStyle = '#fff'
        ctx.fill()
        ctx.beginPath()
        ctx.arc(p.x, p.y, 7.5, 0, Math.PI * 2)
        ctx.fillStyle = ROUTE_BLUE
        ctx.fill()
        ctx.lineWidth = 1.5
        ctx.strokeStyle = 'rgba(0,0,0,0.55)'
        ctx.stroke()
      }

      function draw() {
        if (!canvas || !width || !height) return
        var dpr = window.devicePixelRatio || 1
        var w = Math.round(width * dpr), h = Math.round(height * dpr)
        if (canvas.width !== w || canvas.height !== h) {
          canvas.width = w
          canvas.height = h
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
        ctx.clearRect(0, 0, width, height)
        drawRoute()
        drawPin()
        drawDroppedPin()
        drawPlayer()
      }

      function render() {
        fit()
        if (!ui.visible) return
        measure()
        if (!width) return
        view.z = tileZ()
        originOf()
        if (zoomEl) zoomEl.style.transform = 'scale(' + zoomScale().toFixed(4) + ')'
        scope.tiles = tileList()
        draw()
      }

      // the slide-up on Page Up: applied after the digest has un-hidden the phone, so the animation runs from its
      // first frame (an animation on a display:none element would not)
      function enterAnimation() {
        if (!phoneEl) return
        phoneEl.classList.remove('is-in')
        void phoneEl.offsetWidth
        phoneEl.classList.add('is-in')
        askTiles()
      }

      function fetchRoute(version) {
        routeVersion = version
        lua('beamngEarth_earthPhone and beamngEarth_earthPhone.route() or nil', function (r) {
          routePoints = (r && r.points && r.points.length) ? r.points : null
          routeRefind = true                  // a new route (a re-route): find the car on it again
          routeAt = 0
          routePos = 0
          scope.$applyAsync(render)
        })
      }

      // ---- formatting --------------------------------------------------------------------------
      function distance(m, imperial, small) {
        if (m === null || m === undefined) return ''
        if (imperial) {
          var mi = m / 1609.344
          if (mi < 0.19 && small) return Math.max(50, Math.round(m / 0.3048 / 50) * 50) + ' ft'
          return (mi < 10 ? mi.toFixed(1) : Math.round(mi)) + ' mi'
        }
        if (m < 300 && small) return Math.max(50, Math.round(m / 50) * 50) + ' m'
        return (m < 1000 ? Math.round(m / 10) * 10 + ' m' : (m / 1000).toFixed(m < 10000 ? 1 : 0) + ' km')
      }

      function eta(seconds) {
        if (!seconds || seconds < 0) return { value: '--', unit: '' }
        var min = Math.round(seconds / 60)
        if (min < 60) return { value: String(Math.max(1, min)), unit: 'min' }
        return { value: String(Math.floor(min / 60)), unit: 'hr ' + (min % 60) }
      }

      function glyphOf(step) {
        if (!step) return { show: false, paths: [], style: {} }
        var g = GLYPHS[step.icon] || GLYPHS.straight
        var t = []
        if (g.flip) t.push('scaleX(-1)')
        if (g.rot) t.push('rotate(' + g.rot + 'deg)')
        return { show: true, paths: g.paths, style: { transform: t.join(' '), transformOrigin: '12px 21px' } }
      }

      // the navigator's sentence for the card: "Turn right onto Main Street", "Now, turn left onto Oak Avenue"
      function caption(step, imperial) {
        if (!step || !step.sayNow) return ''
        if (step.dist !== null && step.dist !== undefined && step.dist <= NOW_M) return 'Now, ' + step.say
        return step.sayNow
      }

      // ---- state -------------------------------------------------------------------------------
      var hadRoute = false
      function apply(s) {
        if (!s) return
        var wasVisible = ui.visible
        ui.visible = !!s.visible
        ui.connected = !!s.connected
        ui.player = s.player || null
        ui.limit = s.limit || null
        ui.search = s.search || null
        ui.route = s.route || null
        ui.pin = s.pin || null
        var imperial = !!(s.limit && s.limit.unit === 'mph')
        var step = s.route && s.route.step
        scope.remaining = s.route ? distance(s.route.remaining, imperial) : ''
        scope.eta = eta(s.route && s.route.eta)
        scope.distance = step ? distance(step.dist, imperial, true) : ''
        scope.glyph = glyphOf(step)
        scope.caption = caption(step, imperial)
        scope.captionNow = !!(step && step.dist !== null && step.dist !== undefined && step.dist <= NOW_M)
        scope.thenSay = (step && step.thenSay && !(s.route && s.route.rerouting)) ? 'then ' + step.thenSay : ''
        scope.rerouting = !!(s.route && s.route.rerouting)
        scope.pinCard = (ui.pin && !s.route)
          ? { name: ui.pin.name || 'Dropped Pin', detail: ui.pin.detail || '' }
          : (scope.pinBusy && !s.route ? scope.pinCard : null)
        if (s.route) {
          scope.pinBusy = false
          if (busyTimer) { clearTimeout(busyTimer); busyTimer = null }
        }
        if (s.route && !hadRoute) {           // a destination was set: the search drawer gives way to the route
          scope.searchOpen = false
          scope.layersOpen = false
        }
        hadRoute = !!s.route
        if (s.player && follow) {
          view.lat = s.player.lat
          view.lon = s.player.lon
        }
        if (!s.route) {
          routePoints = null
          routeVersion = -1
          routeAt = 0
          routeRefind = true
        } else if (s.route.version !== routeVersion) {
          fetchRoute(s.route.version)
        }
        if (ui.visible && !wasVisible) setTimeout(enterAnimation, 0)
        render()
      }

      // ---- input -------------------------------------------------------------------------------
      var dragging = null, tap = null
      var TAP_MS = 350, TAP_PX = 12, TAP_MOVE = 5        // a double tap, detected here (the game's browser
                                                        // does not always raise dblclick inside an app)
      // a screen point (client px) as lat, lon: undo the frame's scale and the continuous zoom's
      function pointOf(ev) {
        var box = mapEl.getBoundingClientRect()
        var k = zoomScale()
        var px = (ev.clientX - box.left) / scale, py = (ev.clientY - box.top) / scale
        var ux = width / 2 + (px - width / 2) / k, uy = height / 2 + (py - height / 2) / k
        return { lat: latOf(uy + origin.top, view.z), lon: toLon(ux + origin.left, view.z) }
      }
      function dropPinAt(ev) {
        if (!ui.visible || !width || !mapEl) return
        var p = pointOf(ev)
        if (!isFinite(p.lat) || !isFinite(p.lon)) return
        scope.layersOpen = false
        call('setPin', p.lat, p.lon)
      }
      function onTap(ev) {
        var now = Date.now()
        if (tap && now - tap.t < TAP_MS && Math.abs(ev.clientX - tap.x) < TAP_PX
            && Math.abs(ev.clientY - tap.y) < TAP_PX) {
          tap = null
          dropPinAt(ev)
          return
        }
        tap = { t: now, x: ev.clientX, y: ev.clientY }
      }
      function onMove(ev) {
        if (!dragging) return
        var z = view.z, k = zoomScale()
        if (Math.abs(ev.clientX - dragging.x) > TAP_MOVE || Math.abs(ev.clientY - dragging.y) > TAP_MOVE) {
          dragging.moved = true
        }
        var cx = toX(dragging.lon, z) - (ev.clientX - dragging.x) / (scale * k)
        var cy = toY(dragging.lat, z) - (ev.clientY - dragging.y) / (scale * k)
        view.lon = toLon(cx, z)
        view.lat = latOf(cy, z)
        scope.$applyAsync(render)
      }
      function onUp(ev) {
        var was = dragging
        dragging = null
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
        if (was && !was.moved && ev && ev.target && mapEl && mapEl.contains(ev.target)) onTap(ev)
      }
      scope.dragStart = function (ev) {
        if (ev.button !== 0) return
        follow = false
        dragging = { x: ev.clientX, y: ev.clientY, lat: view.lat, lon: view.lon, moved: false }
        document.addEventListener('mousemove', onMove)
        document.addEventListener('mouseup', onUp)
      }
      function zoomBy(d) {
        var v = Math.max(Z_MIN, Math.min(Z_MAX, view.zoom + d))
        if (v === view.zoom) return
        var before = tileZ()
        view.zoom = v
        scope.$applyAsync(function () {
          render()
          if (tileZ() !== before) askTiles()      // a new level: start its tiles now, not on the next tick
        })
      }
      scope.directions = function () {
        var d = ui.pin
        if (!d || scope.pinBusy) return
        remember({ name: d.name || 'Dropped Pin', lat: d.lat, lon: d.lon, detail: d.detail })
        scope.searchOpen = false
        scope.layersOpen = false
        follow = true
        scope.pinBusy = true                   // the card holds on with "Planning…" until the route is in
        if (busyTimer) clearTimeout(busyTimer)
        busyTimer = setTimeout(function () {
          scope.$applyAsync(function () {
            scope.pinBusy = false
            if (!ui.route) scope.pinCard = null
          })
        }, BUSY_MS)
        call('navigate', d.lat, d.lon, d.name || 'Dropped Pin')
      }
      scope.closePin = function () {
        scope.pinCard = null
        call('clearPin')
      }
      function onWheel(ev) {
        if (!ui.visible) return
        ev.preventDefault()
        var d = ev.deltaY * (ev.deltaMode === 1 ? 16 : 1)      // lines -> px
        zoomBy(-d * WHEEL_SENS * (ev.ctrlKey ? 4 : 1))
      }
      function onResize() { scope.$applyAsync(fit) }

      scope.recenter = function () {
        follow = true
        if (ui.player) {
          view.lat = ui.player.lat
          view.lon = ui.player.lon
        }
        render()
      }
      scope.toggleSearch = function () {
        scope.searchOpen = !scope.searchOpen
        if (scope.searchOpen) setTimeout(function () {
          var box = hostEl.querySelector('.bp-input')
          if (box) box.focus()
        }, 30)
        else typing(false)
        render()
      }
      scope.toggleLayers = function () { scope.layersOpen = !scope.layersOpen }
      scope.setLayer = function (id) {
        scope.layersOpen = false
        if (!LAYERS[id] || id === layer) return
        layer = scope.layer = id
        scope.layerName = LAYER_NAMES[id]
        saveLayer(id)
        ready = {}                       // another style, another set of tile files
        render()
        askTiles()
      }

      // ---- the search field: a native listener, so a keystroke is never lost to a digest ----------------
      function typing(on) {
        lua('if setCEFTyping then setCEFTyping(' + (on ? 'true' : 'false') + ') end')
      }
      function suggest(q) {
        // while the server thinks, the places this player already used answer at once (4 shown, the rest scroll)
        if (!q) { scope.suggestions = recents.slice(0, RECENT_MAX); return }
        var low = q.toLowerCase()
        scope.suggestions = recents.filter(function (r) {
          return (r.name || '').toLowerCase().indexOf(low) >= 0
        }).slice(0, RECENT_MAX)
      }
      function onInput(ev) {
        var q = ev.target.value || ''
        scope.q = q
        suggest(q.trim())
        parseCoord(q.trim())
        scope.$applyAsync()
        if (typeTimer) clearTimeout(typeTimer)
        if (q.trim().length < MIN_CHARS) return
        typeTimer = setTimeout(function () { call('search', q.trim()) }, TYPE_MS)
      }
      // a typed latitude, longitude: places.lua parses every format the big map accepts, so the phone offers the
      // same thing the big map's search does (and offers it first, before the network answers)
      function parseCoord(q) {
        if (!q || !LATLON_RE.test(q)) {
          if (scope.coord) scope.$applyAsync(function () { scope.coord = null })
          return
        }
        lua('beamngEarth_settings and beamngEarth_settings.uiMapParse(' + ser(q) + ') or nil', function (r) {
          if (!r || typeof r.lat !== 'number') return
          scope.$applyAsync(function () {
            scope.coord = {
              name: Number(r.lat).toFixed(5) + ', ' + Number(r.lon).toFixed(5),
              detail: 'Coordinates', lat: r.lat, lon: r.lon
            }
          })
        })
      }
      function onKey(ev) {
        if (ev.key === 'Enter' || ev.keyCode === 13) {
          ev.preventDefault()
          if (typeTimer) clearTimeout(typeTimer)
          var box = hostEl.querySelector('.bp-input')
          var q = ((box && box.value) || '').trim()
          if (q.length >= MIN_CHARS) call('search', q)
          else if (scope.suggestions.length) scope.$applyAsync(function () { pick(scope.suggestions[0]) })
        } else if (ev.key === 'Escape' || ev.keyCode === 27) {
          scope.$applyAsync(function () { scope.searchOpen = false })
          typing(false)
        }
      }
      function pick(r) {
        scope.q = ''
        scope.suggestions = []
        scope.coord = null
        scope.searchOpen = false
        follow = true
        remember(r)
        typing(false)
        call('navigate', r.lat, r.lon, r.name)
      }
      scope.pick = function (r) { pick(r) }
      scope.useOption = function (i) {
        if (!ui.route || !ui.route.options) return
        for (var k = 0; k < ui.route.options.length; k++) ui.route.options[k].chosen = (k === i)
        call('pickOption', i + 1)
        render()
      }
      // a route chip: the name and how long it takes ("18.4 km · 24 min" is the hover title)
      scope.optionMeta = function (o) {
        var e = eta(o && o.dur)
        return e.value + ' ' + (e.unit || 'min')
      }
      scope.optionTitle = function (o) {
        var imperial = !!(ui.limit && ui.limit.unit === 'mph')
        return distance(o && o.dist, imperial) + ' · ' + scope.optionMeta(o)
      }
      scope.selectLayer = function (o) { scope.setLayer(o.id) }
      scope.endRoute = function () {
        scope.searchOpen = true
        call('endRoute')
      }

      // ---- lifecycle ---------------------------------------------------------------------------
      hostEl = element[0]
      fitEl = hostEl.querySelector('.bp-fit')
      phoneEl = hostEl.querySelector('.bp-phone')
      mapEl = hostEl.querySelector('.bp-map')
      canvas = hostEl.querySelector('.bp-overlay')
      zoomEl = hostEl.querySelector('.bp-zoomable')
      if (canvas) ctx = canvas.getContext('2d')
      if (mapEl) {
        mapEl.addEventListener('wheel', onWheel, { passive: false })
        mapEl.addEventListener('error', function (ev) {          // a tile file that is not there: no broken image
          if (ev.target && ev.target.className === 'bp-tile') ev.target.style.visibility = 'hidden'
        }, true)
      }
      // the search field is inside ng-if, so its events are taken where it lives (and it is never re-bound twice)
      function isInput(el) { return el && el.classList && el.classList.contains('bp-input') }
      function onFocusIn(ev) {
        if (!isInput(ev.target)) return
        typing(true)
        scope.$applyAsync(function () { suggest((scope.q || '').trim()) })
      }
      function onFocusOut(ev) { if (isInput(ev.target)) typing(false) }
      function onDelegatedInput(ev) { if (isInput(ev.target)) onInput(ev) }
      function onDelegatedKey(ev) { if (isInput(ev.target)) onKey(ev) }
      hostEl.addEventListener('focusin', onFocusIn, true)
      hostEl.addEventListener('focusout', onFocusOut, true)
      hostEl.addEventListener('input', onDelegatedInput, true)
      hostEl.addEventListener('keydown', onDelegatedKey, true)
      window.addEventListener('resize', onResize)

      var ask = setInterval(askTiles, ASK_MS)
      var off = scope.$on('BeamNGEarthMaps', function (ev, data) { apply(data) })

      function announce() {
        // (inside the phone's Maps app: on screen while mounted, so shown, and the hook streams while it is)
        lua('if beamngEarth_earthPhone then beamngEarth_earthPhone.mounted(true) if not beamngEarth_earthPhone.visible() then beamngEarth_earthPhone.setVisible(true) end end')
      }
      announce()
      var again = setInterval(announce, 3000)          // after a Lua reload the mod forgets the app
      lua('beamngEarth_earthPhone and beamngEarth_earthPhone.state() or nil', function (s) {
        if (s) scope.$applyAsync(function () { apply(s) })
      })

      setTimeout(function () { fit(); render(); askTiles() }, 60)

      scope.$on('$destroy', function () {
        off()
        clearInterval(ask)
        clearInterval(again)
        if (typeTimer) clearTimeout(typeTimer)
        if (busyTimer) clearTimeout(busyTimer)
        onUp()
        window.removeEventListener('resize', onResize)
        if (mapEl) mapEl.removeEventListener('wheel', onWheel)
        hostEl.removeEventListener('focusin', onFocusIn, true)
        hostEl.removeEventListener('focusout', onFocusOut, true)
        hostEl.removeEventListener('input', onDelegatedInput, true)
        hostEl.removeEventListener('keydown', onDelegatedKey, true)
        typing(false)
        lua('if beamngEarth_earthPhone then beamngEarth_earthPhone.setVisible(false) beamngEarth_earthPhone.mounted(false) end')
      })
    }
  }
}])
