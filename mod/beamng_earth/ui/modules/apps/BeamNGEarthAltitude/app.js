// BeamNG.Earth altitude as a UI app: the car's height above sea level (the world's own heights: the terrain is built
// from the elevation data's sea-level heights), above the ground under it, and its climb rate. Asks the game 4 times a
// second (one small Lua call); in the game's units (Options > Display, uiUnitLength): feet and ft/min (miles once more
// than 10 miles up), or metres and m/s (km once more than 10 km up).
angular.module('beamng.apps')
.directive('beamngEarthAltitude', [function () {
  // (an expression: with a callback the game evaluates it as an argument, BeamNGAPI.engineLua)
  var LUA = '(function() ' +
    'local c = rawget(_G, "beamngEarth_core") local v = getPlayerVehicle(0) ' +
    'if not (c and c._state and v) then return nil end ' +
    'local a = c._state().anchor if not a then return nil end ' +
    'local p = v:getPosition() local _, _, h = a:worldToGeodetic(p.x, p.y, p.z) ' +
    'local g = be.getSurfaceHeightBelow and be:getSurfaceHeightBelow(vec3(p.x, p.y, p.z + 1)) ' +
    'local vel = v:getVelocity() ' +
    'local st = rawget(_G, "settings") local u = st and st.getValue and st.getValue("uiUnitLength") ' +
    'return { h = h, agl = (g and g > -1e5) and (p.z - g) or nil, vz = vel.z, metric = (u == "metric") } end)()'
  var FT = 3.28084
  function num(x, d) {
    return Number(x).toLocaleString(undefined, { maximumFractionDigits: d || 0, minimumFractionDigits: d || 0 })
  }
  function height(m, metric) {
    if (m === null || m === undefined || !isFinite(m)) return '-'
    if (metric) {
      if (Math.abs(m) >= 10000) return num(m / 1000, 1) + ' km'
      return num(m) + ' m'
    }
    var ft = m * FT
    if (Math.abs(ft) >= 52800) return num(ft / 5280, 1) + ' mi'
    return num(ft) + ' ft'
  }
  function climb(vz, metric) {
    if (metric) return Math.abs(vz) < 0.25 ? 'level' : (vz > 0 ? '▲ ' : '▼ ') + num(Math.abs(vz), 1) + ' m/s'
    var fpm = vz * FT * 60
    return Math.abs(fpm) < 50 ? 'level' : (fpm > 0 ? '▲ ' : '▼ ') + num(Math.abs(fpm)) + ' ft/min'
  }
  return {
    template:
      '<div class="bngApp" style="width:100%;height:100%;box-sizing:border-box;padding:6px 10px;' +
        'background:rgba(10,12,16,0.55);color:#eef1f4;border-radius:4px;font-family:inherit;line-height:1.25;">' +
        '<div style="font-size:11px;opacity:.7;letter-spacing:.06em;">ALTITUDE</div>' +
        '<div style="font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;">{{ alt }}</div>' +
        '<div style="font-size:12px;opacity:.85;font-variant-numeric:tabular-nums;">' +
          '<span>{{ agl }} above ground</span><br><span>{{ vs }}</span></div>' +
      '</div>',
    replace: true,
    link: function (scope) {
      'use strict'
      scope.alt = '-'
      scope.agl = '-'
      scope.vs = ''
      var busy = false
      var poll = function () {
        if (busy) return
        busy = true
        bngApi.engineLua(LUA, function (r) {
          busy = false
          scope.$evalAsync(function () {
            if (!r || typeof r.h !== 'number') { scope.alt = '-'; scope.agl = '-'; scope.vs = ''; return }
            scope.alt = height(r.h, r.metric)
            scope.agl = (typeof r.agl === 'number') ? height(r.agl, r.metric) : '-'
            scope.vs = climb(r.vz || 0, r.metric)
          })
        })
      }
      var timer = setInterval(poll, 250)
      poll()
      scope.$on('$destroy', function () { clearInterval(timer) })
    }
  }
}]);
