// BeamNG.Earth phone (the user, 2026-10-26: "Cell Phone ui app that has access to your bank, a in game weather app ...
// can play my spotify, show you work available"; "phone style should be like an iphone, it should also have a settings
// tab where you can change your background from my gallery, change the ui size for it, and change themes"). Slides up
// bottom-right on the phone key (beamngEarth_phone: Ctrl+O), down again on the same key. Apps: Bank (wallet.lua),
// Weather (dynweather.lua: the week), Jobs (jobs.lua), Music (server media.py: desktop Spotify's media keys), Settings
// (wallpaper from the screenshots and the wallpapers folder, size, theme, accent, the weather mode). Everything through
// beamngEarth_settings.uiPhone* (polled once a second while the phone is up).
angular.module('beamng.apps')
.directive('beamngEarthPhone', [function () {
  var S = 'beamngEarth_settings'
  var ICONS = {
    bank: '<path d="M4 10 L16 4 L28 10 Z M6 12 H26 M8 13 V23 M13 13 V23 M19 13 V23 M24 13 V23 M5 25 H27" fill="none" stroke="#fff" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>',
    weather: '<circle cx="12" cy="12" r="5" fill="#ffd34d"/><path d="M10 26 H25 A5 5 0 0 0 24 16 A7 7 0 0 0 11 18 A4 4 0 0 0 10 26 Z" fill="#fff"/>',
    jobs: '<rect x="5" y="10" width="22" height="15" rx="3" fill="none" stroke="#fff" stroke-width="2.2"/><path d="M12 10 V7 H20 V10 M5 16 H27" fill="none" stroke="#fff" stroke-width="2.2"/>',
    music: '<path d="M12 22 V8 L25 5 V19" fill="none" stroke="#fff" stroke-width="2.4" stroke-linejoin="round"/><circle cx="9" cy="22" r="3.5" fill="#fff"/><circle cx="22" cy="19" r="3.5" fill="#fff"/>',
    garage: '<path d="M4 14 L16 6 L28 14 V27 H4 Z" fill="none" stroke="#fff" stroke-width="2.2" stroke-linejoin="round"/><path d="M9 27 V18 H23 V27 M9 21 H23 M9 24 H23" fill="none" stroke="#fff" stroke-width="2"/>',
    realestate: '<path d="M5 15 L16 6 L27 15" fill="none" stroke="#fff" stroke-width="2.4" stroke-linejoin="round"/><path d="M8 13 V26 H24 V13" fill="none" stroke="#fff" stroke-width="2.2"/><path d="M14 26 V19 H18 V26" fill="#fff"/><circle cx="25" cy="7" r="3.5" fill="#ffd34d"/>',
beamazon: '<path d="M6 11 L16 6 L26 11 V23 L16 28 L6 23 Z" fill="#ff9900"/><path d="M6 11 L16 16 L26 11 M16 16 V28" fill="none" stroke="#b36b00" stroke-width="1.4"/><text x="16" y="25" text-anchor="middle" font-family="Arial Black,Arial" font-weight="900" font-size="11" fill="#1a1a1a">B</text>',
    rally: '<path d="M8 5 V28" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/><path d="M9 6 H25 V18 H9 Z" fill="#fff"/><path d="M9 6 H13 V10 H9 Z M17 6 H21 V10 H17 Z M13 10 H17 V14 H13 Z M21 10 H25 V14 H21 Z M9 14 H13 V18 H9 Z M17 14 H21 V18 H17 Z" fill="#111"/>',
    settings: '<circle cx="16" cy="16" r="4.5" fill="none" stroke="#fff" stroke-width="2.4"/><path d="M16 4 V8 M16 24 V28 M4 16 H8 M24 16 H28 M7.5 7.5 L10.3 10.3 M21.7 21.7 L24.5 24.5 M7.5 24.5 L10.3 21.7 M21.7 10.3 L24.5 7.5" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>'
  }
  var APPS = [
    { id: 'bank', name: 'Bank', bg: 'linear-gradient(160deg,#2fbf71,#127a46)' },
    { id: 'weather', name: 'Weather', bg: 'linear-gradient(160deg,#4aa3ff,#1c5fd6)' },
    { id: 'jobs', name: 'Jobs', bg: 'linear-gradient(160deg,#ff9f43,#e0601b)' },
    { id: 'music', name: 'Music', bg: 'linear-gradient(160deg,#1ed760,#118a3e)' },
    { id: 'garage', name: 'Garage', bg: 'linear-gradient(160deg,#ff5e57,#b3261e)' },
    { id: 'beamazon', name: 'Beamazon', bg: 'linear-gradient(160deg,#2b3a4e,#131921)' },
    { id: 'realestate', name: 'Real Estate', bg: 'linear-gradient(160deg,#5e5ce6,#2c2a9e)' },
    { id: 'rally', name: 'Rally', bg: 'linear-gradient(160deg,#e10600,#5c0a0a)' },
    { id: 'settings', name: 'Settings', bg: 'linear-gradient(160deg,#8e8e93,#5a5a60)' }
  ]
  var WX = { clear: '☀', partly: '⛅', cloudy: '☁', fog: '🌫', rain: '🌧', storm: '⛈', snow: '❄' }
  var ACCENTS = ['#0a84ff', '#30d158', '#ff9f0a', '#ff375f', '#bf5af2', '#64d2ff']
  var css = [
    '.bnp-wrap{position:absolute;right:0;bottom:0;width:100%;height:100%;pointer-events:none;display:flex;align-items:flex-end;justify-content:flex-end}',
    '.bnp{pointer-events:auto;width:300px;height:620px;border-radius:46px;background:#0b0b0d;padding:11px;box-sizing:border-box;',
    ' box-shadow:0 20px 50px rgba(0,0,0,.55),inset 0 0 0 2px #2a2a2e;transform-origin:100% 100%;transition:transform .35s cubic-bezier(.2,.8,.2,1),opacity .35s;',
    " font-family:-apple-system,'SF Pro Text','Inter Tight','Segoe UI',sans-serif;}",
    '.bnp.down{transform:translateY(110%);opacity:0;pointer-events:none}',
    '.bnp-scr{position:relative;width:100%;height:100%;border-radius:36px;overflow:hidden;background-size:cover;background-position:center;color:var(--fg)}',
    '.bnp-island{position:absolute;left:50%;top:9px;width:92px;height:26px;margin-left:-46px;border-radius:14px;background:#000;z-index:5}',
    '.bnp-status{position:absolute;left:0;right:0;top:0;height:44px;display:flex;justify-content:space-between;align-items:center;padding:0 22px 0 26px;font-size:13px;font-weight:700;z-index:6}',
    '.bnp-home{position:absolute;inset:56px 0 0 0;padding:14px 18px}',
    '.bnp-widget{background:var(--glass);backdrop-filter:blur(18px);border-radius:22px;padding:12px 14px;margin-bottom:18px;color:var(--fg)}',
    '.bnp-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:18px 10px}',
    '.bnp-app{display:flex;flex-direction:column;align-items:center;text-align:center;line-height:1.15;gap:5px;cursor:pointer;font-size:11px;font-weight:600;text-shadow:0 1px 3px rgba(0,0,0,.6);color:#fff}',
    '.bnp-ico{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;box-shadow:0 3px 8px rgba(0,0,0,.35)}',
    '.bnp-dock{position:absolute;left:12px;right:12px;bottom:12px;height:76px;border-radius:28px;background:var(--glass);backdrop-filter:blur(18px);display:flex;justify-content:space-around;align-items:center}',
    '.bnp-view{position:absolute;inset:0;background:var(--bg);padding:50px 16px 30px;box-sizing:border-box;overflow-y:auto;color:var(--fg)}',
    '.bnp-back{color:var(--accent);font-size:15px;cursor:pointer;margin-bottom:6px;display:inline-block}',
    '.bnp-h{font-size:28px;font-weight:800;margin:2px 0 12px}',
    '.bnp-card{background:var(--card);border-radius:14px;padding:12px 14px;margin-bottom:10px}',
    '.bnp-row{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:14px;padding:5px 0}',
    '.bnp-dim{color:var(--dim);font-size:12px}',
    '.bnp-pos{color:#30d158;font-weight:700}.bnp-neg{color:#ff453a;font-weight:700}',
    '.bnp-btn{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:7px 10px;white-space:nowrap;font-weight:700;font-size:13px;cursor:pointer}',
    '.bnp-btn.alt{background:var(--card2);color:var(--fg)}',
    '.bnp-bar{position:absolute;left:50%;bottom:7px;width:110px;height:5px;margin-left:-55px;border-radius:3px;background:var(--fg);opacity:.75;cursor:pointer;z-index:6}',
    '.bnp-switch{width:51px;height:31px;border-radius:16px;background:var(--card2);position:relative;cursor:pointer;transition:background .2s;flex:none}',
    '.bnp-switch span{position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:14px;background:#fff;box-shadow:0 2px 4px rgba(0,0,0,.3);transition:left .2s}',
    '.bnp-switch.on{background:#30d158}.bnp-switch.on span{left:22px}',
    '.bnp-sw{display:inline-block;width:26px;height:26px;border-radius:13px;margin:0 5px 0 0;cursor:pointer;border:2px solid transparent}',
    '.bnp-sw.on{border-color:var(--fg)}',
    '.bnp-thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}',
    '.bnp-thumb{height:90px;border-radius:9px;background-size:cover;background-position:center;cursor:pointer;border:2px solid transparent}',
    '.bnp-thumb.on{border-color:var(--accent)}',
    '.bnp-seg{display:flex;background:var(--card2);border-radius:9px;padding:2px}.bnp-seg span{flex:1;text-align:center;padding:5px 0;border-radius:7px;font-size:13px;cursor:pointer}',
    '.bnp-seg span.on{background:var(--card);font-weight:700;box-shadow:0 1px 3px rgba(0,0,0,.25)}',
    '.bnp-big{font-size:34px;font-weight:800;letter-spacing:-.5px}',
    '.bnp-mctl{display:flex;justify-content:center;gap:26px;font-size:30px;margin-top:14px}.bnp-mctl span{cursor:pointer}',
    '.bnp-art{width:100%;aspect-ratio:1;border-radius:16px;background:linear-gradient(135deg,#1ed760,#0c4d27);display:flex;align-items:center;justify-content:center;font-size:70px;margin:8px 0 14px}'
  ].join('\n')

  return {
    template:
      '<div class="bnp-wrap"><style>' + css + '</style>' +
      '<div class="bnp" ng-class="{down: !up}" ng-style="{transform: up ? \'scale(\' + (p.prefs.size || 1) + \')\' : undefined}">' +
      '<div class="bnp-scr">' +
        '<div class="bnp-island"></div>' +
        '<div class="bnp-status"><span>{{ p.time }}</span><span style="letter-spacing:-1px">5G ▮</span></div>' +
        // home
        '<div class="bnp-home" ng-if="!app">' +
          '<div class="bnp-widget"><div class="bnp-row" style="padding:0"><span style="font-size:13px;font-weight:700">{{ p.place || "BeamNG.Earth" }}</span><span style="font-size:22px">{{ wx(p.weather.now.cond) }}</span></div>' +
            '<div class="bnp-row" style="padding:0"><span class="bnp-big">{{ deg(p.weather.now.tempC) }}</span><span class="bnp-dim" style="color:inherit;opacity:.8">{{ p.weather.now.label }}<br>H {{ deg(p.weather.days[0].hi) }}  L {{ deg(p.weather.days[0].lo) }}</span></div></div>' +
          '<div class="bnp-widget" style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:13px;font-weight:700">Balance</span><span style="font-size:20px;font-weight:800" ng-class="{\'bnp-neg\': p.bank.balance < 0}">{{ p.bank.text }}</span></div>' +
          '<div class="bnp-grid"><div class="bnp-app" ng-repeat="a in apps" ng-click="open(a.id)"><div class="bnp-ico" ng-style="{background: a.bg}"><div style="width:32px;height:32px" ng-style="{\'background-image\': icon(a.id)}"></div></div>{{ a.name }}</div></div>' +
          '<div class="bnp-dock"><div class="bnp-app" ng-repeat="a in dock" ng-click="open(a.id)"><div class="bnp-ico" ng-style="{background: a.bg}"><div style="width:32px;height:32px" ng-style="{\'background-image\': icon(a.id)}"></div></div></div></div>' +
        '</div>' +
        // bank
        '<div class="bnp-view" ng-if="app == \'bank\'"><span class="bnp-back" ng-click="home()">‹ Home</span><div class="bnp-h">Bank</div>' +
          '<div class="bnp-card"><div class="bnp-dim">Checking</div><div class="bnp-big" ng-class="{\'bnp-neg\': p.bank.balance < 0}">{{ p.bank.text }}</div></div>' +
          '<div class="bnp-dim" style="margin:8px 2px">Recent activity</div>' +
          '<div class="bnp-card" style="padding:4px 14px"><div class="bnp-row" ng-repeat="h in p.bank.history track by $index"><span>{{ h.reason }}</span><span ng-class="h.amount < 0 ? \'bnp-neg\' : \'bnp-pos\'">{{ money(h.amount) }}</span></div>' +
          '<div class="bnp-dim" ng-if="!p.bank.history.length" style="padding:8px 0">No activity yet.</div></div>' +
        '</div>' +
        // weather
        '<div class="bnp-view" ng-if="app == \'weather\'" style="background:linear-gradient(180deg,#3a7bd5,#1d3d75);color:#fff"><span class="bnp-back" style="color:#fff" ng-click="home()">‹ Home</span>' +
          '<div style="text-align:center;margin:4px 0 14px"><div style="font-size:15px;font-weight:600">{{ p.place || "Your location" }}</div><div style="font-size:60px;font-weight:300;line-height:1.1">{{ deg(p.weather.now.tempC) }}</div>' +
          '<div style="font-size:15px">{{ p.weather.now.label }}</div><div style="font-size:14px;opacity:.85">H {{ deg(p.weather.days[0].hi) }}  L {{ deg(p.weather.days[0].lo) }}</div></div>' +
          '<div class="bnp-card" style="background:rgba(255,255,255,.15)"><div class="bnp-dim" style="color:#dfe9ff">TODAY</div><div style="display:flex;justify-content:space-between;margin-top:6px">' +
            '<div ng-repeat="q in p.weather.days[0].periods" style="text-align:center;font-size:12px"><div>{{ q.name }}</div><div style="font-size:22px">{{ wx(q.cond) }}</div><div style="font-weight:700">{{ deg(q.tempC) }}</div></div></div></div>' +
          '<div class="bnp-card" style="background:rgba(255,255,255,.15)"><div class="bnp-dim" style="color:#dfe9ff">7-DAY FORECAST</div>' +
            '<div class="bnp-row" ng-repeat="d in p.weather.days"><span style="width:48px">{{ $index == 0 ? "Today" : dayName($index) }}</span><span style="font-size:18px">{{ wx(d.cond) }}</span><span style="opacity:.75;width:38px;text-align:right">{{ deg(d.lo) }}</span><span style="font-weight:700;width:38px;text-align:right">{{ deg(d.hi) }}</span></div></div>' +
          '<div class="bnp-dim" style="color:#dfe9ff;text-align:center">{{ p.weather.mode == "real" ? "Live weather" : "Dynamic weather" }} · change in Settings</div>' +
        '</div>' +
        // jobs
        '<div class="bnp-view" ng-if="app == \'jobs\'"><span class="bnp-back" ng-click="home()">‹ Home</span><div class="bnp-h">Jobs</div>' +
          '<div class="bnp-dim" style="margin:-6px 2px 10px" ng-if="p.jobs.streak">Clean delivery streak: {{ p.jobs.streak }} (+{{ Math.min(25, p.jobs.streak * 5) }}% pay)</div>' +
          '<div class="bnp-card" ng-if="p.jobs.active && !p.jobs.active.stops"><div class="bnp-dim">{{ p.jobs.active.stage == "pickup" ? "Go to the pickup" : "Delivering" }}</div><div style="font-weight:700;font-size:16px">{{ p.jobs.active.car }}</div>' +
            '<div class="bnp-row"><span class="bnp-dim">To</span><span>{{ p.jobs.active.stops ? "Stop " + p.jobs.active.stop + " of " + p.jobs.active.stops : p.jobs.active.dropoff }}</span></div>' +
            '<div class="bnp-row" ng-if="p.jobs.active.estimate"><span class="bnp-big" style="font-size:24px">{{ clock(p.jobs.active.t) }}</span><span class="bnp-pos">${{ p.jobs.active.estimate.total.toFixed(0) }}</span></div>' +
            '<div class="bnp-dim" ng-if="p.jobs.active.estimate">Bonuses: early +{{ p.jobs.active.estimate.bonus.toFixed(0) }}, spotless +{{ p.jobs.active.estimate.spotless.toFixed(0) }}, clean record +{{ p.jobs.active.estimate.record.toFixed(0) }}, streak +{{ p.jobs.active.estimate.streak.toFixed(0) }}</div>' +
            '<button class="bnp-btn" style="background:#30d158;margin:6px 0;width:100%;font-size:15px;padding:10px" ng-if="p.jobs.active.ready" ng-click="lua(\'uiJobStart()\')">Start work</button>' +
            '<div class="bnp-dim" ng-if="p.jobs.active.stage == \'pickup\' && !p.jobs.active.ready">Pull into the green bay at the pickup, then Start work</div>' +
            '<button class="bnp-btn" style="background:#ff453a" ng-click="lua(\'uiJobCancel()\')">Cancel job</button></div>' +
          '<div ng-if="p.jobs.active && p.jobs.active.stops" class="bnp-dim">You are on a Beamazon route: see the Beamazon app.</div>' +
          '<div ng-if="!p.jobs.active"><div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px"><button class="bnp-btn" ng-click="lua(\'uiJobsRefresh()\')">Car jobs</button><button class="bnp-btn alt" ng-click="lua(\'uiJobsRefresh(\\\'truck\\\')\')">Truck jobs</button></div>' +
            '<div class="bnp-dim" ng-if="p.jobs.searching">Searching…</div><div class="bnp-dim" ng-if="p.jobs.error">{{ p.jobs.error }}</div>' +
            '<div class="bnp-card" ng-repeat="j in p.jobs.offers" ng-if="j.kind != \'parcel\'"><div class="bnp-row" style="padding:0"><b>{{ j.cargo || j.car_name }}</b><span class="bnp-pos">${{ j.pay.toFixed(0) }}</span></div>' +
              '<div class="bnp-dim">{{ j.pickup.name }} → {{ j.dropoff.name }}</div><div class="bnp-row"><span class="bnp-dim">{{ (j.dist / 1609.344).toFixed(1) }} mi<span ng-if="j.rent"> · -${{ j.rent }} rent</span></span><button class="bnp-btn" ng-click="lua(\'uiJobAccept(\' + ($index + 1) + \')\')">Take</button></div></div></div>' +
        '</div>' +
        // music
        '<div class="bnp-view" ng-if="app == \'music\'" style="background:linear-gradient(180deg,#1f3b2a,#0d0f0e);color:#fff"><span class="bnp-back" style="color:#1ed760" ng-click="home()">‹ Home</span>' +
          '<div class="bnp-art">♫</div><div style="font-size:19px;font-weight:800">{{ p.media.title || "Nothing playing" }}</div><div style="opacity:.7;font-size:14px">{{ p.media.artist || (p.media.ok === false ? "Open Spotify on your PC" : "Spotify") }}</div>' +
          '<div class="bnp-mctl"><span ng-click="media(\'prev\')">⏮</span><span ng-click="media(\'play\')">{{ p.media.playing ? "⏸" : "▶" }}</span><span ng-click="media(\'next\')">⏭</span></div>' +
        '</div>' +
        // garage
        // (one view for both: Garage = repair shops; Real Estate = property to buy / rent, and yours)
        '<div class="bnp-view" ng-if="app == \'garage\' || app == \'realestate\'"><span class="bnp-back" ng-click="home()">‹ Home</span><div class="bnp-h">{{ app == "garage" ? "Garage" : "Real Estate" }}</div>' +
          '<div class="bnp-seg" style="margin-bottom:10px" ng-if="app == \'realestate\'"><span ng-class="{on: gtab == \'property\'}" ng-click="setTab(\'property\')">For sale &amp; rent</span><span ng-class="{on: gtab == \'mine\'}" ng-click="setTab(\'mine\')">My properties</span></div>' +
          '<div class="bnp-card" ng-if="g.here && (app == \'garage\' || g.here.kind == \'home\')"><div class="bnp-dim">{{ g.here.kind == "home" ? "At your property" : "At the shop" }}</div><div style="font-weight:700;margin-bottom:6px">{{ g.here.name }}</div>' +
            '<div class="bnp-row" ng-if="app == \'garage\'"><span>Repair</span><button class="bnp-btn" ng-click="gdo(\'repair\')">{{ g.repairCost ? "$" + g.repairCost : "Free" }}</button></div>' +
            '<div class="bnp-row" ng-if="app == \'garage\'"><span>Parts &amp; tuning</span><button class="bnp-btn" ng-click="gdo(\'parts\')">{{ g.here.kind == "home" ? "Free" : "$" + g.prices.labor }}</button></div>' +
            '<div class="bnp-row" ng-if="app == \'garage\'"><span>New plates</span><button class="bnp-btn" ng-click="gdo(\'plates\')">${{ g.prices.plates }}</button></div>' +
            '<div class="bnp-row" ng-if="app == \'garage\'"><span>Respray ${{ g.prices.respray }}</span><span><span class="bnp-sw" ng-repeat="c in g.colours track by $index" ng-style="{background: rgb(c)}" ng-click="gdo(\'respray\', $index + 1)"></span></span></div>' +
            '<div ng-if="g.here.kind == \'home\' && app == \'realestate\'"><div class="bnp-dim" style="margin-top:6px">STORAGE</div>' +
              '<div class="bnp-row" ng-repeat="c in homeCars() track by $index"><span>{{ c.model }} <span class="bnp-dim">{{ c.plate }}</span></span><button class="bnp-btn alt" ng-click="gdo(\'store\', $index + 1)">Swap</button></div>' +
              '<button class="bnp-btn alt" style="margin-top:4px" ng-click="gdo(\'store\')">Store a copy of this car</button></div>' +
          '</div>' +
          '<div ng-if="gtab != \'mine\'"><button class="bnp-btn" style="margin-bottom:8px" ng-click="lua(\'uiGarageRefresh(\\\'\' + gtab + \'\\\')\')">{{ g.searching[gtab] ? "Searching…" : "Find near me" }}</button>' +
            '<div class="bnp-dim" ng-if="g.errors[gtab]">{{ g.errors[gtab] }}</div>' +
            '<div class="bnp-card" ng-repeat="s in g[gtab] track by $index"><div class="bnp-row" style="padding:0"><b>{{ s.name }}</b><span class="bnp-dim">{{ dist(s.dist) }}</span></div>' +
              '<div class="bnp-row" ng-if="gtab == \'property\'"><span class="bnp-dim">Buy ${{ s.buy | number:0 }} · Rent ${{ s.rent }}/wk</span></div>' +
              '<div style="display:flex;gap:6px;margin-top:4px"><button class="bnp-btn alt" ng-click="lua(\'uiGarageNav(\\\'\' + gtab + \'\\\', \' + ($index + 1) + \')\')">Navigate</button>' +
              '<button class="bnp-btn" ng-if="gtab == \'property\'" ng-click="gdo(\'rent\', $index + 1)">Rent</button><button class="bnp-btn" ng-if="gtab == \'property\'" ng-click="gdo(\'buy\', $index + 1)">Buy</button></div></div></div>' +
          '<div ng-if="gtab == \'mine\'"><div class="bnp-dim" ng-if="!g.mine.length">You have no property yet: find one under Property.</div>' +
            '<div class="bnp-card" ng-repeat="m in g.mine track by $index"><div class="bnp-row" style="padding:0"><b>{{ m.name }}</b><span class="bnp-dim">{{ m.kind == "own" ? "Owned" : (m.held ? m.daysLeft + " days left" : "Rent due") }}</span></div>' +
              '<div class="bnp-dim">{{ m.cars.length }} car(s) stored<span ng-if="m.kind == \'own\' && m.paid"> · sells for ${{ (m.paid * 0.7) | number:0 }}</span></div>' +
              '<div style="display:flex;gap:6px;margin-top:4px"><button class="bnp-btn alt" ng-click="lua(\'uiGarageNav(\\\'mine\\\', \' + ($index + 1) + \')\')">Navigate</button>' +
              '<button class="bnp-btn" ng-if="m.kind != \'own\'" ng-click="gdo(\'buyout\', m.id)">Buy{{ m.buy ? " $" + (m.buy | number:0) : "" }}</button>' +
              '<button class="bnp-btn" style="background:#ff453a" ng-click="gdo(\'sell\', m.id)">{{ m.kind == "own" ? "Sell" : "End lease" }}</button></div></div></div>' +
          '<div class="bnp-dim" ng-if="gmsg" style="margin-top:6px">{{ gmsg }}</div>' +
        '</div>' +
        // Beamazon (the user, 2026-10-26: "jobs and beamazon shouldn't be the same thing, make beamazon its own thing
        // inside its own app"): its routes, the route you are on
        '<div class="bnp-view" ng-if="app == \'beamazon\'" style="background:#131921;color:#fff"><span class="bnp-back" style="color:#ff9900" ng-click="home()">‹ Home</span>' +
          '<div style="display:flex;align-items:center;gap:10px;margin:4px 0 14px"><div style="width:44px;height:44px;border-radius:11px;background-size:cover" ng-style="{\'background-image\': icon(\'beamazon\')}"></div>' +
            '<div><div style="font-size:26px;font-weight:900;letter-spacing:-.5px">beamazon</div><div style="font-size:12px;color:#ff9900">Delivery partner</div></div></div>' +
          '<div class="bnp-card" style="background:#232f3e" ng-if="p.jobs.active && p.jobs.active.stops">' +
            '<div style="color:#ff9900;font-size:12px;font-weight:700">{{ p.jobs.active.stage == "pickup" ? "GO TO THE DEPOT" : "ON ROUTE" }}</div>' +
            '<div style="font-size:20px;font-weight:800">{{ p.jobs.active.stage == "pickup" ? p.jobs.active.pickup : "Stop " + p.jobs.active.stop + " of " + p.jobs.active.stops }}</div>' +
            '<div style="height:6px;border-radius:3px;background:#37475a;margin:8px 0"><div style="height:6px;border-radius:3px;background:#ff9900" ng-style="{width: ((p.jobs.active.stop - 1) / p.jobs.active.stops * 100) + \'%\'}"></div></div>' +
            '<div class="bnp-row" ng-if="p.jobs.active.estimate"><span class="bnp-big" style="font-size:22px">{{ clock(p.jobs.active.t) }}</span><span class="bnp-pos">${{ p.jobs.active.estimate.total.toFixed(0) }}</span></div>' +
            '<button class="bnp-btn" style="background:#ff9900;color:#111;margin:6px 0;width:100%;font-size:15px;padding:10px" ng-if="p.jobs.active.ready" ng-click="lua(\'uiJobStart()\')">Start work</button>' +
            '<div style="font-size:12px;color:#c9ced6" ng-if="p.jobs.active.stage == \'pickup\' && !p.jobs.active.ready">Pull into the green bay at the depot, then Start work</div>' +
            '<button class="bnp-btn" style="background:#ff453a" ng-click="lua(\'uiJobCancel()\')">Cancel route</button></div>' +
          '<div ng-if="!p.jobs.active"><button class="bnp-btn" style="background:#ff9900;color:#111;margin-bottom:10px" ng-click="lua(\'uiJobsRefresh(\\\'parcel\\\')\')">{{ p.jobs.searching ? "Finding routes…" : "Find routes" }}</button>' +
            '<div class="bnp-card" style="background:#232f3e" ng-repeat="j in p.jobs.offers" ng-if="j.kind == \'parcel\'">' +
              '<div class="bnp-row" style="padding:0"><b>{{ j.stops.length }} packages</b><span class="bnp-pos">${{ j.pay.toFixed(0) }}</span></div>' +
              '<div style="font-size:12px;color:#c9ced6">From {{ j.pickup.name }} · {{ (j.dist / 1609.344).toFixed(1) }} mi · {{ clock(j.limit) }}</div>' +
              '<div style="text-align:right;margin-top:4px"><button class="bnp-btn" style="background:#ff9900;color:#111" ng-click="lua(\'uiJobAccept(\' + ($index + 1) + \')\')">Start route</button></div></div></div>' +
        '</div>' +
        // Rally (the user, 2026-10-26: a real US stage, timed with pacenotes, in a loaned rally car): the STPR's forest
        // stages with your best times; the run you are on
        '<div class="bnp-view" ng-if="app == \'rally\'" style="background:#0d0d0f;color:#fff"><span class="bnp-back" style="color:#ff3b30" ng-click="home()">‹ Home</span>' +
          '<div style="font-size:26px;font-weight:900;font-style:italic;letter-spacing:-.5px;margin:4px 0 2px">RALLY</div><div style="font-size:12px;color:#ff3b30;margin-bottom:12px">Susquehannock Trail stages · Pennsylvania</div>' +
          '<div class="bnp-card" style="background:#1d1d21" ng-if="p.rally.active">' +
            '<div style="color:#ff3b30;font-size:12px;font-weight:700">{{ rphase(p.rally.active.phase) }}</div>' +
            '<div style="font-size:20px;font-weight:800">{{ p.rally.active.name }}<span ng-if="p.rally.active.car" style="font-size:12px;color:#aaa;font-weight:400"> · {{ p.rally.active.car }}</span></div>' +
            '<div ng-if="p.rally.active.phase == \'run\'"><div class="bnp-big" style="font-size:30px;font-variant-numeric:tabular-nums">{{ rtime(p.rally.active.t) }}</div>' +
              '<div style="height:6px;border-radius:3px;background:#333;margin:8px 0"><div style="height:6px;border-radius:3px;background:#ff3b30" ng-style="{width: (p.rally.active.s / p.rally.active.total * 100) + \'%\'}"></div></div>' +
              '<div style="font-size:12px;color:#aaa">{{ dist(p.rally.active.s) }} of {{ dist(p.rally.active.total) }}<span ng-if="p.rally.active.penalty"> · +{{ p.rally.active.penalty }}s penalty</span></div>' +
              '<div style="display:flex;gap:10px;margin-top:4px;font-size:12px;font-variant-numeric:tabular-nums"><span ng-repeat="t in p.rally.active.targets" ng-style="{color: medalColor(t.name), opacity: (p.rally.active.t + (p.rally.active.penalty || 0)) > t.time ? 0.35 : 1}">● {{ rtime(t.time) }}</span></div></div>' +
            '<div style="display:flex;gap:8px;margin-top:8px"><button class="bnp-btn" style="background:#0a84ff" ng-if="p.rally.active.phase == \'run\'" ng-click="lua(\'uiRallyReset()\')">Reset to road</button>' +
            '<button class="bnp-btn" style="background:#ff453a" ng-click="lua(\'uiRallyQuit()\')">Quit stage</button></div></div>' +
          '<div class="bnp-card" style="background:#1d1d21" ng-if="p.rally.last && !p.rally.active">' +
            '<div style="font-size:12px;color:#aaa">LAST RUN · {{ p.rally.last.name }}</div>' +
            '<div class="bnp-big" style="font-size:26px" ng-if="!p.rally.last.dnf">{{ rtime(p.rally.last.time) }}<span ng-if="p.rally.last.record" style="font-size:12px;color:#30d158"> BEST</span></div>' +
            '<div ng-if="!p.rally.last.dnf" style="font-size:13px;margin-top:2px" ng-style="{color: medalColor(p.rally.last.medal)}">{{ p.rally.last.medal ? p.rally.last.medal + " · +$" + p.rally.last.prize : "No medal" }}</div>' +
            '<div class="bnp-big" style="font-size:26px;color:#ff453a" ng-if="p.rally.last.dnf">DNF</div></div>' +
          '<div class="bnp-dim" ng-if="p.rally.error || rmsg" style="color:#ff9f0a;margin-bottom:6px">{{ rmsg || p.rally.error }}</div>' +
          '<div class="bnp-card" style="background:#1d1d21" ng-repeat="s in p.rally.stages" ng-if="!p.rally.active">' +
            '<div class="bnp-row" style="padding:0"><b style="font-size:17px">{{ s.name }}</b><span style="font-size:13px;color:#aaa">{{ s.miles.toFixed(2) }} mi</span></div>' +
            '<div style="font-size:12px;color:#aaa">{{ s.area }} · gravel<span ng-if="s.best"> · Best {{ rtime(s.best) }}</span></div>' +
            '<div style="display:flex;gap:10px;margin-top:6px;font-size:12px;font-variant-numeric:tabular-nums"><span ng-repeat="t in s.targets" ng-style="{color: medalColor(t.name)}">● {{ rtime(t.time) }} <span style="color:#aaa">${{ t.prize }}</span></span></div>' +
            '<div style="text-align:right;margin-top:6px"><button class="bnp-btn" style="background:#e10600" ng-disabled="p.rally.active" ng-click="rstart(s.id)">Enter ${{ s.fee }}</button></div></div>' +
          '<div class="bnp-card" style="background:#1d1d21" ng-if="!p.rally.active"><div style="font-size:12px;color:#aaa;margin-bottom:6px">CO-DRIVER</div>' +
            '<div style="display:flex;gap:6px;flex-wrap:wrap"><button class="bnp-btn" ng-repeat="v in p.rally.voices" ng-style="{background: p.rally.voice == v.id ? \'#e10600\' : \'#3a3a3e\'}" ng-click="rvoice(v.id)">{{ v.name }}</button>' +
            '<button class="bnp-btn" ng-style="{background: p.rally.voice == \'off\' ? \'#e10600\' : \'#3a3a3e\'}" ng-click="rvoice(\'off\')">Off</button></div></div>' +
          '<div class="bnp-dim" style="font-size:11px">Entry is refunded if you quit before GO. A rally car is loaned for the stage; your car is parked and given back at the finish. The co-driver calls each corner (also shown on screen).</div>' +
        '</div>' +
        // settings
        '<div class="bnp-view" ng-if="app == \'settings\'"><span class="bnp-back" ng-click="home()">‹ Home</span><div class="bnp-h">Settings</div>' +
          '<div class="bnp-dim" style="margin:0 2px 6px">WALLPAPER</div><div class="bnp-card"><div class="bnp-thumbs"><div class="bnp-thumb" ng-repeat="w in p.wallpapers track by $index" ng-class="{on: w == p.prefs.wallpaper}" ng-style="{\'background-image\': \'url(\' + w + \')\'}" ng-click="set(\'wallpaper\', w)"></div></div>' +
            '<div class="bnp-dim" style="margin-top:8px">Your screenshots and the pictures in BeamNG.drive\\\\settings\\\\beamngEarth_wallpapers</div></div>' +
          '<div class="bnp-dim" style="margin:10px 2px 6px">APPEARANCE</div><div class="bnp-card">' +
            '<div class="bnp-row"><span>Theme</span><div class="bnp-seg" style="width:140px"><span ng-class="{on: p.prefs.theme != \'light\'}" ng-click="set(\'theme\', \'dark\')">Dark</span><span ng-class="{on: p.prefs.theme == \'light\'}" ng-click="set(\'theme\', \'light\')">Light</span></div></div>' +
            '<div class="bnp-row"><span>Accent</span><span><span class="bnp-sw" ng-repeat="c in accents" ng-class="{on: c == accent()}" ng-style="{background: c}" ng-click="set(\'accent\', c)"></span></span></div>' +
            '<div class="bnp-row"><span>Size</span><input type="range" min="0.7" max="1.3" step="0.05" ng-model="size" ng-change="set(\'size\', size)" style="width:140px"></div></div>' +
          '<div class="bnp-dim" style="margin:10px 2px 6px">WEATHER</div><div class="bnp-card"><div class="bnp-seg"><span ng-class="{on: p.weather.mode != \'real\'}" ng-click="lua(\'uiPhoneWeatherMode(\\\'dynamic\\\')\')">Dynamic</span><span ng-class="{on: p.weather.mode == \'real\'}" ng-click="lua(\'uiPhoneWeatherMode(\\\'real\\\')\')">Live (real)</span></div></div>' +
          '<div class="bnp-dim" style="margin:10px 2px 6px">WORLD</div><div class="bnp-card"><div class="bnp-row"><span>World traffic<br><span class="bnp-dim">15 cars, 2 of them police</span></span>' +
            '<span class="bnp-switch" ng-class="{on: p.prefs.traffic}" ng-click="set(\'traffic\', !p.prefs.traffic)"><span></span></span></div></div>' +
          '<div class="bnp-dim" style="margin:10px 2px 6px">TESTING</div><div class="bnp-card"><button class="bnp-btn" ng-click="lua(\'uiCashBoost()\')">+$10,000 test cash</button></div>' +
          '<div class="bnp-dim" style="margin:10px 2px">Phone key: Ctrl+O (Options > Controls to change)</div>' +
        '</div>' +
        '<div class="bnp-bar" ng-click="home()"></div>' +
      '</div></div></div>',
    replace: true,
    link: function (scope, element) {
      'use strict'
      scope.up = false
      scope.app = null
      scope.p = { prefs: {}, bank: {}, weather: { days: [{}], now: {} }, jobs: {}, media: {}, wallpapers: [] }
      scope.apps = APPS
      scope.dock = [APPS[0], APPS[2], APPS[4], APPS[APPS.length - 1]]
      scope.accents = ACCENTS
      scope.icon = function (id) {
        return 'url("data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">' + ICONS[id] + '</svg>') + '")'
      }
      scope.wx = function (c) { return WX[c] || '' }
      scope.deg = function (c) {
        if (c == null || !isFinite(c)) return '--'
        return (scope.p.mph !== false ? Math.round(c * 9 / 5 + 32) : Math.round(c)) + '°'
      }
      scope.money = function (v) { return (v < 0 ? '-$' : '+$') + Math.abs(v).toFixed(2) }
      scope.clock = function (t) { t = Math.max(0, Math.round(t || 0)); var m = Math.floor(t / 60), s = t % 60; return m + ':' + (s < 10 ? '0' : '') + s }
      scope.dayName = function (i) { return ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][((scope.p.weekday || 0) + i) % 7] }
      scope.accent = function () { return scope.p.prefs.accent || ACCENTS[0] }
      function themeVars() {
        var light = scope.p.prefs.theme === 'light'
        var wp = scope.p.prefs.wallpaper
        scope.vars = {
          '--accent': scope.accent(),
          '--fg': light ? '#111' : '#f5f5f7', '--dim': light ? '#6e6e73' : '#98989f',
          '--bg': light ? '#f2f2f7' : '#000', '--card': light ? '#fff' : '#1c1c1e', '--card2': light ? '#e5e5ea' : '#2c2c2e',
          '--glass': light ? 'rgba(255,255,255,.55)' : 'rgba(30,30,32,.55)',
          'background-image': wp ? 'url(' + wp + ')' : 'linear-gradient(160deg,#3a1c71,#d76d77 55%,#ffaf7b)'
        }
        // (custom properties need setProperty: ng-style's style[name] = value drops them)
        var scr = element[0].querySelector('.bnp-scr')
        if (scr) for (var k in scope.vars) scr.style.setProperty(k, scope.vars[k])
        scope.size = Number(scope.p.prefs.size) || 1
      }
      setTimeout(function () { themeVars() }, 0)
      function lua(cmd, cb) { if (window.bngApi) bngApi.engineLua(S + ' and ' + S + '.' + cmd, cb || function () { poll() }) }
      scope.lua = function (cmd) { lua(cmd) }
      scope.open = function (id) {
        scope.app = id
        if (id == 'garage') scope.gtab = 'repair'
        else if (id == 'realestate' && scope.gtab == 'repair') scope.gtab = 'property'
      }
      scope.home = function () { scope.app = null }
      scope.set = function (k, v) {
        scope.p.prefs[k] = v
        themeVars()
        lua('uiPhoneSet(' + JSON.stringify(k) + ', ' + JSON.stringify(v) + ')')
      }
      scope.Math = Math
      scope.gtab = 'repair'
      // (a setter: ng-click="gtab=..." inside an ng-if wrote a child scope's copy, so the tab lit up but the list
      // underneath kept the old tab: the user's "My properties" showed the listings, 2026-10-26)
      scope.setTab = function (tab) { scope.gtab = tab }
      scope.g = { errors: {}, searching: {}, prices: {}, mine: [] }
      scope.rgb = function (c) { return 'rgb(' + Math.round(c[0] * 255) + ',' + Math.round(c[1] * 255) + ',' + Math.round(c[2] * 255) + ')' }
      scope.dist = function (m) { return scope.p.mph !== false ? (m / 1609.344).toFixed(1) + ' mi' : (m / 1000).toFixed(1) + ' km' }
      scope.homeCars = function () {
        var h = scope.g.here; if (!h) return []
        for (var i = 0; i < (scope.g.mine || []).length; i++) if (scope.g.mine[i].id == h.id) return scope.g.mine[i].cars || []
        return []
      }
      scope.gdo = function (a, arg) {
        lua('uiGarageDo(' + JSON.stringify(a) + (arg != null ? ', ' + JSON.stringify(arg) : '') + ')', function (r) {
          scope.$evalAsync(function () { scope.gmsg = r && r.error ? r.error : '' })
          poll()
        })
      }
      scope.rtime = function (t) { t = Math.max(0, t || 0); var m = Math.floor(t / 60), s = t - m * 60; return m + ':' + (s < 10 ? '0' : '') + s.toFixed(1) }
      scope.rphase = function (ph) { return { loading: 'LOADING STAGE…', travel: 'GOING TO THE START…', countdown: 'STAND BY', run: 'STAGE', done: 'FINISHED' }[ph] || '' }
      scope.rstart = function (id) {
        lua('uiRallyStart(' + JSON.stringify(id) + ')', function (r) {
          scope.$evalAsync(function () { scope.rmsg = r && r.error ? r.error : '' })
          poll()
        })
      }
      scope.medalColor = function (m) { return { Gold: '#ffd60a', Silver: '#c7c7cc', Bronze: '#d08a4a' }[m] || '#aaa' }
      scope.rvoice = function (id) { lua('uiRallyVoice(' + JSON.stringify(id) + ')') }
      scope.media = function (a) { lua('uiPhoneMedia(' + JSON.stringify(a) + ')') }
      function poll() {
        lua('uiPhone and ' + S + '.uiPhone() or nil', function (p) {
          scope.$evalAsync(function () {
            if (p) {
              scope.p = p
              p.prefs = p.prefs || {}
              p.bank = p.bank || {}
              p.weather = p.weather || { days: [{}], now: {} }
              scope.g = p.garage || scope.g
              scope.g.errors = scope.g.errors || {}; scope.g.searching = scope.g.searching || {}
              if (p.up != null) scope.up = !!p.up
              themeVars()
            }
          })
        })
      }
      var timer = setInterval(function () { if (scope.up) poll() }, 1000)
      scope.$on('BneEarthPhone', function (_, d) {
        scope.$evalAsync(function () {
          scope.up = d && d.up != null ? !!d.up : !scope.up
          if (d && d.app) scope.open(d.app)           // (a garage spot: straight to its menu)
          if (scope.up) poll()
        })
      })
      poll()
      scope.$on('$destroy', function () { clearInterval(timer) })
    }
  }
}]);
