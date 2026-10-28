// BeamNG.Earth wallet (wallet.lua): the balance, red below zero, polled from settings.uiWallet.
angular.module('beamng.apps')
.directive('beamngEarthWallet', [function () {
  return {
    template:
      '<div class="bngApp" style="width:100%;height:100%;background:transparent;box-shadow:none;display:flex;justify-content:flex-end;align-items:flex-start;">' +
        '<span ng-show="text" style="background:rgba(12,14,18,0.82);border-radius:10px;padding:6px 14px;font-size:20px;font-weight:800;' +
        'font-family:\'Inter Tight\',\'Segoe UI\',sans-serif;" ng-style="{color: neg ? \'#ff6b6b\' : \'#7ee08a\'}">{{ text }}</span>' +
        '<span ng-show="heat >= 1" style="margin-left:6px;background:rgba(12,14,18,0.82);border-radius:10px;padding:6px 10px;font-size:14px;font-weight:800;' +
        'font-family:\'Inter Tight\',\'Segoe UI\',sans-serif;" ng-style="{color: heat >= 4 ? \'#ff4d4d\' : (heat >= 2 ? \'#ffb84d\' : \'#c9ced6\')}">{{ stars }}</span>' +
      '</div>',
    replace: true,
    link: function (scope) {
      'use strict'
      scope.text = ''
      scope.neg = false
      function poll() {
        if (!window.bngApi) return
        bngApi.engineLua('beamngEarth_settings and beamngEarth_settings.uiWallet and beamngEarth_settings.uiWallet() or nil', function (w) {
          scope.$evalAsync(function () {
            scope.text = w ? w.text : ''
            scope.neg = !!(w && w.balance < 0)
            scope.heat = w && w.heat ? Math.round(w.heat) : 0
            scope.stars = scope.heat >= 1 ? new Array(scope.heat + 1).join('\u2605') : ''
          })
        })
      }
      var timer = setInterval(poll, 1000)
      poll()
      scope.$on('$destroy', function () { clearInterval(timer) })
    }
  }
}]);
