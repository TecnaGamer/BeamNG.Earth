// BeamNG.Earth status bar as a UI app (lua/ge/extensions/beamngEarth/hudapps.lua): street | place, the loading line under it.
// A line longer than the app wraps onto more lines, breaking only after a separator (" | ", " · "), and the box grows
// downward with it (the user, 2026-11-12: "the loading bar gets cut off when to much text is shown": a long loading
// line ended in "..." at the app's width, losing the parts after it).
angular.module('beamng.apps')
.directive('beamngEarthStatus', [function () {
  var ROW = 'display:inline-block;max-width:100%;box-sizing:border-box;background:rgba(10,12,16,0.62);color:#eef1f4;' +
    'border-radius:4px;padding:3px 12px;font-size:14px;line-height:1.3;text-align:center;white-space:normal;' +
    'overflow-wrap:anywhere;word-break:normal;'
  // the text with its parts kept whole: spaces inside a part made non-breaking, so a line breaks after a separator
  function breakable (text) {
    var s = String(text || '').replace(/\s+/g, ' ').trim()
    if (!s) return ''
    return s.split(/ ([|·]) /).map(function (part, i) {
      return i % 2 ? ' ' + part + ' ' : part.replace(/ /g, ' ')
    }).join('')
  }
  return {
    template:
      '<div class="bngApp" style="width:100%;height:100%;background:transparent;box-shadow:none;overflow:visible;" layout="column" layout-align="start center">' +
        '<span ng-show="text && !hidden" style="' + ROW + '">{{ text }}</span>' +
        '<span ng-show="loading && !hidden" style="margin-top:3px;' + ROW + '">{{ loading }}</span>' +
      '</div>',
    replace: true,
    link: function (scope, element) {
      'use strict'
      scope.text = ''
      scope.loading = ''
      scope.hidden = false
      // the game's app frame (.ui-app-host, the layout's 64 px) clips its content: a wrapped row hangs below it instead
      // (in-game 2026-11-12: the second line of a wrapped loading row was cut in half)
      var unclipped = false
      function unclip () {
        if (unclipped) return
        var el = element && element[0]
        var host = el && el.closest ? el.closest('.ui-app-host') : null
        if (host) { host.style.overflow = 'visible'; unclipped = true }
      }
      var hello = function (on) {
        bngApi.engineLua('if beamngEarth_core and beamngEarth_core.hudApp then beamngEarth_core.hudApp("status", ' + on + ') end')
      }
      scope.$on('BeamNGEarthHud', function (event, data) {
        scope.$evalAsync(function () {
          scope.text = breakable(data && data.status)
          scope.loading = breakable(data && data.loading)
          scope.hidden = !!(data && data.hidden)
          unclip()
        })
      })
      hello(true)
      var again = setInterval(function () { hello(true) }, 3000)   // (after a Lua reload the mod forgets the app)
      scope.$on('$destroy', function () { clearInterval(again); hello(false) })
    }
  }
}]);
