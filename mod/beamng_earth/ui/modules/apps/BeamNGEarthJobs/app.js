// BeamNG.Earth Jobs (jobs.lua through settings.uiJobs*): the delivery offers near you, the job you are on, the last
// payout. Polled once a second.
angular.module('beamng.apps')
.directive('beamngEarthJobs', [function () {
  var S = 'beamngEarth_settings'
  var css =
    '.bnej{width:100%;height:100%;box-sizing:border-box;padding:12px;background:rgba(12,14,18,0.86);border-radius:12px;' +
    'color:#eef1f5;font-family:"Inter Tight","Segoe UI",sans-serif;font-size:13px;display:flex;flex-direction:column;gap:8px;overflow:hidden}' +
    '.bnej h1{margin:0;font-size:15px;font-weight:800;letter-spacing:.08em;display:flex;justify-content:space-between;align-items:center}' +
    '.bnej button{background:#2b6cff;color:#fff;border:0;border-radius:7px;padding:5px 10px;font-weight:700;cursor:pointer;font-size:12px}' +
    '.bnej button.ghost{background:rgba(255,255,255,0.12)}.bnej button.stop{background:#c0392b}' +
    '.bnej .list{overflow-y:auto;display:flex;flex-direction:column;gap:6px;min-height:0;flex:1}' +
    '.bnej .card{background:rgba(255,255,255,0.07);border-radius:9px;padding:8px 10px;display:flex;flex-direction:column;gap:3px}' +
    '.bnej .row{display:flex;justify-content:space-between;align-items:center;gap:8px}' +
    '.bnej .pay{color:#7ee08a;font-weight:800;font-size:15px}.bnej .dim{color:#9aa3ad;font-size:12px}' +
    '.bnej .car{font-weight:700}.bnej .bad{color:#ff6b6b}.bnej .big{font-size:22px;font-weight:800}'
  return {
    template:
      '<div class="bngApp bnej"><style>' + css + '</style>' +
      '<h1><span>JOBS</span><button class="ghost" ng-hide="s.active" ng-click="refresh()">{{ s.searching ? "Searching..." : "Find jobs" }}</button></h1>' +
      '<div ng-if="s.active" class="card">' +
        '<div class="dim">{{ s.active.stage == "pickup" ? "Drive to the pickup and stop there" : "Deliver the car and stop at the garage" }}</div>' +
        '<div class="car">{{ s.active.car }}</div>' +
        '<div class="row"><span class="dim">From</span><span>{{ s.active.pickup }}</span></div>' +
        '<div class="row"><span class="dim">To</span><span>{{ s.active.dropoff }}</span></div>' +
        '<div class="row" ng-if="s.active.stage == \'deliver\'"><span class="big" ng-class="{bad: s.active.t > s.active.limit}">{{ clock(s.active.t) }}</span>' +
          '<span class="dim">limit {{ clock(s.active.limit) }}</span></div>' +
        '<div class="row" ng-if="s.active.estimate"><span class="dim">Pay now</span><span class="pay">${{ s.active.estimate.total.toFixed(2) }}</span></div>' +
        '<div class="row" ng-if="s.active.estimate"><span class="dim">Early bonus / damage / late</span>' +
          '<span>+{{ s.active.estimate.bonus.toFixed(0) }} / <span class="bad">-{{ s.active.estimate.damage.toFixed(0) }}</span> / <span class="bad">-{{ s.active.estimate.late.toFixed(0) }}</span></span></div>' +
        '<div class="row"><span class="dim">Tolls and fuel paid back</span><span>${{ s.active.expenses.toFixed(2) }}</span></div>' +
        '<div class="row"><span></span><button class="stop" ng-click="cancel()">Cancel job</button></div>' +
      '</div>' +
      '<div ng-if="s.last && !s.active" class="card">' +
        '<div ng-if="s.last.cancelled" class="dim">Job cancelled: {{ s.last.job.car_name }}</div>' +
        '<div ng-if="s.last.pay" class="row"><span>Delivered the {{ s.last.job.car_name }} in {{ clock(s.last.t) }}</span><span class="pay">+${{ s.last.pay.total.toFixed(2) }}</span></div>' +
        '<div ng-if="s.last.pay" class="dim">pay ${{ s.last.pay.base.toFixed(0) }}, bonus +{{ s.last.pay.bonus.toFixed(0) }}, damage -{{ s.last.pay.damage.toFixed(0) }}, late -{{ s.last.pay.late.toFixed(0) }}, expenses +{{ s.last.pay.expenses.toFixed(2) }}</div>' +
      '</div>' +
      '<div class="list" ng-hide="s.active">' +
        '<div class="dim" ng-if="s.error">{{ s.error }}</div>' +
        '<div class="dim" ng-if="!s.error && !s.searching && !(s.offers && s.offers.length)">Press Find jobs for car deliveries from garages near you.</div>' +
        '<div class="card" ng-repeat="j in s.offers">' +
          '<div class="row"><span class="car">{{ j.car_name }}</span><span class="pay">${{ j.pay.toFixed(0) }}</span></div>' +
          '<div class="dim">{{ j.pickup.name }} → {{ j.dropoff.name }}</div>' +
          '<div class="row"><span class="dim">{{ (j.dist / 1609.344).toFixed(1) }} mi · {{ clock(j.limit) }} limit</span>' +
          '<button ng-click="take($index)">Take job</button></div>' +
        '</div>' +
      '</div>' +
      '</div>',
    replace: true,
    link: function (scope) {
      'use strict'
      scope.s = {}
      function lua(cmd, cb) { if (window.bngApi) bngApi.engineLua(cmd, cb || function () {}) }
      scope.clock = function (t) {
        t = Math.max(0, Math.round(t || 0))
        var m = Math.floor(t / 60), s = t % 60
        return m + ':' + (s < 10 ? '0' : '') + s
      }
      function poll() {
        lua(S + ' and ' + S + '.uiJobs and ' + S + '.uiJobs() or nil', function (s) {
          scope.$evalAsync(function () { scope.s = s || {} })
        })
      }
      scope.refresh = function () { lua(S + '.uiJobsRefresh()', poll) }
      scope.take = function (i) { lua(S + '.uiJobAccept(' + (i + 1) + ')', poll) }
      scope.cancel = function () { lua(S + '.uiJobCancel()', poll) }
      var timer = setInterval(poll, 1000)
      poll()
      scope.$on('$destroy', function () { clearInterval(timer) })
    }
  }
}]);
