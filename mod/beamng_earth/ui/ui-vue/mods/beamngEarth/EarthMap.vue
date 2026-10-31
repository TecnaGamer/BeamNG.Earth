<template>
  <div class="earth-map" :class="{ embedded }" @keydown.esc.stop="onEscape" tabindex="-1" ref="rootEl"
       v-bng-on-ui-nav:action_2,context="onNavDeclared">
    <!-- the map itself: tiles, the lat/lon grid, markers -->
    <div
      class="map-view"
      ref="viewEl"
      @mousedown="onDown"
      @wheel.prevent="onWheel"
      @dblclick.prevent="onDblClick"
      @contextmenu.prevent="onContext"
    >
      <!-- the base layer, then the roads & labels over it; each with its coarser tiles stretched under the missing ones -->
      <template v-for="L in tileLayers" :key="L.id">
        <img
          v-for="t in L.fallback"
          :key="L.id + t.key"
          class="map-tile"
          :class="{ 'tile-dark': baseLayer === 'dark' && !L.overlay }"
          :src="t.src"
          :style="{ left: t.left + 'px', top: t.top + 'px', width: t.size + (L.overlay ? 0 : 1) + 'px', height: t.size + (L.overlay ? 0 : 1) + 'px' }"
          draggable="false"
        />
        <img
          v-for="t in L.tiles"
          :key="L.id + ':' + t.key"
          class="map-tile"
          :class="{ 'tile-dark': baseLayer === 'dark' && !L.overlay }"
          :src="t.src"
          :style="{ left: t.left + 'px', top: t.top + 'px', width: t.size + (L.overlay ? 0 : 1) + 'px', height: t.size + (L.overlay ? 0 : 1) + 'px' }"
          draggable="false"
          @load="onTileLoad"
          @error="onTileError($event, t)"
        />
      </template>
      <!-- the cache view (server covermap.py): what is downloaded where, at what detail; under the grid and markers -->
      <canvas v-show="cacheOn" ref="cacheEl" class="cache-canvas" :width="w" :height="h" />
      <!-- saved areas (server areas.py) and the box being picked -->
      <div v-for="a in areaRects" :key="'ar' + a.id" class="area-rect" :class="a.cls"
           :style="{ left: a.x + 'px', top: a.y + 'px', width: a.w + 'px', height: a.h + 'px' }">
        <span class="area-rect-label">{{ a.name }}</span>
      </div>
      <div v-if="selectRect" class="area-rect selecting"
           :style="{ left: selectRect.x + 'px', top: selectRect.y + 'px', width: selectRect.w + 'px', height: selectRect.h + 'px' }" />
      <div v-if="selecting" class="pad-hint" :class="{ 'below-gps': !!state.gps }">{{ selectHint }}</div>
      <!-- a controller: the point A picks (the centre; padTick) -->
      <div v-if="padMode" class="pad-cross" />
      <div v-if="padMode && !selecting" class="pad-hint" :class="{ 'below-gps': !!state.gps }">Right stick: move &middot; LT / RT: zoom
        &middot; A: {{ state.gps ? "clear GPS" : "set GPS here" }} &middot; X: teleport &middot; Y: {{ state.gps &&
        state.gps.autodrive ? "stop autodrive" : "autodrive" }} &middot; B: back</div>
      <svg class="map-grid" :width="w" :height="h">
        <g v-if="layersOn" :transform="lineTransform">
          <polyline v-for="(p, i) in stateLines" :key="'s' + i" class="layer-state" :points="p" />
          <polyline v-for="(p, i) in borderLines" :key="'b' + i" class="layer-border" :points="p" />
        </g>
        <polyline v-for="r in areaRoutes" :key="'rt' + r.id" class="area-route" :class="r.cls" :points="r.points"
                  :style="{ strokeWidth: r.px + 'px' }" />
        <g v-if="roadsDrivenOn">
          <polyline v-for="(p, i) in drivenLines" :key="'rdg_' + i" class="driven-line-glow" :points="p" />
          <polyline v-for="(p, i) in drivenLines" :key="'rd_' + i" class="driven-line" :points="p" />
        </g>
        <polyline v-if="routeDone" class="route-done" :points="routeDone" />
        <polyline v-if="routeAhead" class="route-line-casing" :points="routeAhead" />
        <polyline v-if="routeAhead" class="route-line" :points="routeAhead" />
        <g v-for="g in grid" :key="g.key">
          <line :x1="g.x1" :y1="g.y1" :x2="g.x2" :y2="g.y2" :class="g.cls" />
          <text v-if="g.label" :x="g.lx" :y="g.ly" class="grid-label">{{ g.label }}</text>
        </g>
        <template v-if="layersOn">
          <text v-for="l in countryLabels" :key="'c' + l.name" :x="l.x" :y="l.y" class="layer-country">{{ l.name }}</text>
          <g v-for="l in placeLabels" :key="'p' + l.name + l.x.toFixed(0)">
            <circle :cx="l.x" :cy="l.y" :r="l.rank ? 3 : 2" class="layer-dot" />
            <text :x="l.x + 5" :y="l.y - 3" class="layer-place" :class="{ capital: l.rank }">{{ l.name }}</text>
          </g>
        </template>
      </svg>
      <!-- saved places -->
      <div
        v-for="p in (panelOpen ? placeMarkers : [])"
        :key="'p' + p.id"
        class="marker place"
        :style="{ left: p.sx + 'px', top: p.sy + 'px' }"
        @mousedown.stop
        @click.stop="setGps(p.lat, p.lon, p.name)"
      >
        <div class="pin"></div>
        <div class="marker-label">{{ p.name }}</div>
      </div>
      <!-- the GPS destination -->
      <div v-if="gpsScreen" class="marker gps" :style="{ left: gpsScreen.x + 'px', top: gpsScreen.y + 'px' }">
        <div class="flag"></div>
      </div>
      <!-- the pending target -->
      <div v-if="target" class="marker target" :style="{ left: targetScreen.x + 'px', top: targetScreen.y + 'px' }">
        <div class="cross"></div>
      </div>
      <!-- the other players (a BeamMP session): a click on a name follows that player (as the Players list's Follow) -->
      <div v-for="o in otherMarkers" :key="'o' + o.pid" class="marker other" :class="{ followed: follow === o.pid }"
           :style="{ left: o.sx + 'px', top: o.sy + 'px' }">
        <div class="arrow" :style="{ transform: 'rotate(' + (o.h || 0) + 'deg)' }" :class="{ noheading: o.h == null }"></div>
        <span class="marker-label" :title="follow === o.pid ? 'Stop following' : 'Follow on the map'" @mousedown.stop @click.stop="followPlayer(o)" @dblclick.stop>{{ o.name }}</span>
      </div>
      <!-- the player -->
      <div v-if="playerScreen" class="marker player" :style="{ left: playerScreen.x + 'px', top: playerScreen.y + 'px' }">
        <div class="arrow" :style="{ transform: 'rotate(' + (state.player.heading || 0) + 'deg)' }" :class="{ noheading: state.player.heading == null }"></div>
      </div>
    </div>

    <!-- top bar: title, search, buttons -->
    <div class="map-top">
      <div class="map-title">BeamNG.Earth map</div>
      <!-- the player's local time + weather (settings.uiMapState's weather: sky.lua's real clock for the lat/lon,
           weather.lua's last message), 2026-10-27 -->
      <div v-if="weatherBadge" class="weather-badge">
        <span v-if="weatherBadge.icon">{{ weatherBadge.icon }}</span>
        <span v-if="weatherBadge.temp != null">{{ weatherBadge.temp }}</span>
        <span v-if="weatherBadge.localTime">{{ weatherBadge.localTime }}</span>
      </div>
      <div v-if="state.roadsDriven && state.roadsDriven.totalKm > 0" class="odometer-badge" title="Exploration Odometer (Issue #365): Total distance driven on Earth">
        <span class="odo-icon">🛣️</span>
        <span class="odo-val">{{ (state.roadsDriven.totalKm || 0).toFixed(1) }} km</span>
      </div>
      <div v-if="view.z >= 13" class="zoom-level-badge" title="Street Level View">
        <span>🔍 Street view (z{{ view.z.toFixed(0) }})</span>
      </div>
      <div class="search">
        <input
          ref="searchEl"
          @focus="typing(true)"
          @blur="typing(false)"
          v-model="query"
          class="search-input"
          placeholder="Search addresses, places, or lat, lon"
          @keydown.enter.stop.prevent="onSearch"
          @keydown.down.stop.prevent="moveResult(1)"
          @keydown.up.stop.prevent="moveResult(-1)"
          @keydown.esc.stop.prevent="query = ''"
          @mousedown.stop
        />
        <div v-if="results.length" class="search-results">
          <div v-for="(r, i) in results" :key="i" class="search-result" :class="{ active: i === resultIndex }"
               @mousedown.stop @click="pickResult(r)">
            <span class="result-name">{{ r.saved ? "★ " : "" }}{{ r.name }}</span>
            <span class="result-coords">{{ r.detail || fmtLatLon(r.lat, r.lon) }}</span>
          </div>
          <div class="search-credit">Search: Photon by komoot, © OpenStreetMap contributors</div>
        </div>
        <div v-if="searchMsg" class="search-msg">{{ searchMsg }}</div>
        <div v-else-if="searching && !results.length" class="search-msg">Searching…</div>
      </div>
      <button class="map-btn" @click="zoomBy(1)" title="Zoom in">+</button>
      <button class="map-btn" @click="zoomBy(-1)" title="Zoom out">-</button>
      <button class="map-btn" :class="{ on: follow === true }" @click="centreOnMe" title="Centre on the car and follow it">Me</button>
      <button class="map-btn" :class="{ on: selecting }" @click="toggleSelect"
              title="Pick an area to pre-cache: its map data, elevation and imagery downloaded ahead of time, and the world built too if you like (or Shift + drag on the map)">Pre-cache area</button>
      <!-- the other players of a BeamMP session, nearest first: follow one on the map, or a GPS route to them -->
      <div v-if="state.others" class="layers-wrap">
        <button class="map-btn" :class="{ on: playersPanel || typeof follow === 'string' }"
                @click="playersPanel = !playersPanel; layersPanel = false"
                :title="followedPlayer ? 'Following ' + followedPlayer.name + ' (drag the map to stop)' : 'The other players'">
          <span v-if="followedPlayer" class="following">Following {{ followedPlayer.name }}</span>
          <template v-else>Players ({{ otherList.length }})</template>
        </button>
        <div v-if="playersPanel" class="layers-panel players-panel" :style="{ maxHeight: `calc(${h}px - 74px - 6.5vh)` }"
             @mousedown.stop @wheel.stop @dblclick.stop>
          <div class="players-head">
            <span class="panel-sub">Other players</span>
            <button class="map-btn small" :disabled="!otherList.length" @click="showAllPlayers"
                    title="Zoom out to show everyone">Show all</button>
          </div>
          <div v-for="p in playerRows" :key="p.pid" class="panel-item">
            <div class="item-text" @click="followPlayer(p)" title="Follow on the map">
              <div class="item-name">{{ p.name }}</div>
              <div class="item-sub">{{ p.dist == null ? "" : fmtMiles(p.dist) + " away" }}</div>
            </div>
            <div class="item-btns">
              <button class="map-btn small" :class="{ on: follow === p.pid }" @click="followPlayer(p)"
                      :title="follow === p.pid ? 'Stop following' : 'Keep the map on them as they move, until you drag the map (the map only: your car stays where it is)'">{{ follow === p.pid ? "Following" : "Follow" }}</button>
              <button class="map-btn small" @click="gpsToPlayer(p)" title="A GPS route to where they are now">GPS</button>
            </div>
          </div>
          <div v-if="!otherList.length" class="panel-empty">No other players on the map yet.</div>
        </div>
      </div>
      <div class="layers-wrap">
        <button class="map-btn" :class="{ on: layersPanel }" @click="layersPanel = !layersPanel; playersPanel = false" title="Base map and overlays">Layers</button>
        <div v-if="layersPanel" class="layers-panel" @mousedown.stop @wheel.stop @dblclick.stop>
          <div class="panel-sub">Base map</div>
          <label class="layer-opt"><input type="radio" value="sat" v-model="baseLayer" /> Satellite</label>
          <label class="layer-opt"><input type="radio" value="map" v-model="baseLayer" /> Map</label>
          <label class="layer-opt"><input type="radio" value="dark" v-model="baseLayer" /> Dark</label>
          <div class="panel-sub">Overlays</div>
          <label class="layer-opt"><input type="checkbox" v-model="labelsOn" /> Roads &amp; labels</label>
          <label class="layer-opt"><input type="checkbox" v-model="layersOn" /> Borders &amp; places
            <span class="layer-hint">(world to city zoom)</span></label>
          <label class="layer-opt" title="What is downloaded on this computer, and at what detail (LB / RB: the next view)">
            <input type="checkbox" v-model="cacheOn" /> Cache <span class="layer-hint">(downloaded data)</span></label>
          <BngSelect v-if="cacheOn" class="cache-select" :value="cacheMode" :options="cacheModeOptions"
                     :config="cacheSelectConfig" @valueChanged="v => (cacheMode = v)" />
          <label class="layer-opt"><input type="checkbox" v-model="roadsDrivenOn" /> Roads driven tracker
            <span class="layer-hint" v-if="state.roadsDriven">({{ (state.roadsDriven.totalKm || 0).toFixed(1) }} km)</span></label>
        </div>
      </div>
      <button v-if="!embedded" class="map-btn close" @click="close" title="Close (Esc / M)">Close</button>
    </div>

    <!-- the cache view's legend (bumpers: the next view) -->
    <div v-if="cacheOn" class="cache-legend" @mousedown.stop @wheel.stop>
      <div class="cache-legend-title">Cache: {{ mm.CACHE_MODE_NAMES[cacheMode] }}</div>
      <div v-for="c in cacheLegendRows" :key="c.id" class="cache-legend-row">
        <span class="cache-swatch" :style="{ background: c.css }"></span>{{ c.name }}<span v-if="c.note" class="layer-hint">&nbsp;{{ c.note }}</span>
      </div>
      <div v-if="cacheMode === 'elevation' || cacheMode === 'imagery'" class="cache-legend-hint">Faint: only part of the square</div>
      <div v-if="cacheMsg" class="cache-legend-hint">{{ cacheMsg }}</div>
    </div>

    <!-- status: tiles unavailable / not connected -->
    <div v-if="notice" class="map-notice">{{ notice }}</div>
    <div v-if="toast" class="map-toast">{{ toast }}</div>

    <!-- right-click menu -->
    <div v-if="ctx" class="ctx-menu" :style="{ left: ctx.x + 'px', top: ctx.y + 'px' }" @mousedown.stop @contextmenu.prevent>
      <div class="ctx-where">{{ fmtLatLon(ctx.lat, ctx.lon) }}</div>
      <div class="ctx-item" :class="{ disabled: !!state.cannotGo }" @click="ctxTeleport">Teleport here</div>
      <div class="ctx-item" @click="ctxGps">Set GPS destination</div>
      <div class="ctx-item" @click="ctxSave">Save this place</div>
      <div class="ctx-item" @click="ctxCopy">Copy coordinates</div>
      <div v-if="state.gps" class="ctx-item" @click="clearGps">Clear GPS</div>
    </div>

    <!-- the GPS destination -->
    <div v-if="state.gps" class="gps-stack" @mousedown.stop>
    <div class="gps-chip">
      <div class="gps-chip-row">
        <span>GPS: {{ state.gps.name || fmtLatLon(state.gps.lat, state.gps.lon) }}</span>
        <span v-if="state.gps.planning && !routePts.length" class="gps-dist">planning route…</span>
        <span v-else-if="state.gps.remaining != null" class="gps-dist">{{ fmtMiles(state.gps.remaining) }}<template
          v-if="state.gps.eta != null">, {{ fmtDur(state.gps.eta) }}</template></span>
        <span v-if="state.gps.routeError && !routePts.length" class="gps-warn" :title="state.gps.routeError">no road route: heading straight there</span>
        <span v-if="state.gps.routeDownload && state.gps.routeDownload.active" class="gps-download"
              title="Downloading the route's road and elevation data ahead of time so driving it is smoother">
          <span class="gps-download-bar"><span class="gps-download-fill" :style="{ width: (state.gps.routeDownload.total
            ? (100 * state.gps.routeDownload.done / state.gps.routeDownload.total) : 0) + '%' }"></span></span>
          {{ state.gps.routeDownload.done }} / {{ state.gps.routeDownload.total }}
        </span>
        <button class="map-btn small" @click="showOnMap(state.gps)">Show</button>
        <button v-if="routePts.length >= 2" class="map-btn small" @click="askRoute(routeHalf)"
                title="Pre-cache this route: its data (elevation, roads, buildings, aerial photos) downloaded and kept, and the world built too if you like">Pre-cache route</button>
        <button class="map-btn small" :disabled="!!state.cannotGo" @click="teleportNow(state.gps.lat, state.gps.lon, state.gps.name)">Teleport</button>
        <button class="map-btn small" :class="{ primary: state.gps.autodrive }" @click="setAutodrive(!state.gps.autodrive)"
                :title="state.gps.autodrive ? 'Stop driving by itself' : 'The car drives itself along the route (in its lane, at the speed limit)'">
          {{ state.gps.autodrive ? "Autodrive on" : "Autodrive" }}
        </button>
        <button class="map-btn small" @click="clearGps">Clear</button>
      </div>
    </div>
    <!-- the route's elevation profile (server routing.resample + dem.sample, smoothed), with where the car is on it: a
         box of its own under the GPS box (the user, 2026-10-27) -->
    <div v-if="elevStats" class="elev-box" title="Elevation along the route">
      <div class="elev-title">Elevation <span class="elev-climb">&#8599; {{ fmtHeight(elevStats.up) }}
        &nbsp;&#8600; {{ fmtHeight(elevStats.down) }}</span></div>
      <div class="elev-profile">
        <svg :width="ELEV_W" :height="ELEV_H" class="elev-svg">
          <polygon :points="elevStats.area" class="elev-area" />
          <polyline :points="elevStats.line" class="elev-line" />
          <line :x1="elevStats.markerX" y1="0" :x2="elevStats.markerX" :y2="ELEV_H" class="elev-marker" />
        </svg>
        <div class="elev-minmax"><span>{{ fmtHeight(elevStats.maxE) }}</span><span>{{ fmtHeight(elevStats.minE) }}</span></div>
      </div>
    </div>
    </div>

    <!-- the confirmation -->
    <div v-if="target && (!target.quick || teleportMsg)" class="confirm" @mousedown.stop>
      <div class="confirm-title">Teleport here?</div>
      <div class="confirm-where">{{ target.name ? target.name + " - " : "" }}{{ fmtLatLon(target.lat, target.lon) }}</div>
      <div v-if="state.cannotGo" class="confirm-warn">{{ state.cannotGo }}</div>
      <div v-if="teleportMsg" class="confirm-warn">{{ teleportMsg }}</div>
      <div class="confirm-btns">
        <button class="map-btn primary" :disabled="!!state.cannotGo || busy" @click="teleport">Teleport</button>
        <button class="map-btn" @click="setGps(target.lat, target.lon, target.name); target = null">Set GPS</button>
        <button class="map-btn" @click="target = null; teleportMsg = ''">Cancel</button>
      </div>
    </div>

    <!-- a picked area: its estimate and Download (server areas.py plan) -->
    <div v-if="areaAsk" class="confirm area-confirm" @mousedown.stop @wheel.stop>
      <div class="confirm-title">{{ areaAsk.route ? "Pre-cache along this route?" : "Pre-cache this area?" }}</div>
      <div v-if="areaAsk.route" class="panel-row route-width">
        <span class="layer-hint">Width each side:</span>
        <button v-for="o in ROUTE_WIDTHS" :key="o.m" class="map-btn small" :class="{ primary: areaAsk.half === o.m }"
                :disabled="areaAsk.busy" @click="askRoute(o.m)">{{ metricUnits ? o.km : o.mi }}</button>
      </div>
      <div v-if="!areaAsk.plan" class="confirm-where">Working out what it needs...</div>
      <template v-else-if="areaAsk.plan.ok">
        <div class="confirm-where"><template v-if="areaAsk.plan.km != null">{{ fmtMiles(areaAsk.plan.km * 1000) }} of route,
          </template>{{ fmtArea(areaAsk.plan.km2) }} &middot; about {{ fmtBytes(areaAsk.plan.downloadBytes) }} to download,
          {{ fmtBytes(areaAsk.plan.diskBytes) }} on disk &middot; {{ fmtEta(areaAsk.plan) }}</div>
        <div v-for="hst in areaHosts" :key="hst[0]" class="area-host">{{ hst[0] }}: {{ hst[1].toLocaleString() }} requests<template
          v-if="hst[2] > 1"> (about {{ Math.ceil(hst[2]) }} days: its daily limit)</template></div>
        <div v-if="areaAsk.plan.excluded && areaAsk.plan.excluded.length" class="confirm-warn">Not included (these sources
          are fetched while you drive): {{ areaAsk.plan.excluded.join(", ") }}</div>
        <div v-if="areaAsk.plan.worldTiles" class="confirm-where">Builds {{ areaAsk.plan.worldTiles.toLocaleString() }} road
          tiles ahead ({{ areaAsk.plan.worldMissing.toLocaleString() }} not built yet)</div>
        <div class="area-note">It downloads in the background, slower while you drive, and the data is kept when the
          cache is cleared or full.<template v-if="!areaWorld"> The world itself is still built when you get there.</template></div>
        <div class="panel-row area-name-row">
          <input v-model="areaName" class="panel-input" @focus="typing(true)" @blur="typing(false)" placeholder="Name (optional)"
                 maxlength="60" @keydown.enter.stop.prevent="confirmArea" @keydown.esc.stop.prevent="cancelSelect" @mousedown.stop />
        </div>
      </template>
      <label class="layer-opt area-world" title="Also build the roads, sidewalks, bridges, buildings and street furniture ahead of time, so arriving there loads them instead of building them (more disk space and time; rebuilt after a BeamNG.Earth update)">
        <input type="checkbox" :checked="areaWorld" :disabled="areaAsk.busy" @change="setAreaWorld($event.target.checked)" />
        Build the world too <span class="layer-hint">(roads and buildings ready when you arrive)</span></label>
      <div v-if="areaAsk.error" class="confirm-warn">{{ areaAsk.error }}</div>
      <div class="confirm-btns">
        <button class="map-btn primary" :disabled="!areaAsk.plan || !areaAsk.plan.ok || areaAsk.busy" @click="confirmArea">Pre-cache{{ padMode ? " (X)" : "" }}</button>
        <button class="map-btn" @click="cancelSelect">Cancel{{ padMode ? " (B)" : "" }}</button>
      </div>
    </div>

    <!-- Places (moved here from the settings page, 2026-10-14): save, go, rename, delete; recent places -->
    <div class="places-panel" :class="{ open: panelOpen }" @mousedown.stop @wheel.stop @dblclick.stop>
      <button class="map-btn panel-toggle" @click="panelOpen = !panelOpen"
              :title="panelOpen ? 'Hide the places (the list and their pins on the map)' : 'Show the places'">
        {{ panelOpen ? "Places ›" : "‹ Places" }}
      </button>
      <div v-if="panelOpen" class="panel-body">
        <div v-if="state.cannotGo" class="panel-msg warn">Go is off: {{ state.cannotGo }}</div>
        <div v-if="placesMsg" class="panel-msg" :class="{ warn: placesMsgError }">{{ placesMsg }}</div>

        <div class="panel-sub">Save where you are</div>
        <div class="panel-row">
          <input v-model="newName" class="panel-input" @focus="typing(true)" @blur="typing(false)" placeholder="Name (optional)" maxlength="60"
                 @keydown.enter.stop.prevent="saveHere" @keydown.esc.stop.prevent="newName = ''" />
          <button class="map-btn primary" :disabled="!state.player" @click="saveHere">Save</button>
        </div>

        <div class="panel-sub">Latitude, longitude</div>
        <div class="panel-row">
          <input v-model="coordText" class="panel-input" @focus="typing(true)" @blur="typing(false)" placeholder="36.5785, -118.2923" maxlength="80"
                 @keydown.enter.stop.prevent="goText" @keydown.esc.stop.prevent="coordText = ''" />
          <button class="map-btn primary" :disabled="!!state.cannotGo || !coordText.trim()" @click="goText">Go</button>
          <button class="map-btn" :disabled="!coordText.trim()" @click="saveText">Save</button>
        </div>

        <div class="panel-sub">Saved places</div>
        <div v-for="p in placeList" :key="'s' + p.id" class="panel-item">
          <div v-if="renamingId === p.id" class="panel-row">
            <input v-model="renameText" class="panel-input" @focus="typing(true)" @blur="typing(false)" maxlength="60"
                   @keydown.enter.stop.prevent="commitRename(p)" @keydown.esc.stop.prevent="renamingId = null" />
            <button class="map-btn primary" @click="commitRename(p)">OK</button>
            <button class="map-btn" @click="renamingId = null">Cancel</button>
          </div>
          <template v-else>
            <div class="item-text" @click="showOnMap(p)" title="Show on the map">
              <div class="item-name">{{ p.name }}</div>
              <div class="item-sub">{{ p.text }}</div>
            </div>
            <div class="item-btns">
              <button class="map-btn primary small" :disabled="!!state.cannotGo" @click="goPlace(p)">Go</button>
              <button class="map-btn small" @click="startRename(p)">Rename</button>
              <button class="map-btn small" :class="{ danger: confirmDeleteId === p.id }" @click="deletePlace(p)">
                {{ confirmDeleteId === p.id ? "Confirm" : "Delete" }}
              </button>
            </div>
          </template>
        </div>
        <div v-if="!placeList.length" class="panel-empty">No saved places yet.</div>

        <div class="panel-sub">Pre-cached areas <span class="layer-hint">(Pre-cache area / Pre-cache route)</span></div>
        <div v-for="a in areaList" :key="'a' + a.id" class="panel-item area-item">
          <div class="item-text" @click="showArea(a)" title="Show on the map">
            <div class="item-name">{{ a.name }}<span v-if="a.route" class="layer-hint"> (route)</span><span
              v-if="a.mode === 'world'" class="layer-hint"> (world built)</span></div>
            <div class="item-sub">{{ areaStateText(a) }}</div>
            <div class="area-bar"><span class="area-bar-fill" :class="a.state" :style="{ width: areaPct(a) + '%' }"></span></div>
          </div>
          <div class="item-btns">
            <button v-if="a.state !== 'done'" class="map-btn small" @click="areaCmd(a, a.state === 'paused' ? 'resume' : 'pause')">
              {{ a.state === "paused" ? "Resume" : "Pause" }}</button>
            <button class="map-btn small" :class="{ danger: confirmAreaId === a.id }" @click="deleteArea(a)"
                    title="Stop and forget this area (its data stays in the cache, no longer kept)">
              {{ confirmAreaId === a.id ? "Confirm" : "Delete" }}</button>
          </div>
        </div>
        <div v-if="!areaList.length" class="panel-empty">Nothing pre-cached yet.</div>
        <div v-if="areaMsg" class="panel-msg" :class="{ warn: areaMsgError }">{{ areaMsg }}</div>

        <div class="panel-sub">Recent places</div>
        <div v-for="r in recentList" :key="'r' + r.index + '_' + r.lat" class="panel-item">
          <div class="item-text" @click="showOnMap(r)" title="Show on the map">
            <div class="item-name">{{ r.name || r.text }}</div>
            <div class="item-sub">{{ recentDetail(r) }}</div>
          </div>
          <div class="item-btns">
            <button class="map-btn primary small" :disabled="!!state.cannotGo" @click="goRecent(r)">Go</button>
            <button class="map-btn small" @click="saveRecent(r)">Save</button>
          </div>
        </div>
        <div v-if="!recentList.length" class="panel-empty">No recent places yet.</div>
      </div>
    </div>

    <!-- bottom: cursor position, scale, attribution -->
    <div class="map-bottom" :class="{ 'beside-panel': panelOpen }">
      <div class="bottom-left-group">
        <span class="cursor-pos">{{ cursorText }}</span>
        <div class="scale-bar-wrap" title="Map Scale">
          <div class="scale-bar" :style="{ width: scaleBar.px + 'px' }"></div>
          <span class="scale-label">{{ scaleBar.label }}</span>
        </div>
      </div>
      <!-- two lines, the whole text on hover or a click (it grew to five lines over the Places panel, 2026-10-20) -->
      <span class="attribution" :class="{ full: creditsFull }" :title="attributionText"
            @click.stop="creditsFull = !creditsFull" @mousedown.stop>{{ attributionText }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue"
import { useEvents } from "@/services/events"
import { vBngOnUiNav } from "@/common/directives"
import * as mm from "./mapmath.js"
import layerData from "./maplayers.js"
import { BngSelect } from "@/common/components/base"

const props = defineProps({ embedded: { type: Boolean, default: false } })
const embedded = props.embedded

const rootEl = ref(null), viewEl = ref(null), searchEl = ref(null)
const w = ref(1280), h = ref(720)
const view = ref({ lat: 20, lon: 0, z: 3 })
// what the view follows until the user pans: true the car, a player's pid (the Players list) that player, false nothing
const follow = ref(true)
const state = ref({ places: [], recent: [] })
const target = ref(null)              // { lat, lon, name } waiting for "Teleport here?"
const teleportMsg = ref("")
const busy = ref(false)
const query = ref("")
const results = ref([])
const searchMsg = ref("")
const cursor = ref(null)
const tileErrors = ref(0), tileLoads = ref(0)
const seenPlayer = ref(false)
let pollTimer = null, resizeObs = null
let drag = null

function engineLua(script, cb) {
  if (!window.bngApi || !window.bngApi.engineLua) return
  window.bngApi.engineLua(script, cb)
}

// ---- state from Lua (every second) -------------------------------------------------------
function poll() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiMapState and beamngEarth_settings.uiMapState() or nil", s => {
    if (!s) { state.value = { places: [], recent: [], missing: true }; return }
    state.value = { places: [], recent: [], ...s }
    if (!s.others && playersPanel.value) closePlayers()   // (out of the session: not open again by itself in the next)
    if (followingPlayer()) {
      const o = followedPlayer.value
      if (o) glideTo(o.lat, o.lon)
      else { follow.value = false; showToast("That player is no longer on the map") }
    } else if (s.player && follow.value) {
      centreOn(s.player.lat, s.player.lon, seenPlayer.value ? view.value.z : 12)
      seenPlayer.value = true
    }
  })
}
// a new view only when it moved (to half a pixel: a parked car's position still wobbles in the last digits): a parked
// car, or a player's position (sent every 2 s), would redraw the tiles, borders and labels every second for nothing
function centreOn(lat, lon, z) {
  const v = view.value
  if (v.z === z) {
    const a = mm.project(v.lat, v.lon, z), b = mm.project(lat, lon, z)
    if (Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5) return
  }
  view.value = { lat, lon, z }
}

// Tiles are files the server writes into the game's temp folder on request (the game's UI browser cancels every
// http request, even to 127.0.0.1): beamngEarth_settings.mapTiles reports which are there and asks for the rest.
// Layers (the Layers panel, remembered): the base map, "sat" (the imagery) or "map" (EOX terrain-light), and the
// "labels" overlay (EOX overlay_bright: roads and names, transparent PNG) over it.
const TILE_DIR = "/temp/beamngEarth/maptiles/"
function stored(key, def) { try { const v = localStorage.getItem(key); return v === null ? def : v } catch (e) { return def } }
function store(key, v) { try { localStorage.setItem(key, v) } catch (e) { /* storage blocked: not kept */ } }
const BASE_LAYERS = new Set(["sat", "map", "dark"])
const storedBase = stored("beamngEarth.map.base", "sat")
const baseLayer = ref(BASE_LAYERS.has(storedBase) ? storedBase : "sat")
const labelsOn = ref(stored("beamngEarth.map.labels", "0") === "1")
const roadsDrivenOn = ref(stored("beamngEarth.map.roadsDriven", "1") === "1")
watch(baseLayer, v => store("beamngEarth.map.base", v))
watch(labelsOn, on => store("beamngEarth.map.labels", on ? "1" : "0"))
watch(roadsDrivenOn, on => store("beamngEarth.map.roadsDriven", on ? "1" : "0"))
const layersPanel = ref(false)
// "Dark" is not a tile source of its own: the "map" tiles (terrain-light, already hillshaded) through a CSS
// filter (.tile-dark below) - the usual trick for a dark map without a dark-themed tile set of its own.
const tileBaseLayer = computed(() => (baseLayer.value === "dark" ? "map" : baseLayer.value))
const activeLayers = computed(() => (labelsOn.value ? [tileBaseLayer.value, "labels"] : [tileBaseLayer.value]))

const readyTiles = ref({})                // layer -> Set of "z/x/y" whose file is there
const NONE = new Set()
const readySet = layer => readyTiles.value[layer] || NONE
const tileBase = computed(() => (state.value.mapPort ? TILE_DIR : null))
const wanted = computed(() => {
  if (!tileBase.value) return []
  const v = view.value
  return mm.visibleTiles(v.lat, v.lon, v.z, w.value, h.value)
})
// the missing tiles, nearest the centre first (each tile's base, then its labels)
function missingTiles() {
  const out = []
  const layers = activeLayers.value
  for (const t of mm.centreFirst(wanted.value, w.value, h.value)) {
    for (const L of layers) if (!readySet(L).has(`${t.z}/${t.x}/${t.y}`)) out.push([t.z, t.x, t.y, L])
  }
  return out
}
// Asked every 250 ms while tiles are missing (the server says each one as soon as it is written), else every second;
// the whole missing list each time: Lua passes it on when it changed (the server builds the newest list first)
const ASK_MAX = 128                       // server maptiles.MAX_FILES_REQUEST
let askTimer = null, askAt = 0
function scheduleAsk(ms) {
  const at = Date.now() + ms
  if (askTimer && askAt <= at) return     // one is due sooner (a drag must not keep pushing it back)
  clearTimeout(askTimer)
  askAt = at
  askTimer = setTimeout(askTiles, ms)
}
function askTiles() {
  askTimer = null
  const missing = tileBase.value ? missingTiles() : []
  scheduleAsk(missing.length ? 250 : 1000)
  if (!tileBase.value) return
  const list = JSON.stringify(missing.slice(0, ASK_MAX))
  engineLua(`beamngEarth_settings and beamngEarth_settings.mapTiles and beamngEarth_settings.mapTiles(jsonDecode('${list}')) or {}`, r => {
    if (!Array.isArray(r) || !r.length) return
    let next = null
    for (const k of r) {
      const i = k.indexOf("/")
      const L = k.slice(0, i), zxy = k.slice(i + 1)
      if (((next && next[L]) || readySet(L)).has(zxy)) continue
      if (!next) next = { ...readyTiles.value }
      if (!next[L] || next[L] === readyTiles.value[L]) next[L] = new Set(next[L] || [])
      next[L].add(zxy)
    }
    if (next) readyTiles.value = next
  })
}
watch([wanted, activeLayers], () => scheduleAsk(120))
scheduleAsk(0)
onUnmounted(() => { clearTimeout(askTimer); askTimer = null; clearTimeout(searchTimer); typing(false) })

const tileLayers = computed(() => activeLayers.value.map(L => {
  const ready = readySet(L)
  return {
    id: L, overlay: L === "labels",
    tiles: wanted.value.filter(t => ready.has(`${t.z}/${t.x}/${t.y}`)).map(t => ({ ...t, src: mm.tilePath(TILE_DIR, L, t.z, t.x, t.y) })),
    // loaded coarser tiles stretched over the ones still coming (mapmath.fallbackTiles)
    fallback: mm.fallbackTiles(wanted.value, ready).map(t => ({ ...t, src: mm.tilePath(TILE_DIR, L, t.z, t.x, t.y) })),
  }
}))

function onTileLoad() { tileLoads.value++ }
function onTileError(ev, t) {
  tileErrors.value++
  if (ev && ev.target) ev.target.style.visibility = "hidden"   // grey background shows; grid stays
}

const notice = computed(() => {
  const s = state.value
  if (s.missing) return "BeamNG.Earth is not loaded"
  if (!s.connected) return "Companion server not connected: map tiles unavailable (places and the grid still work)"
  if (!s.mapPort) return "Map tiles unavailable (the companion server is older or has --map-tiles off)"
  if (tileErrors.value > 8 && tileLoads.value === 0) return "Map tiles unavailable (no imagery from the server: offline?)"
  return ""
})

// the player's local time + weather, next to the title (state.weather: settings.uiMapState)
const weatherBadge = computed(() => {
  const w = state.value.weather
  if (!w || (!w.localTime && w.code == null)) return null
  // (in the game's unit, as the GPS distances: gps.lua's metric, Options > Display)
  const metric = !!w.metric
  const temp = w.tempC == null ? null : (metric ? `${Math.round(w.tempC)}°C` : `${Math.round(w.tempC * 9 / 5 + 32)}°F`)
  return { icon: w.code != null ? mm.weatherIcon(w.code) : "", localTime: w.localTime || "", temp }
})

const creditsFull = ref(false)          // the credits' whole text (else two lines)
const attributionText = computed(() => {
  if (!state.value.mapPort) return ""
  const sat = baseLayer.value === "sat"
  const parts = [mm.layerCredits(tileBaseLayer.value, labelsOn.value)]   // the EOX map / labels layers shown
  const cba = state.value.creditsByArea
  if (cba) {                                // only the imagery sources the view shows (the server's areas)
    const v = view.value
    const list = sat ? toArray(cba.imagery).map(c => (Array.isArray(c) ? c : toArray(c))) : []
    parts.push(mm.creditsFor(list, cba.other || "", mm.viewBox(v.lat, v.lon, v.z, w.value, h.value, 0)))
  } else if (sat) {
    const a = state.value.attribution || ""
    parts.push(a.startsWith("Imagery") ? "Map " + a.charAt(0).toLowerCase() + a.slice(1) : a)
  }
  return parts.filter(Boolean).join("; ")
})

// ---- markers ------------------------------------------------------------------------------
function screen(lat, lon) {
  const v = view.value
  return mm.toScreen(lat, lon, v.lat, v.lon, v.z, w.value, h.value)
}
const onScreen = p => p.x > -50 && p.y > -50 && p.x < w.value + 50 && p.y < h.value + 50

const placeMarkers = computed(() =>
  (state.value.places || []).map(p => ({ ...p, ...(() => { const s = screen(p.lat, p.lon); return { sx: s.x, sy: s.y } })() }))
    .filter(p => onScreen({ x: p.sx, y: p.sy })))

// state.others: mpsession.others() ({ pid, name, lat, lon, h }), nil outside a BeamMP session. An older mod's Lua (a
// player's own copy, mods/unpacked: the server's zip does not replace the Lua it already runs) has no pid: their name
const otherList = computed(() => toArray(state.value.others).map(o => (o.pid != null ? o : { ...o, pid: "name:" + o.name })))
// (a followed player at the centre: the view is on them or gliding there, glideTo; drawn at their latest position the
// arrow jumped ahead at each update and the map slid after it)
const otherMarkers = computed(() =>
  otherList.value.map(o => {
    const s = o.pid === follow.value ? { x: w.value / 2, y: h.value / 2 } : screen(o.lat, o.lon)
    return { ...o, sx: s.x, sy: s.y }
  }).filter(o => onScreen({ x: o.sx, y: o.sy })))

const playerScreen = computed(() => {
  const p = state.value.player
  return p ? screen(p.lat, p.lon) : null
})
const targetScreen = computed(() => (target.value ? screen(target.value.lat, target.value.lon) : { x: 0, y: 0 }))

// ---- layers: country / state borders and place names (maplayers.js, Natural Earth) ------------------------------
const layersOn = ref(stored("beamngEarthMapLayers", "1") !== "0")
watch(layersOn, on => store("beamngEarthMapLayers", on ? "1" : "0"))
// in normalised Mercator once (mapmath.normLines): drawing is a multiply-add per vertex, not a log and a tan
const borderBoxes = mm.lineBoxes(layerData.borders), borderNorm = mm.normLines(layerData.borders)
const stateBoxes = mm.lineBoxes(layerData.states), stateNorm = mm.normLines(layerData.states)
const countryPoints = layerData.countries.map(c => [c[0], c[1], c[2], Math.max(1.5, c[3] - 0.5)])
  .sort((a, b) => a[3] - b[3])
const countryNorm = mm.normPoints(countryPoints)
const placePoints = layerData.places.map(p => [p[0], p[1], p[2], mm.placeMinZoom(p[3], p[4]), p[4]])
const placeNorm = mm.normPoints(placePoints)
// The border lines are drawn for an anchor view with `pad` px round it, and moved (an SVG translate) while the view
// pans less than that at the same zoom: a drag no longer redraws hundreds of polylines every frame. Only where the
// world's wrap-around seam stays off screen (the world wider than the padded view), so the moved lines are the
// ones a redraw would give (to 0.05 px).
const lineAnchor = ref(null)              // { lat, lon, z, w, h, pad }
const lineShift = ref({ x: 0, y: 0 })
function placeLines() {
  const v = view.value, W = w.value, H = h.value
  const pad = Math.round(Math.max(W, H) / 2)
  const s = mm.TILE * Math.pow(2, v.z)
  const shiftable = s > W + 2 * pad + 40
  const a = lineAnchor.value
  if (shiftable && a && a.pad === pad && a.z === v.z && a.w === W && a.h === H) {
    const pa = mm.project(a.lat, a.lon, v.z), pc = mm.project(v.lat, v.lon, v.z)
    let dx = pa.x - pc.x
    if (dx > s / 2) dx -= s
    if (dx < -s / 2) dx += s
    const dy = pa.y - pc.y
    if (Math.abs(dx) <= pad && Math.abs(dy) <= pad) { lineShift.value = { x: dx - pad, y: dy - pad }; return }
  }
  const p = shiftable ? pad : 0
  lineAnchor.value = { lat: v.lat, lon: v.lon, z: v.z, w: W, h: H, pad: p }
  lineShift.value = { x: -p, y: -p }
}
watch([view, w, h], placeLines, { immediate: true })
const lineTransform = computed(() => `translate(${lineShift.value.x.toFixed(2)},${lineShift.value.y.toFixed(2)})`)
function anchoredLines(lines, boxes, norm) {
  const a = lineAnchor.value
  return mm.visibleLines(lines, boxes, a.lat, a.lon, a.z, a.w + 2 * a.pad, a.h + 2 * a.pad, 3000, norm)
}
const borderLines = computed(() => {
  const a = lineAnchor.value
  return layersOn.value && a && a.z <= 13 ? anchoredLines(layerData.borders, borderBoxes, borderNorm) : []
})
const stateLines = computed(() => {
  const a = lineAnchor.value
  return layersOn.value && a && a.z >= 4 && a.z <= 13 ? anchoredLines(layerData.states, stateBoxes, stateNorm) : []
})
const countryLabels = computed(() => {
  const v = view.value
  return layersOn.value ? mm.visibleLabels(countryPoints, v.lat, v.lon, v.z, w.value, h.value, { maxZoom: 6.5, charW: 8, norm: countryNorm }) : []
})
const placeLabels = computed(() => {
  const v = view.value
  // no z < 15 cap (the user, 2026-10-22: zoomed in past it, every place's name should stay, not just vanish):
  // placeMinZoom's highest value is ~9.7 (scalerank 8, not a capital), so nothing new appears past z ~10 anyway
  return layersOn.value ? mm.visibleLabels(placePoints, v.lat, v.lon, v.z, w.value, h.value, { max: 80, norm: placeNorm }) : []
})

// the lat/lon grid: a step that gives lines ~120-300 px apart
const grid = computed(() => {
  const v = view.value
  const pxPerDeg = 256 * Math.pow(2, v.z) / 360
  const steps = [30, 10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001]
  const step = steps.find(s => s * pxPerDeg < 300) || 0.001
  const tl = mm.fromScreen(0, 0, v.lat, v.lon, v.z, w.value, h.value)
  const br = mm.fromScreen(w.value, h.value, v.lat, v.lon, v.z, w.value, h.value)
  const out = []
  const dec = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step)))
  let lon0 = tl.lon, lon1 = br.lon
  if (lon1 < lon0) lon1 += 360
  for (let k = Math.ceil(lon0 / step); k * step <= lon1 && out.length < 200; k++) {
    const lon = mm.wrapLon(k * step)
    const x = screen(v.lat, lon).x
    out.push({ key: "x" + k, x1: x, y1: 0, x2: x, y2: h.value, cls: Math.abs(lon) < 1e-9 ? "meridian" : "",
      label: `${Math.abs(lon).toFixed(dec)}°${lon < 0 ? "W" : lon > 0 ? "E" : ""}`, lx: x + 4, ly: 74 })   // under the top bar (the bottom has the game's footer)
  }
  for (let k = Math.ceil(br.lat / step); k * step <= tl.lat && out.length < 400; k++) {
    const lat = k * step
    const y = screen(lat, v.lon).y
    out.push({ key: "y" + k, x1: 0, y1: y, x2: w.value, y2: y, cls: Math.abs(lat) < 1e-9 ? "meridian" : "",
      label: `${Math.abs(lat).toFixed(dec)}°${lat < 0 ? "S" : lat > 0 ? "N" : ""}`, lx: 6, ly: y - 4 })
  }
  return out
})

const cursorText = computed(() => {
  const c = cursor.value
  const z = `zoom ${view.value.z.toFixed(1)}`
  return c ? `${mm.fmtLatLon(c.lat, c.lon)}   ${z}` : z
})

// Roads Driven polylines (Issue #365): transformed into screen SVG polyline coords
const drivenLines = computed(() => {
  view.value; w.value; h.value
  if (!roadsDrivenOn.value) return []
  const polylines = state.value.roadsDriven?.polylines || []
  const out = []
  for (const line of polylines) {
    if (!line || line.length < 2) continue
    const pts = []
    for (const pt of line) {
      const lat = Number(pt[0]), lon = Number(pt[1])
      if (!isFinite(lat) || !isFinite(lon)) continue
      const p = screen(lat, lon)
      pts.push(`${p.x.toFixed(1)},${p.y.toFixed(1)}`)
    }
    if (pts.length >= 2) out.push(pts.join(" "))
  }
  return out
})

// Dynamic Google Maps style scale bar: calculates real-world distance corresponding to ~100px
const scaleBar = computed(() => {
  const v = view.value
  const latRad = (v.lat * Math.PI) / 180
  const mPerPx = (156543.03392 * Math.cos(latRad)) / Math.pow(2, v.z)
  const targetM = 100 * mPerPx
  const steps = [
    5, 10, 20, 50, 100, 200, 500,
    1000, 2000, 5000, 10000, 20000, 50000,
    100000, 200000, 500000, 1000000, 2000000
  ]
  const dist = steps.find(s => s >= targetM * 0.75) || steps[steps.length - 1]
  const px = Math.min(180, Math.max(25, Math.round(dist / mPerPx)))
  const label = dist >= 1000 ? `${dist / 1000} km` : `${dist} m`
  return { px, label }
})

// ---- mouse ---------------------------------------------------------------------------------
function localXY(ev) {
  const r = viewEl.value.getBoundingClientRect()
  return { x: ev.clientX - r.left, y: ev.clientY - r.top }
}

function onDown(ev) {
  if (ev.button !== 0) return
  layersPanel.value = false
  // (a click beside the Players list only closes it, as a menu: not a GPS route there or a drag)
  if (playersPanel.value) { closePlayers(); return }
  if (ctx.value) { closeCtx(); return }
  results.value = []
  if (selecting.value || ev.shiftKey) { startBox(ev); return }
  const p = localXY(ev)
  drag = { x: p.x, y: p.y, sx: p.x, sy: p.y, moved: false }
  window.addEventListener("mousemove", onMove)
  window.addEventListener("mouseup", onUp)
}

function onHover(ev) {
  if (!viewEl.value) return
  const p = localXY(ev)
  const v = view.value
  cursor.value = mm.fromScreen(p.x, p.y, v.lat, v.lon, v.z, w.value, h.value)
}

// a drag moves the view once per animation frame (the mouse moves more often, and each move redrew the whole map)
let panDx = 0, panDy = 0, panFrame = 0
const nextFrame = window.requestAnimationFrame ? f => window.requestAnimationFrame(f) : f => setTimeout(f, 16)
function applyPan() {
  panFrame = 0
  if (!panDx && !panDy) return
  const v = view.value
  const c = mm.pan(v.lat, v.lon, v.z, panDx, panDy)
  panDx = 0; panDy = 0
  view.value = { ...v, lat: c.lat, lon: c.lon }
}

function onMove(ev) {
  if (!drag) return
  const p = localXY(ev)
  const dx = p.x - drag.x, dy = p.y - drag.y
  if (!drag.moved && Math.hypot(p.x - drag.sx, p.y - drag.sy) < 4) return
  drag.moved = true
  follow.value = false
  panDx += dx; panDy += dy
  drag.x = p.x; drag.y = p.y
  if (!panFrame) panFrame = nextFrame(applyPan) || 1
}

function onUp(ev) {
  window.removeEventListener("mousemove", onMove)
  window.removeEventListener("mouseup", onUp)
  applyPan()
  const d = drag
  drag = null
  if (d && !d.moved) {
    // a click sets the GPS route there (the user, 2026-10-14); delayed so a double-click only zooms
    const p = localXY(ev)
    const v = view.value
    const ll = mm.fromScreen(p.x, p.y, v.lat, v.lon, v.z, w.value, h.value)
    clearTimeout(clickTimer)
    clickTimer = setTimeout(() => setGps(ll.lat, ll.lon, ""), 260)
  }
}

let clickTimer = null

function onWheel(ev) {
  const p = localXY(ev)
  const v = view.value
  const z2 = v.z + (ev.deltaY < 0 ? 0.5 : -0.5)
  // following a player: zoomed round them (the centre), still following (a pan stops it; the user, 2026-10-28: "it
  // actually follows the player icon, until you move the map around manually")
  if (followingPlayer()) { view.value = mm.zoomAt(v.lat, v.lon, v.z, z2, w.value / 2, h.value / 2, w.value, h.value); return }
  const r = mm.zoomAt(v.lat, v.lon, v.z, z2, p.x, p.y, w.value, h.value)
  if (Math.abs(p.x - w.value / 2) > 2 || Math.abs(p.y - h.value / 2) > 2) follow.value = false
  view.value = r
}

function onDblClick(ev) {
  clearTimeout(clickTimer)
  // (the second click of a double-click on a player's name, which the first click's Follow moved: no zoom, still on them)
  if (Date.now() - followedAt < 500) return
  const p = localXY(ev)
  const v = view.value
  if (followingPlayer()) { view.value = mm.zoomAt(v.lat, v.lon, v.z, v.z + 1, w.value / 2, h.value / 2, w.value, h.value); return }
  view.value = mm.zoomAt(v.lat, v.lon, v.z, v.z + 1, p.x, p.y, w.value, h.value)
  follow.value = false
}

function zoomBy(d) {
  const v = view.value
  view.value = { ...v, z: Math.max(mm.MIN_Z, Math.min(mm.MAX_Z, Math.round(v.z + d))) }
}

function centreOnMe() {
  follow.value = true
  const p = state.value.player
  if (p) view.value = { ...view.value, lat: p.lat, lon: p.lon, z: Math.max(view.value.z, 12) }
}

// ---- the Players list (a BeamMP session) ---------------------------------------------------
const playersPanel = ref(false)
// nearest first (Lua's order, by name, without our position); only worked out while the list is open (nothing
// else reads it)
const playerRows = computed(() => {
  const me = state.value.player
  const rows = otherList.value.map(o => ({ ...o, dist: me ? mm.distanceM(me.lat, me.lon, o.lat, o.lon) : null }))
  return me ? rows.sort((a, b) => a.dist - b.dist) : rows
})
// a focused button of the list goes with it: the focus back on the map (its keys: Esc)
function closePlayers() {
  const el = document.activeElement
  const lost = !el || el === document.body || !!(el.closest && el.closest(".players-panel"))
  playersPanel.value = false
  if (lost && rootEl.value) rootEl.value.focus({ preventScroll: true })
}
// the map's view stays on them as they move (poll, glideTo), until a pan (a zoom keeps it: onWheel, onDblClick, the
// pad's triggers); the car stays where it is
let followedAt = 0
const followingPlayer = () => typeof follow.value === "string"
const followedPlayer = computed(() => (followingPlayer() ? otherList.value.find(p => p.pid === follow.value) || null : null))
function followPlayer(o) {
  closePlayers()
  // (Following again: stop, the view left where it is; not the second click of a double-click on their name)
  if (follow.value === o.pid) { if (Date.now() - followedAt > 500) follow.value = false; return }
  followedAt = Date.now()
  follow.value = o.pid
  glide = null
  view.value = { lat: o.lat, lon: o.lon, z: Math.max(view.value.z, 12) }
}
// Their positions come every 2 s (mpsession.POSE_S, the plugin's TICK_MS): the view glides to each, at an even speed
// over that time, so a moving player (drawn at the centre: otherMarkers) moves on smoothly rather than in jumps; a jump
// farther than the view (a teleport) is shown at once. Each glide frame is a pan's work, only while following a player
// who moves.
const GLIDE_MS = 2000
let glide = null, glideFrame = 0
function glideTo(lat, lon) {
  // (the poll is every second, a new position every 2 s: the same one again keeps its glide, else it never got there)
  if (glide && glide.lat === lat && glide.lon === lon) return
  const v = view.value, s = screen(lat, lon)
  const dx = s.x - w.value / 2, dy = s.y - h.value / 2
  if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return
  if (Math.abs(dx) > w.value || Math.abs(dy) > h.value) { glide = null; view.value = { lat, lon, z: v.z }; return }
  glide = { lat, lon, lat0: v.lat, lon0: v.lon, dLat: lat - v.lat, dLon: mm.wrapLon(lon - v.lon), t0: performance.now() }
  if (!glideFrame) glideFrame = nextFrame(glideStep) || 1
}
function glideStep(now) {
  glideFrame = 0
  const g = glide
  if (!g || !followingPlayer()) { glide = null; return }      // (a pan, Me, a search: the glide stops with the follow)
  // (a frame's time can be from just before glideTo: not backwards)
  const t = Math.max(0, Math.min(1, ((typeof now === "number" ? now : performance.now()) - g.t0) / GLIDE_MS))
  view.value = { lat: g.lat0 + g.dLat * t, lon: mm.wrapLon(g.lon0 + g.dLon * t), z: view.value.z }
  if (t < 1) glideFrame = nextFrame(glideStep) || 1
  else glide = null
}
// to where they are now (the route does not follow them)
function gpsToPlayer(o) {
  closePlayers()
  setGps(o.lat, o.lon, o.name)
}
// everyone, us included, in the part of the map the Places panel leaves free (as .map-bottom.beside-panel)
function showAllPlayers() {
  const pts = state.value.player ? [state.value.player, ...otherList.value] : otherList.value
  if (!pts.length) return
  closePlayers()
  follow.value = false
  const right = panelOpen.value ? 376 : 0
  const v = mm.fitView(pts, w.value - right, h.value, 80)
  view.value = { ...mm.pan(v.lat, v.lon, v.z, -right / 2, 0), z: v.z }
}

// ---- controller (settings.lua mapControls: the stock big map's actions and bindings while this map is open) ---------
// The stock map's layout (the user, 2026-10-21: "copy the stock map controlls so players can use their muscle memory"):
// its pan (bigMapMove*: the right stick on a pad) moves the view (squared: fine near the centre), the triggers zoom,
// the left stick and d-pad walk the buttons as in any menu; A on the map sets the GPS route to the centre crosshair
// and again clears it (as the stock map's set / clear route), X teleports (the stock map's quick travel), Y starts or
// stops the autodrive, B goes back. A on a focused button is the button's.
const padMode = ref(false)
const PAD_PAN_PX_S = 900          // px a second at a full stick
const PAD_ZOOM_S = 2.5            // zoom levels a second at a full trigger
const PAD_DEAD = 0.08
const pad = { lr: 0, ud: 0, zin: 0, zout: 0, zaxis: 0, keys: [0, 0, 0, 0] }
let padFrame = 0, padLast = 0

function padActive() {
  return Math.abs(pad.lr) > PAD_DEAD || Math.abs(pad.ud) > PAD_DEAD || pad.zin > PAD_DEAD || pad.zout > PAD_DEAD ||
    Math.abs(pad.zaxis) > PAD_DEAD || pad.keys.some(k => k > 0)
}

function padTick(ts) {
  padFrame = 0
  const dt = padLast ? Math.min(0.1, (ts - padLast) / 1000) : 1 / 60
  padLast = ts
  const sq = v => (Math.abs(v) > PAD_DEAD ? Math.sign(v) * v * v : 0)
  const kx = pad.keys[3] - pad.keys[2], ky = pad.keys[0] - pad.keys[1]       // (camMoveKey: 1 fwd, 2 back, 3 left, 4 right)
  const sx = sq(pad.lr) + kx, sy = sq(pad.ud) + ky
  const dz = (pad.zin > PAD_DEAD ? pad.zin : 0) - (pad.zout > PAD_DEAD ? pad.zout : 0) + sq(pad.zaxis)
  if (sx || sy || dz) {
    if (sx || sy || !followingPlayer()) follow.value = false     // (the triggers' zoom: still on a followed player)
    const v = view.value
    let nv = v
    if (sx || sy) {
      const c = mm.pan(v.lat, v.lon, v.z, -sx * PAD_PAN_PX_S * dt, sy * PAD_PAN_PX_S * dt)
      nv = { ...nv, lat: c.lat, lon: c.lon }
    }
    if (dz) nv = { ...nv, z: Math.max(mm.MIN_Z, Math.min(mm.MAX_Z, nv.z + dz * PAD_ZOOM_S * dt)) }
    view.value = nv
    if (selecting.value && boxStart.value && !areaAsk.value) boxEnd.value = { lat: nv.lat, lon: nv.lon }
  }
  if (padActive()) padFrame = nextFrame(padTick) || 1
  else padLast = 0
}

// moving the map takes the controls back from the panels: the selected button lets go (the user, 2026-10-21: "if you
// move the map refocus the controls on it and deselect any UI elements")
function releaseButtons() {
  const root = rootEl.value
  if (!root) return
  root.querySelectorAll(".focus-visible").forEach(el => { el.classList.remove("focus-visible"); if (el.blur) el.blur() })
  if (document.activeElement && root.contains(document.activeElement) && document.activeElement !== root) document.activeElement.blur()
  root.focus({ preventScroll: true })
}

function padStart() {
  padMode.value = true
  if (padActive()) releaseButtons()
  if (!padFrame && padActive()) padFrame = nextFrame(padTick) || 1
}

// the d-pad / left stick walk the buttons: the selected one scrolled into its panel's view (the saved places below
// the panel's edge could not be reached, the user 2026-10-21)
function showSelected() {
  nextTick(() => {
    const el = rootEl.value && rootEl.value.querySelector(".focus-visible")
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "nearest" })
  })
}
const FOCUS_MOVES = new Set(["focus_u", "focus_d", "focus_l", "focus_r", "focus_ud", "focus_lr"])

function buttonFocused() {
  return !!(rootEl.value && rootEl.value.querySelector(".focus-visible"))
}

function padSelect() {
  // A on a focused button is the button's (the menu's own 'ok')
  if (buttonFocused()) return
  if (selecting.value && !areaAsk.value) {          // picking an area: the crosshair is a corner
    padMode.value = true
    const v = view.value
    if (!boxStart.value) { boxStart.value = boxEnd.value = { lat: v.lat, lon: v.lon }; return }
    boxEnd.value = { lat: v.lat, lon: v.lon }
    askArea()
    return
  }
  if (!padMode.value) { padMode.value = true; return }      // the first A shows the crosshair
  if (state.value.gps) { clearGps(); return }                // again: clears it (the stock map's set / clear route)
  const v = view.value
  setGps(v.lat, v.lon, "")
}

// X: teleport to the GPS destination, else to the crosshair (the stock map's quick travel)
function padTeleport() {
  if (selecting.value) { if (areaAsk.value) confirmArea(); return }      // X: download the picked area
  if (state.value.cannotGo || busy.value) return
  const g = state.value.gps
  if (g) teleportNow(g.lat, g.lon, g.name)
  else if (padMode.value) teleportNow(view.value.lat, view.value.lon, "")
}

// Y: the autodrive on or off; with no destination, to the crosshair
function padAutodrive() {
  if (selecting.value) return
  const g = state.value.gps
  if (g) setAutodrive(!g.autodrive)
  else if (padMode.value) setGps(view.value.lat, view.value.lon, "", () => setAutodrive(true))
}

// X and Y: as the menus get them (core/input/actions/menu.json: guihooks 'UINavigation' name, value; X is action_2,
// Y context), or from the vehicle triggers they are also bound to (settings.lua mapControls: pad_x / pad_y), which
// take the press first on stock pads. A press is 1; one press may come both ways: once within PAD_BUTTON_MS
const PAD_BUTTONS = { action_2: padTeleport, context: padAutodrive, pad_x: padTeleport, pad_y: padAutodrive }
const PAD_BUTTON_MS = 300
const padPressedAt = new Map()
function padButton(name, value) {
  const fn = PAD_BUTTONS[name]
  if (embedded || !fn || Number(value) !== 1) return
  const now = Date.now()
  if (now - (padPressedAt.get(fn) || 0) < PAD_BUTTON_MS) return
  padPressedAt.set(fn, now)
  fn()
}

const mapEvents = useEvents()
mapEvents.on("BeamNGEarthMapInput", d => {
  if (!d) return
  const a = Number(d.a) || 0
  switch (d.kind) {
    case "move_lr": pad.lr = a; padStart(); break
    case "move_ud": pad.ud = a; padStart(); break
    case "zoom_trigger": if (d.b) pad.zin = Math.abs(a) * 2; else pad.zout = Math.abs(a) * 2; padStart(); break
    case "zoom_axis": pad.zaxis = -a * 2; padStart(); break
    case "move_key": if (d.b >= 1 && d.b <= 4) pad.keys[d.b - 1] = a; if (a) padStart(); break
    case "select": padSelect(); break
    case "pad_x": case "pad_y": padButton(d.kind, a); break
    case "filter": if (a) cycleCache(Number(d.b) < 0 || a < 0 ? -1 : 1); break   // the bumpers (bigMap's onChangeUiFilter)
  }
})
mapEvents.on("UINavigation", (name, value) => {
  if (FOCUS_MOVES.has(name)) { if (Number(value)) showSelected(); return }
  if (selecting.value && (name === "back" || name === "cancel" || name === "menu_back") && Number(value)) { cancelSelect(); return }
  padButton(name, value)
})
// declared to the menus (v-bng-on-ui-nav on the root) so the UI keeps X / Y on while the map is open; the presses
// themselves are taken from 'UINavigation' above (padButton: once a press)
function onNavDeclared() { return false }
// the mouse takes over again: no crosshair
watch(cursor, () => { if (padMode.value && !padActive()) padMode.value = false })


// ---- the cache view (server covermap.py; settings.uiCacheMap): what is downloaded where, and at what detail -----------
// A grid of levels for the view (8 cells a map tile: mapmath.cacheGridZoom), asked 300 ms after the view settles and
// every 5 s while it is shown, drawn on a canvas under the grid; moved with the view in between. Remembered.
const cacheEl = ref(null)
const cacheOn = ref(stored("beamngEarth.map.cache", "0") === "1")
const storedCacheMode = stored("beamngEarth.map.cacheMode", "summary")
const cacheMode = ref(mm.CACHE_MODES.includes(storedCacheMode) ? storedCacheMode : "summary")
watch(cacheOn, on => store("beamngEarth.map.cache", on ? "1" : "0"))
watch(cacheMode, v => store("beamngEarth.map.cacheMode", v))
const cacheModeOptions = mm.CACHE_MODES.map(v => ({ label: mm.CACHE_MODE_NAMES[v], value: v }))
const cacheSelectConfig = { value: o => o.value, label: o => o.label }
const cacheGrid = ref(null)               // { z, x0, y0, w, h, cells: Uint8Array, mode }
const cacheMsg = ref("")
const CACHE_ASK_MS = 300, CACHE_REFRESH_MS = 5000, CACHE_POLL_MS = 100, CACHE_POLL_TRIES = 50
let cacheTimer = null, cachePollTimer = null, cacheSeq = 0
function scheduleCache(ms) {
  clearTimeout(cacheTimer)
  cacheTimer = setTimeout(askCache, ms)
}
function askCache() {
  cacheTimer = null
  if (!cacheOn.value) return
  const v = view.value
  const b = mm.cacheGridBox(v.lat, v.lon, v.z, w.value, h.value, mm.cacheGridZoom(v.z))
  const mode = cacheMode.value
  const seq = ++cacheSeq
  clearTimeout(cachePollTimer)
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiCacheMap and beamngEarth_settings.uiCacheMap(${b.z}, ${b.x0}, ${b.y0}, ${b.x1}, ${b.y1}, ${luaStr(mode)}) or nil`, r => {
    if (seq !== cacheSeq) return
    if (!r || r.error || !r.id) {
      cacheMsg.value = (r && r.error) || "Cache view unavailable (BeamNG.Earth not loaded?)"
      scheduleCache(CACHE_REFRESH_MS)
      return
    }
    let tries = 0
    const poll = () => {
      if (seq !== cacheSeq) return
      engineLua(`beamngEarth_settings and beamngEarth_settings.uiCacheMapResult(${r.id}) or nil`, res => {
        if (seq !== cacheSeq) return
        if (!res) {
          if (++tries < CACHE_POLL_TRIES) cachePollTimer = setTimeout(poll, CACHE_POLL_MS)
          else { cacheMsg.value = "No answer from the companion server"; scheduleCache(CACHE_REFRESH_MS) }
          return
        }
        const waiting = res.ready === false
        cacheMsg.value = res.error ? String(res.error) : waiting ? "Indexing the cache..." : ""
        if (!res.error && !waiting) {
          cacheGrid.value = { z: res.z, x0: res.x0, y0: res.y0, w: res.w, h: res.h, cells: mm.b64Bytes(res.cells), mode }
        }
        scheduleCache(waiting ? 1000 : CACHE_REFRESH_MS)
      })
    }
    cachePollTimer = setTimeout(poll, CACHE_POLL_MS / 2)
  })
}
watch([view, w, h], () => { if (cacheOn.value) scheduleCache(CACHE_ASK_MS) })
watch([cacheOn, cacheMode], () => {
  if (cacheGrid.value && cacheGrid.value.mode !== cacheMode.value) cacheGrid.value = null
  if (cacheOn.value) scheduleCache(0)
  else { cacheSeq++; clearTimeout(cacheTimer); clearTimeout(cachePollTimer); cacheMsg.value = "" }
}, { immediate: true })
onUnmounted(() => { cacheSeq++; clearTimeout(cacheTimer); clearTimeout(cachePollTimer) })

// the bumpers: off -> each view in turn -> off
function cycleCache(dir) {
  const list = mm.CACHE_MODES
  let i = cacheOn.value ? list.indexOf(cacheMode.value) + dir : (dir > 0 ? 0 : list.length - 1)
  if (i < 0 || i >= list.length) { cacheOn.value = false; showToast("Cache view off"); return }
  cacheMode.value = list[i]
  cacheOn.value = true
  showToast(`Cache: ${mm.CACHE_MODE_NAMES[list[i]]}`)
}

const CACHE_ALPHA = 0.45, CACHE_ALPHA_PARTIAL = 0.2
const cacheCss = (rgb, a) => `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${a})`
const cacheMetric = computed(() => !!((state.value.weather && state.value.weather.metric) || (state.value.gps && state.value.gps.metric)))
const cacheLegendRows = computed(() => {
  const mode = cacheMode.value
  const detail = mode === "elevation" || mode === "imagery"
  const rows = mm.cacheLegend(mode).map(c => ({ ...c, css: cacheCss(c.rgb, CACHE_ALPHA + 0.2), note: "" }))
  if (detail) {
    const f = m => mm.fmtRes(m, cacheMetric.value)
    for (let i = 0; i < rows.length; i++) {
      const lo = i + 1 < rows.length ? rows[i + 1].max : 0
      rows[i].note = rows[i].max === Infinity ? `(over ${f(lo)})` : lo ? `(${f(lo)} - ${f(rows[i].max)})` : `(under ${f(rows[i].max)})`
    }
    rows.reverse()
  }
  return rows
})

function drawCache() {
  const cv = cacheEl.value
  if (!cv) return
  const ctx = cv.getContext("2d")
  if (!ctx) return
  ctx.clearRect(0, 0, cv.width, cv.height)
  const g = cacheGrid.value
  if (!cacheOn.value || !g || !g.cells || g.cells.length < g.w * g.h) return
  const v = view.value, W = w.value, H = h.value
  const c = mm.project(v.lat, v.lon, v.z)
  const s = mm.TILE * Math.pow(2, v.z - g.z)          // px a cell
  const left0 = W / 2 - c.x, top0 = H / 2 - c.y
  for (let j = 0; j < g.h; j++) {
    const y = g.y0 + j
    const ya = Math.round(top0 + y * s), yb = Math.round(top0 + (y + 1) * s)
    if (yb < 0 || ya > H) continue
    const lat = mm.unproject(0, (y + 0.5) * mm.TILE, g.z).lat
    for (let i = 0; i < g.w; i++) {
      const lv = g.cells[j * g.w + i]
      if (!lv) continue
      const x = g.x0 + i
      const xa = Math.round(left0 + x * s), xb = Math.round(left0 + (x + 1) * s)
      if (xb < 0 || xa > W) continue
      const k = mm.cacheClass(g.mode, lv, lat)
      if (!k) continue
      ctx.fillStyle = cacheCss(k.rgb, k.partial ? CACHE_ALPHA_PARTIAL : CACHE_ALPHA)
      ctx.fillRect(xa, ya, Math.max(1, xb - xa), Math.max(1, yb - ya))
    }
  }
}
let cacheFrame = 0
watch([cacheGrid, view, w, h, cacheOn], () => {
  if (!cacheFrame) cacheFrame = nextFrame(() => { cacheFrame = 0; drawCache() }) || 1
})

// ---- saved areas (server areas.py; settings.uiArea*): pick a box, see its estimate, download it ahead -------------
// The Area button (or Shift + drag): drag a box on the map; a controller: A at one corner, A at the other (the
// crosshair), X downloads, B cancels. The confirm box shows the server's estimate (size, download, time with the hosts'
// daily limits, the imagery left out). The list (Places panel) shows each area's progress, Pause / Resume, Delete.
const selecting = ref(false)
const boxStart = ref(null), boxEnd = ref(null)      // { lat, lon } corners
const areaAsk = ref(null)                           // { box: {s, w, n, e}, plan, error, busy }
const areaName = ref("")
const areas = ref({ areas: [] })
const areaMsg = ref(""), areaMsgError = ref(false)
const confirmAreaId = ref(null)
let areaSeq = 0, areaTimer = null, confirmAreaTimer = null
const areaList = computed(() => toArray(areas.value && areas.value.areas))
const selectHint = computed(() => (areaAsk.value ? "X: pre-cache this area · B: cancel"
  : padMode.value ? (boxStart.value ? "A: the other corner · B: cancel" : "A: one corner of the area · B: cancel")
    : "Drag a box over the area to download · Esc: cancel"))

function toggleSelect() {
  if (selecting.value) { cancelSelect(); return }
  // (picking an area: the Players list closed, and a followed player's glide no longer moves the map under the box)
  if (playersPanel.value) closePlayers()
  if (followingPlayer()) follow.value = false
  selecting.value = true
  boxStart.value = boxEnd.value = null
  areaAsk.value = null
}
function cancelSelect() {
  selecting.value = false
  boxStart.value = boxEnd.value = null
  areaAsk.value = null
  areaSeq++
}
function llAt(ev) {
  const p = localXY(ev)
  const v = view.value
  return mm.fromScreen(p.x, p.y, v.lat, v.lon, v.z, w.value, h.value)
}
function startBox(ev) {
  selecting.value = true
  areaAsk.value = null
  boxStart.value = llAt(ev)
  boxEnd.value = boxStart.value
  const move = e => { boxEnd.value = llAt(e) }
  const up = e => {
    window.removeEventListener("mousemove", move)
    window.removeEventListener("mouseup", up)
    boxEnd.value = llAt(e)
    askArea()
  }
  window.addEventListener("mousemove", move)
  window.addEventListener("mouseup", up)
}
// the box (south, west, north, east) of the two corners, or null
const pickedBox = computed(() => {
  const a = boxStart.value, b = boxEnd.value
  if (!a || !b) return null
  return { s: Math.min(a.lat, b.lat), n: Math.max(a.lat, b.lat), w: Math.min(a.lon, b.lon), e: Math.max(a.lon, b.lon) }
})
function rectOf(b) {
  const p0 = screen(b.n, b.w), p1 = screen(b.s, b.e)
  return { x: Math.min(p0.x, p1.x), y: Math.min(p0.y, p1.y), w: Math.abs(p1.x - p0.x), h: Math.abs(p1.y - p0.y) }
}
const selectRect = computed(() => { view.value; w.value; h.value; return selecting.value && pickedBox.value ? rectOf(pickedBox.value) : null })
const areaRects = computed(() => {
  view.value; w.value; h.value
  if (!(panelOpen.value || cacheOn.value || selecting.value)) return []
  return areaList.value.filter(a => !a.route).map(a => ({ id: a.id, name: a.name, cls: a.state, ...rectOf(a) }))
    .filter(r => r.x < w.value && r.y < h.value && r.x + r.w > 0 && r.y + r.h > 0)
})
// saved routes (areas.py "route": its points, ~400 at most; "half": m each side): their corridors, the line as wide
// as the corridor
const areaRoutes = computed(() => {
  view.value; w.value; h.value
  if (!(panelOpen.value || cacheOn.value)) return []
  return areaList.value.filter(a => a.route).map(a => {
    const pts = toArray(a.route).map(p => toArray(p))
    if (pts.length < 2) return null
    const lat = Number(pts[0][0]), lon = Number(pts[0][1])
    const p0 = screen(lat, lon), p1 = screen(lat + (Number(a.half) || 800) / 111195, lon)
    return { id: a.id, cls: a.state, px: Math.max(3, 2 * Math.abs(p1.y - p0.y)),
      points: pts.map(p => { const q = screen(Number(p[0]), Number(p[1])); return `${q.x.toFixed(1)},${q.y.toFixed(1)}` }).join(" ") }
  }).filter(Boolean)
})
// "Save route": the planned GPS route's corridor, this far each side (m)
const ROUTE_WIDTHS = [
  { m: 402, mi: "¼ mi", km: "0.4 km" }, { m: 805, mi: "½ mi", km: "0.8 km" },
  { m: 1609, mi: "1 mi", km: "1.6 km" }, { m: 3219, mi: "2 mi", km: "3.2 km" },
]
const routeHalf = ref(805)
// "Build the world too" (server areas.py mode "world"): the estimate asked again with it
const areaWorld = ref(false)
const areaMode = () => (areaWorld.value ? "world" : "data")
function setAreaWorld(on) {
  areaWorld.value = !!on
  const a = areaAsk.value
  if (!a) return
  if (a.route) askRoute(a.half)
  else askArea(a.box)
}
function askRoute(half) {
  routeHalf.value = half
  const seq = ++areaSeq
  areaAsk.value = { route: true, half, plan: null, error: "" }
  areaName.value = state.value.gps && state.value.gps.name ? String(state.value.gps.name).slice(0, 60) : ""
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiRoutePlan and beamngEarth_settings.uiRoutePlan(${half}, ${luaStr(areaMode())}) or nil`, r => {
    if (seq !== areaSeq || !areaAsk.value) return
    if (!r || r.error || !r.id) { areaAsk.value = { ...areaAsk.value, error: (r && r.error) || "BeamNG.Earth is not loaded" }; return }
    let tries = 0
    const poll = () => {
      if (seq !== areaSeq || !areaAsk.value) return
      engineLua(`beamngEarth_settings and beamngEarth_settings.uiAreaPlanResult(${r.id}) or nil`, res => {
        if (seq !== areaSeq || !areaAsk.value) return
        if (!res) {
          if (++tries < 200) setTimeout(poll, 150)
          else areaAsk.value = { ...areaAsk.value, error: "No answer from the companion server" }
          return
        }
        areaAsk.value = { ...areaAsk.value, plan: res, error: res.ok ? "" : String(res.error || "Not possible") }
      })
    }
    setTimeout(poll, 100)
  })
}
function askArea(again) {
  const b = again || pickedBox.value
  if (!b) return
  const rect = rectOf(b)
  if (!again && (rect.w < 6 || rect.h < 6)) { boxStart.value = boxEnd.value = null; return }      // a click, not a box
  const seq = ++areaSeq
  areaAsk.value = { box: b, plan: null, error: "" }
  if (!again) areaName.value = ""
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiAreaPlan and beamngEarth_settings.uiAreaPlan(${b.s}, ${b.w}, ${b.n}, ${b.e}, ${luaStr(areaMode())}) or nil`, r => {
    if (seq !== areaSeq || !areaAsk.value) return
    if (!r || r.error || !r.id) { areaAsk.value = { ...areaAsk.value, error: (r && r.error) || "BeamNG.Earth is not loaded" }; return }
    let tries = 0
    const poll = () => {
      if (seq !== areaSeq || !areaAsk.value) return
      engineLua(`beamngEarth_settings and beamngEarth_settings.uiAreaPlanResult(${r.id}) or nil`, res => {
        if (seq !== areaSeq || !areaAsk.value) return
        if (!res) {
          if (++tries < 100) setTimeout(poll, 150)
          else areaAsk.value = { ...areaAsk.value, error: "No answer from the companion server" }
          return
        }
        areaAsk.value = { ...areaAsk.value, plan: res, error: res.ok ? "" : String(res.error || "Not possible") }
      })
    }
    setTimeout(poll, 100)
  })
}
const areaHosts = computed(() => {
  const p = areaAsk.value && areaAsk.value.plan
  return p ? toArray(p.hosts).map(hst => toArray(hst)).slice(0, 5) : []
})
function confirmArea() {
  const a = areaAsk.value
  if (!a || !a.plan || !a.plan.ok || a.busy) return
  areaAsk.value = { ...a, busy: true }
  const b = a.box
  const call = a.route
    ? `beamngEarth_settings.uiRouteAdd and beamngEarth_settings.uiRouteAdd(${a.half}, ${luaStr(areaName.value)}, ${luaStr(areaMode())})`
    : `beamngEarth_settings.uiAreaAdd and beamngEarth_settings.uiAreaAdd(${b.s}, ${b.w}, ${b.n}, ${b.e}, ${luaStr(areaName.value)}, ${luaStr(areaMode())})`
  engineLua(`beamngEarth_settings and ${call} or nil`, r => {
    if (!r || r.error) { areaAsk.value = { ...a, busy: false, error: (r && r.error) || "BeamNG.Earth is not loaded" }; return }
    let tries = 0
    const poll = () => {
      engineLua(`beamngEarth_settings and beamngEarth_settings.uiAreaResult(${r.id}) or nil`, res => {
        if (!res) { if (++tries < 60) setTimeout(poll, 150); else areaAsk.value = { ...a, busy: false, error: "No answer from the companion server" }; return }
        if (!res.ok) { areaAsk.value = { ...a, busy: false, error: String(res.error || "Not saved") }; return }
        cancelSelect()
        panelOpen.value = true
        areaMsg.value = a.route ? "Downloading along the route in the background" : "Downloading the area in the background"
        areaMsgError.value = false
        pollAreas()
      })
    }
    setTimeout(poll, 100)
  })
}
function areaCmd(a, op) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiAreaCmd and beamngEarth_settings.uiAreaCmd(${luaStr(a.id)}, ${luaStr(op)}) or nil`, r => {
    if (!r || r.error) { areaMsg.value = (r && r.error) || "BeamNG.Earth is not loaded"; areaMsgError.value = true; return }
    setTimeout(pollAreas, 200)
  })
}
function deleteArea(a) {
  clearTimeout(confirmAreaTimer)
  if (confirmAreaId.value !== a.id) {
    confirmAreaId.value = a.id
    confirmAreaTimer = setTimeout(() => { confirmAreaId.value = null }, 4000)
    return
  }
  confirmAreaId.value = null
  areaCmd(a, "delete")
}
function showArea(a) {
  follow.value = false
  const lat = (Number(a.s) + Number(a.n)) / 2, lon = (Number(a.w) + Number(a.e)) / 2
  // the zoom where the box fills about half the view
  const span = Math.max(1e-6, (Number(a.e) - Number(a.w)) / 360)
  const z = Math.max(mm.MIN_Z, Math.min(15, Math.log2(w.value * 0.5 / (mm.TILE * span))))
  view.value = { lat, lon, z }
}
function pollAreas() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiAreas and beamngEarth_settings.uiAreas() or nil", r => {
    if (r) areas.value = r
  })
}
function areaPct(a) { return a.total ? Math.min(100, Math.round(100 * a.done / a.total)) : (a.state === "done" ? 100 : 0) }
function areaStateText(a) {
  const pct = `${areaPct(a)} %`
  const what = { queued: "queued", checking: "checking what is there", running: "downloading", paused: "paused",
    waiting: "waiting", done: "downloaded" }[a.state] || a.state
  return a.msg ? `${pct} · ${what}: ${a.msg}` : `${pct} · ${what}`
}
areaTimer = setInterval(() => { if (panelOpen.value || cacheOn.value || selecting.value) pollAreas() }, 2000)
pollAreas()
onUnmounted(() => { clearInterval(areaTimer); clearTimeout(confirmAreaTimer); areaSeq++ })

const metricUnits = computed(() => cacheMetric.value)
function fmtArea(km2) {
  const mi2 = km2 / 2.58999
  const m = metricUnits.value
  const v = m ? km2 : mi2
  return `${v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString()} ${m ? "km²" : "sq mi"}`
}
function fmtBytes(b) {
  if (b >= 1e9) return `${(b / 1e9).toFixed(1)} GB`
  return `${Math.max(1, Math.round(b / 1e6))} MB`
}
function fmtEta(p) {
  if (p.days > 1) return `about ${Math.ceil(p.days)} days (the hosts' daily limits)`
  const s = p.seconds || 0
  if (s < 3600) return `about ${Math.max(1, Math.round(s / 60))} min`
  return `about ${Math.round(s / 3600)} h`
}

// ---- Places panel (settings.lua uiPlace* -> places.lua; every call answers with both lists) ---------
// the places panel; closed, the saved places' pins leave the map too (the user, 2026-10-14). Remembered.
const PANEL_KEY = "beamngEarth.map.placesOpen"
const panelOpen = ref((() => { try { return localStorage.getItem(PANEL_KEY) !== "0" } catch (e) { return true } })())
watch(panelOpen, on => { try { localStorage.setItem(PANEL_KEY, on ? "1" : "0") } catch (e) {} })
const newName = ref(""), coordText = ref(""), renameText = ref("")
const renamingId = ref(null), confirmDeleteId = ref(null)
const placesMsg = ref(""), placesMsgError = ref(false)
let confirmTimer = null

// an empty Lua table arrives as {} (an object), not []
function toArray(v) {
  if (Array.isArray(v)) return v
  return v && typeof v === "object" ? Object.values(v) : []
}
const placeList = computed(() => toArray(state.value.places))
const recentList = computed(() => toArray(state.value.recent))

// a Lua string literal (control characters become spaces) and a Lua number ("nil" if not one)
function luaStr(v) {
  return '"' + String(v).replace(/[\x00-\x1f\x7f]/g, " ").replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'
}
function luaNum(v) {
  const n = Number(v)
  return Number.isFinite(n) ? String(n) : "nil"
}

function recentDetail(r) {
  const parts = []
  if (r.name) parts.push(r.text)
  if (r.kind === "start") parts.push("level start")
  return parts.join("  |  ")
}

function placesCall(script, okText, onOk) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiPlaces and beamngEarth_settings.${script} or nil`, res => {
    if (!res) { placesMsg.value = "BeamNG.Earth is not loaded"; placesMsgError.value = true; return }
    state.value = { ...state.value, places: res.places, recent: res.recent, cannotGo: res.cannotGo }
    if (res.ok === false && res.error) { placesMsg.value = res.error; placesMsgError.value = true }
    else if (okText !== undefined) { placesMsg.value = okText; placesMsgError.value = false }
    if (res.ok && onOk) onOk()
  })
}

function showOnMap(p) {
  if (p.lat == null || p.lon == null) return
  follow.value = false
  view.value = { ...view.value, lat: Number(p.lat), lon: Number(p.lon), z: Math.max(view.value.z, 13) }
}
// after a Go: back to driving, as the map's own Teleport does
function wentThere() { follow.value = true; if (!embedded) close() }
function saveHere() { placesCall(`uiPlaceAddHere(${luaStr(newName.value)})`, "Saved", () => { newName.value = "" }) }
function goText() {
  if (!coordText.value.trim()) return
  placesCall(`uiPlaceGoText(${luaStr(coordText.value)})`, `Going to ${coordText.value.trim()}`, wentThere)
}
function saveText() {
  if (!coordText.value.trim()) return
  placesCall(`uiPlaceAdd(${luaStr(newName.value)}, ${luaStr(coordText.value)})`, "Saved", () => { newName.value = ""; coordText.value = "" })
}
function goPlace(p) { placesCall(`uiPlaceGo(${luaNum(p.id)})`, `Going to ${p.name}`, wentThere) }
function startRename(p) { renamingId.value = p.id; renameText.value = p.name; confirmDeleteId.value = null }
function commitRename(p) {
  placesCall(`uiPlaceRename(${luaNum(p.id)}, ${luaStr(renameText.value)})`, "Renamed", () => { renamingId.value = null })
}
// Delete asks once more: the button reads "Confirm" for 4 s
function deletePlace(p) {
  if (confirmTimer) clearTimeout(confirmTimer)
  confirmTimer = null
  if (confirmDeleteId.value !== p.id) {
    confirmDeleteId.value = p.id
    confirmTimer = setTimeout(() => { confirmDeleteId.value = null }, 4000)
    return
  }
  confirmDeleteId.value = null
  placesCall(`uiPlaceDelete(${luaNum(p.id)})`, `Deleted ${p.name}`)
}
// the row's lat/lon go along: a Go moves its place to the front, so the index alone can be stale
function goRecent(r) {
  placesCall(`uiPlaceGoRecent(${luaNum(r.index)}, ${luaNum(r.lat)}, ${luaNum(r.lon)})`, `Going to ${r.name || r.text}`, wentThere)
}
function saveRecent(r) {
  const ll = `${Number(r.lat).toFixed(6)}, ${Number(r.lon).toFixed(6)}`
  placesCall(`uiPlaceAdd(${luaStr(r.name || "")}, ${luaStr(ll)})`, "Saved")
}

// ---- right-click menu: teleport / GPS / save at a point ---------------------------------------
const ctx = ref(null)       // { x, y, lat, lon }
function onContext(ev) {
  const p = localXY(ev)
  const v = view.value
  const ll = mm.fromScreen(p.x, p.y, v.lat, v.lon, v.z, w.value, h.value)
  ctx.value = { x: Math.min(p.x, w.value - 210), y: Math.min(p.y, h.value - 170), lat: ll.lat, lon: ll.lon }
}
function closeCtx() { ctx.value = null }
// picked from the right-click menu: straight away (a second "Teleport here?" with the same choices was one click too
// many, the user 2026-10-14); a failure shows in the confirmation box
function ctxTeleport() {
  if (!ctx.value || state.value.cannotGo) return
  const c = ctx.value
  closeCtx()
  teleportNow(c.lat, c.lon, "")
}
function ctxSave() {
  if (!ctx.value) return
  const c = ctx.value
  closeCtx()
  panelOpen.value = true
  placesCall(`uiPlaceAdd("", ${luaStr(`${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`)})`, "Saved")
}
// decimal "lat, lon" (pasteable into Google Maps etc.), not the N/S/E/W display format
const toast = ref("")
let toastTimer = null
function showToast(msg) {
  toast.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = "" }, 1500)
}
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => showToast("Copied"), () => copyFallback(text))
  } else {
    copyFallback(text)
  }
}
// the CEF UI's clipboard permission can be missing (navigator.clipboard rejects): the old execCommand way instead
function copyFallback(text) {
  try {
    const ta = document.createElement("textarea")
    ta.value = text
    ta.style.position = "fixed"
    ta.style.opacity = "0"
    document.body.appendChild(ta)
    ta.select()
    document.execCommand("copy")
    document.body.removeChild(ta)
    showToast("Copied")
  } catch (e) {
    showToast("Copy failed")
  }
}
function ctxCopy() {
  if (!ctx.value) return
  const c = ctx.value
  closeCtx()
  copyText(`${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`)
}
function ctxGps() {
  if (!ctx.value) return
  const c = ctx.value
  closeCtx()
  setGps(c.lat, c.lon, "")
}
function setGps(lat, lon, name, then) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapGps(${luaNum(lat)}, ${luaNum(lon)}, ${luaStr(name || "")}) or nil`, r => {
    if (r && r.ok) {
      state.value = { ...state.value, gps: { lat, lon, name, planning: true } }
      routePts.value = []
      routeVersion = 0
      if (then) then()
    }
  })
}
// the car drives itself along the route; turned on, the map closes so the drive can be watched
function setAutodrive(on) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiGpsAutodrive(${on ? "true" : "false"}) or nil`, r => {
    if (!r || !r.ok) return
    state.value = { ...state.value, gps: state.value.gps && { ...state.value.gps, autodrive: r.autodrive } }
    if (r.autodrive && !embedded) close()
  })
}
function clearGps() {
  closeCtx()
  engineLua("beamngEarth_settings and beamngEarth_settings.uiMapGps(nil) or nil", () => {
    state.value = { ...state.value, gps: null }
  })
}
// the planned route (server routing.py), fetched when its version changes; drawn done (grey) / ahead (blue)
const routePts = ref([])
const routeElev = ref([])            // [[distM, elevM], ...] along the route (routing.resample, dem.sample)
let routeVersion = 0
watch(() => state.value.gps && state.value.gps.routeVersion, v => {
  if (!v) { routePts.value = []; routeElev.value = []; routeVersion = 0; return }
  if (v === routeVersion) return
  engineLua("beamngEarth_settings and beamngEarth_settings.uiGpsRoute and beamngEarth_settings.uiGpsRoute() or nil", r => {
    if (!r || !r.points) return
    routeVersion = r.version
    const pair = p => (Array.isArray(p) ? [Number(p[0]), Number(p[1])] : [Number(p["1"]), Number(p["2"])])
    routePts.value = toArray(r.points).map(pair).filter(p => isFinite(p[0]) && isFinite(p[1]))
    routeElev.value = toArray(r.elev || []).map(pair).filter(p => isFinite(p[0]) && isFinite(p[1]))
  })
})
watch(() => state.value.gps, g => { if (!g) { routePts.value = []; routeElev.value = []; routeVersion = 0 } })

// the elevation chart (a sparkline + fill, under the GPS box): min/max over the whole route, a marker at the
// car's current distance along it (from the remaining distance, same as the route-ahead/done split above)
const ELEV_W = 220, ELEV_H = 46, ELEV_MIN_SPAN_M = 30
const elevStats = computed(() => {
  const pts = routeElev.value
  if (pts.length < 2) return null
  const total = pts[pts.length - 1][0]
  if (!(total > 0)) return null
  let minE = Infinity, maxE = -Infinity, up = 0, down = 0
  for (let i = 0; i < pts.length; i++) {
    const e = pts[i][1]
    if (e < minE) minE = e
    if (e > maxE) maxE = e
    if (i > 0) { const d = e - pts[i - 1][1]; if (d > 0) up += d; else down -= d }
  }
  // at least ELEV_MIN_SPAN_M tall, centred: a short, flat drive's few metres filled the whole chart and looked like
  // mountains (the user, 2026-10-27: "way to spikey even for a short drive")
  const span = Math.max(ELEV_MIN_SPAN_M, maxE - minE)
  const lo = (minE + maxE) / 2 - span / 2
  const xs = d => (d / total) * ELEV_W
  const ys = e => ELEV_H - ((e - lo) / span) * ELEV_H
  const line = pts.map(p => `${xs(p[0]).toFixed(1)},${ys(p[1]).toFixed(1)}`).join(" ")
  const remaining = state.value.gps && state.value.gps.remaining
  const frac = remaining != null ? Math.max(0, Math.min(1, 1 - remaining / total)) : 0
  return { line, area: `0,${ELEV_H} ${line} ${ELEV_W},${ELEV_H}`, minE, maxE, up, down, markerX: xs(frac * total) }
})
function routePath(from, to) {
  const pts = routePts.value.slice(from, to)
  if (pts.length < 2) return ""
  return pts.map(p => { const s = screen(p[0], p[1]); return `${s.x.toFixed(1)},${s.y.toFixed(1)}` }).join(" ")
}
const routeSplit = computed(() => {
  const g = state.value.gps
  return Math.max(0, Math.min(routePts.value.length - 1, (g && g.progress ? g.progress : 1) - 1))
})
const routeAhead = computed(() => { view.value; w.value; h.value; return routePath(routeSplit.value, routePts.value.length) })
const routeDone = computed(() => { view.value; w.value; h.value; return routePath(0, routeSplit.value + 2) })
function fmtDur(s) {
  const m = Math.round(s / 60)
  return m < 60 ? `${Math.max(1, m)} min` : `${Math.floor(m / 60)} h ${m % 60} min`
}

const gpsScreen = computed(() => {
  const g = state.value.gps
  return g ? screen(g.lat, g.lon) : null
})
// a height for the route's elevation profile, in the game's length unit (fmtMiles)
function fmtHeight(m) {
  return state.value.gps && state.value.gps.metric ? `${Math.round(m)} m` : `${Math.round(m * 3.28084)} ft`
}
function fmtMiles(m) {
  // (the game's own length unit, Options > Display: gps.lua's metric; GitHub #327. uiMapState's own metric with no
  // GPS route: the Players list)
  if ((state.value.gps || state.value).metric) {
    const km = m / 1000
    return km < 1 ? `${Math.round(m / 10) * 10} m` : `${km < 10 ? km.toFixed(1) : Math.round(km)} km`
  }
  const mi = m / 1609.344
  return mi < 0.1 ? `${Math.round(m * 3.28084)} ft` : `${mi < 10 ? mi.toFixed(1) : Math.round(mi)} mi`
}

// ---- search --------------------------------------------------------------------------------
function allPlaces() {
  const s = state.value
  return [...(s.places || []), ...(s.recent || []).map(r => ({ ...r, name: r.name || r.text }))]
}

function goTo(lat, lon, name) {
  follow.value = false
  view.value = { lat, lon, z: Math.max(view.value.z, 13) }
  askTeleport(lat, lon, name)
}

// Search as you type: saved / recent places at once, and addresses and places from the server (geocode.py:
// Photon, OpenStreetMap) 300 ms after the last key, 3+ characters; a typed "lat, lon" on Enter.
const searching = ref(false)
const resultIndex = ref(-1)
let searchTimer = null, searchSeq = 0
const LATLON_RE = /^[-+]?\d+(\.\d+)?[°\s]*[NSns]?\s*[,;\s]\s*[-+]?\d+(\.\d+)?/

function typing(on) { engineLua(`if setCEFTyping then setCEFTyping(${on ? "true" : "false"}) end`) }

function localHits(text) {
  return mm.searchPlaces(allPlaces(), text).slice(0, 3).map(r => ({ ...r, saved: true }))
}

function liveSearch() {
  const text = query.value.trim()
  searchMsg.value = ""
  resultIndex.value = -1
  const seq = ++searchSeq
  const local = text ? localHits(text) : []
  results.value = local
  if (text.length < 3 || LATLON_RE.test(text)) { searching.value = false; return }
  searching.value = true
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiGeocode(${luaStr(text)}) or nil`, r => {
    if (seq !== searchSeq) return
    if (!r || r.error || !r.id) { searching.value = false; if (r && r.error && !local.length) searchMsg.value = r.error; return }
    let tries = 0
    const poll = () => {
      if (seq !== searchSeq) return
      engineLua(`beamngEarth_settings and beamngEarth_settings.uiGeocodeResult(${r.id}) or nil`, res => {
        if (seq !== searchSeq) return
        if (!res) {
          if (++tries < 40) setTimeout(poll, 150)
          else { searching.value = false; if (!local.length) searchMsg.value = "No answer from the address search" }
          return
        }
        searching.value = false
        const online = toArray(res.results)
        results.value = [...local, ...online]
        if (!results.value.length) searchMsg.value = res.error ? "Address search unavailable (offline?)" : "Nothing found"
      })
    }
    setTimeout(poll, 150)
  })
}
watch(query, () => { clearTimeout(searchTimer); searchTimer = setTimeout(liveSearch, 300) })

function moveResult(d) {
  const n = results.value.length
  if (!n) return
  resultIndex.value = (resultIndex.value + d + n) % n
}

function onSearch() {
  const text = query.value.trim()
  if (!text) { results.value = []; return }
  if (results.value.length) { pickResult(results.value[Math.max(0, resultIndex.value)]); return }
  // a typed latitude, longitude (places.lua parses every format the page accepts)
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapParse(${luaStr(text)}) or nil`, r => {
    if (r && typeof r.lat === "number") goTo(r.lat, r.lon, "")
    else if (!searching.value) searchMsg.value = "Nothing found (try more of the address, or a latitude, longitude)"
  })
}

function pickResult(r) {
  searchSeq++
  clearTimeout(searchTimer)
  results.value = []
  searching.value = false
  searchMsg.value = ""
  const name = r.name || ""
  query.value = name
  setTimeout(() => { searchSeq++; clearTimeout(searchTimer) }, 0)   // the watch on query must not search again
  const label = r.detail ? `${name}, ${r.detail}` : name
  follow.value = false
  view.value = { lat: r.lat, lon: r.lon, z: Math.max(view.value.z, 13) }
  setGps(r.lat, r.lon, label)      // a search result: the route there (Teleport: the GPS box or right-click)
}

// ---- teleport ------------------------------------------------------------------------------
function askTeleport(lat, lon, name) {
  teleportMsg.value = ""
  target.value = { lat, lon: mm.wrapLon(lon), name: name || "" }
}

// The GPS box's Teleport (a search result) and the right-click menu: straight away, no "Teleport here?" (the user
// 2026-10-16: "we dont need the 2nd popup"); a failure shows in the confirmation box
function teleportNow(lat, lon, name) {
  if (state.value.cannotGo) return
  askTeleport(lat, lon, name)
  target.value.quick = true       // the box only if it fails
  teleport()
}

function teleport() {
  const t = target.value
  if (!t) return
  busy.value = true
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapTeleport(${t.lat}, ${t.lon}, ${JSON.stringify(t.name)}) or nil`, r => {
    busy.value = false
    if (r && r.ok) {
      target.value = null
      follow.value = true
      view.value = { ...view.value, lat: t.lat, lon: t.lon }
      if (!embedded) close()
    } else {
      teleportMsg.value = (r && r.error) || "Teleport failed (BeamNG.Earth not loaded?)"
    }
  })
}

function close() {
  engineLua("if beamngEarth_settings and beamngEarth_settings.mapOpened then beamngEarth_settings.mapOpened(false) end; " +
            "if beamngEarth_settings and beamngEarth_settings.navigate then beamngEarth_settings.navigate('play') else guihooks.trigger('ChangeState', {state = 'play'}) end")
}

function onEscape() {
  if (playersPanel.value) { closePlayers(); return }
  if (selecting.value) { cancelSelect(); return }
  if (layersPanel.value) { layersPanel.value = false; return }
  if (target.value) { target.value = null; return }
  if (results.value.length) { results.value = []; return }
  if (!embedded) close()
}

function measure() {
  const el = viewEl.value
  if (!el) return
  w.value = el.clientWidth || 1280
  h.value = el.clientHeight || 720
}

onMounted(() => {
  measure()
  if (window.ResizeObserver) {
    resizeObs = new ResizeObserver(measure)
    resizeObs.observe(viewEl.value)
  }
  viewEl.value.addEventListener("mousemove", onHover)
  engineLua("if beamngEarth_settings and beamngEarth_settings.mapOpened then beamngEarth_settings.mapOpened(true) end")
  poll()
  pollTimer = setInterval(poll, 1000)
  nextTick(() => rootEl.value && rootEl.value.focus())
})

onUnmounted(() => {
  clearInterval(pollTimer)
  if (resizeObs) resizeObs.disconnect()
  window.removeEventListener("mousemove", onMove)
  window.removeEventListener("mouseup", onUp)
  if (padFrame && window.cancelAnimationFrame) window.cancelAnimationFrame(padFrame)
  if (glideFrame && window.cancelAnimationFrame) window.cancelAnimationFrame(glideFrame)
  glide = null
  padFrame = 0
  engineLua("if beamngEarth_settings and beamngEarth_settings.mapOpened then beamngEarth_settings.mapOpened(false) end")
})

const fmtLatLon = mm.fmtLatLon
</script>

<style scoped>
.pad-cross {
  position: absolute; left: 50%; top: 50%; width: 34px; height: 34px; margin: -17px 0 0 -17px;
  border: 2px solid rgba(255, 255, 255, 0.95); border-radius: 50%; box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.6);
  pointer-events: none; z-index: 5;
}
.pad-cross::before, .pad-cross::after {
  content: ""; position: absolute; background: rgba(255, 255, 255, 0.95); box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.5);
}
.pad-cross::before { left: 15px; top: -9px; width: 2px; height: 50px; }
.pad-cross::after { top: 15px; left: -9px; height: 2px; width: 50px; }
.pad-hint {
  position: absolute; left: 50%; top: 3.4em; transform: translateX(-50%); padding: 0.35em 0.9em;
  background: rgba(0, 0, 0, 0.6); color: #fff; border-radius: 4px; font-size: 0.9em; pointer-events: none;
  white-space: nowrap; z-index: 5;
}
/* the element a controller has selected (the menu's focus: the game marks it .focus-visible), plain to see (the user,
   2026-10-21: "it can be hard to see what is selected using the menu buttons") */
.earth-map .focus-visible, .earth-map :focus-visible {
  outline: 3px solid #ffb54a !important; outline-offset: 2px; box-shadow: 0 0 0 6px rgba(0, 0, 0, 0.55), 0 0 14px 4px rgba(255, 181, 74, 0.7) !important;
}
.earth-map .map-btn.focus-visible { background: rgba(224, 118, 28, 0.9); border-color: #ffb54a; }
/* under the GPS box (left: 10px, top: 56px, one row of buttons), which it partly covered (the user, 2026-10-21) */
.pad-hint.below-gps { top: 100px; }
.earth-map {
  position: absolute;
  inset: 0;
  background: #1d2228;
  color: #eee;
  font-family: inherit;
  overflow: hidden;
  outline: none;
  user-select: none;
  pointer-events: auto;
}
.earth-map.embedded {
  position: relative;
  width: 100%;
  height: 70vh;
  min-height: 420px;
  border-radius: 6px;
}
.map-view {
  position: absolute;
  inset: 0;
  overflow: hidden;
  cursor: crosshair;
  background: #2a3038;
}
.map-tile {
  position: absolute;
  pointer-events: none;
  image-rendering: auto;
}
/* "Dark" base layer: the "map" tiles (terrain-light, already hillshaded) inverted and hue-rotated back towards
   blue/grey - relief stays visible, just dark instead of the light "paper" look (checked against a sample tile,
   2026-10-27: a plain satellite dark mode lost the mountains, the user wanted them kept) */
.map-tile.tile-dark {
  filter: invert(1) hue-rotate(180deg) brightness(0.92);
}
.cache-canvas { position: absolute; left: 0; top: 0; pointer-events: none; }
.area-rect {
  position: absolute; pointer-events: none; border: 2px dashed rgba(255, 154, 60, 0.9); box-sizing: border-box;
  background: rgba(255, 154, 60, 0.07); z-index: 1;
}
.area-rect.done { border-color: rgba(61, 220, 132, 0.9); background: rgba(61, 220, 132, 0.06); }
.area-rect.paused { border-color: rgba(160, 170, 180, 0.9); background: none; }
.map-grid .area-route { fill: none; stroke: rgba(255, 181, 74, 0.28); stroke-linecap: round; stroke-linejoin: round; }
.map-grid .area-route.done { stroke: rgba(61, 220, 132, 0.22); }
.map-grid .area-route.paused { stroke: rgba(160, 170, 180, 0.25); }
.route-width { gap: 6px; align-items: center; margin: 4px 0; }
.area-world { display: block; margin: 6px 0 2px; }
.area-rect.selecting { border: 2px solid #ffb54a; background: rgba(255, 181, 74, 0.15); z-index: 3; }
.area-rect-label {
  position: absolute; left: 4px; top: 2px; font-size: 11px; padding: 0 4px; background: rgba(0, 0, 0, 0.6);
  border-radius: 3px; white-space: nowrap;
}
.area-confirm { max-width: 520px; }
.area-host { font-size: 12px; color: #c8d2dc; margin-top: 2px; }
.area-note { font-size: 11px; color: #9aa4ae; margin-top: 6px; }
.area-name-row { margin-top: 8px; }
.area-bar { height: 4px; margin-top: 3px; border-radius: 2px; background: rgba(255, 255, 255, 0.12); overflow: hidden; }
.area-bar-fill { display: block; height: 100%; background: #36a3ff; transition: width 0.3s linear; }
.area-bar-fill.done { background: #3ddc84; }
.area-bar-fill.paused, .area-bar-fill.waiting { background: #9aa4ae; }
.cache-select { margin: 2px 0 4px; }
.cache-legend {
  position: absolute; left: 10px; bottom: calc(6.5vh + 40px); z-index: 9; padding: 6px 10px 7px; font-size: 12px;
  background: rgba(15, 18, 22, 0.88); border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 5px; cursor: default;
}
.cache-legend-title { font-weight: 600; margin-bottom: 4px; }
.cache-legend-row { display: flex; align-items: center; gap: 6px; padding: 1px 0; white-space: nowrap; }
.cache-swatch { width: 12px; height: 12px; border-radius: 2px; border: 1px solid rgba(255, 255, 255, 0.35); flex-shrink: 0; }
.cache-legend-hint { margin-top: 3px; font-size: 11px; color: #9aa4ae; }
.map-grid {
  position: absolute;
  left: 0;
  top: 0;
  pointer-events: none;
}
.map-grid line { stroke: rgba(255, 255, 255, 0.18); stroke-width: 1; }
.map-grid line.meridian { stroke: rgba(255, 210, 120, 0.45); }
.grid-label { fill: rgba(255, 255, 255, 0.65); font-size: 11px; }
/* layers (Natural Earth): borders with a dark casing so they read over any photo; names with a halo */
.map-grid .layer-border { fill: none; stroke: rgba(255, 236, 170, 0.85); stroke-width: 1.6; stroke-dasharray: 6 3; }
.map-grid .layer-state { fill: none; stroke: rgba(255, 255, 255, 0.45); stroke-width: 1; stroke-dasharray: 3 3; }
.map-grid .layer-country { fill: #fff3d0; font-size: 15px; font-weight: 600; letter-spacing: 0.08em; text-anchor: middle;
  text-transform: uppercase; paint-order: stroke; stroke: rgba(0, 0, 0, 0.7); stroke-width: 3px; }
.map-grid .layer-place { fill: #fff; font-size: 12px; paint-order: stroke; stroke: rgba(0, 0, 0, 0.75); stroke-width: 3px; }
.map-grid .layer-place.capital { font-weight: 700; font-size: 13px; }
.map-grid .layer-dot { fill: #fff; stroke: rgba(0, 0, 0, 0.8); stroke-width: 1; }

.marker { position: absolute; width: 0; height: 0; }
.marker.place { cursor: pointer; z-index: 2; }
.marker.place .pin {
  position: absolute; left: -7px; top: -7px; width: 14px; height: 14px; border-radius: 50%;
  background: #ff9a3c; border: 2px solid #fff; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.6);
}
.marker-label {
  position: absolute; left: 11px; top: -9px; white-space: nowrap; font-size: 12px; padding: 1px 5px;
  background: rgba(0, 0, 0, 0.6); border-radius: 3px;
}
.marker.player { z-index: 4; pointer-events: none; }
.marker.player .arrow {
  position: absolute; left: -11px; top: -13px; width: 22px; height: 26px;
  background: #36a3ff; border: 2px solid #fff;
  clip-path: polygon(50% 0, 100% 100%, 50% 75%, 0 100%);
  transform-origin: 50% 50%;
  filter: drop-shadow(0 0 3px rgba(0, 0, 0, 0.8));
}
.marker.player .arrow.noheading { clip-path: circle(45%); }
.marker.other { z-index: 3; pointer-events: none; }
.marker.other .marker-label { pointer-events: auto; cursor: pointer; }
.marker.other .marker-label:hover { color: #c5ffdd; }
.marker.other.followed { z-index: 4; }
.marker.other.followed .marker-label { background: #17613c; box-shadow: 0 0 0 1px #3ddc84; }
.marker.other .arrow {
  position: absolute; left: -9px; top: -11px; width: 18px; height: 22px;
  background: #3ddc84;
  clip-path: polygon(50% 0, 100% 100%, 50% 75%, 0 100%);
  transform-origin: 50% 50%;
  filter: drop-shadow(0 0 3px rgba(0, 0, 0, 0.8));
}
.marker.other .arrow.noheading { clip-path: circle(40%); }
.marker.target { z-index: 3; pointer-events: none; }
.marker.target .cross {
  position: absolute; left: -10px; top: -10px; width: 20px; height: 20px; border: 2px solid #ff4b4b; border-radius: 50%;
}

.map-top {
  position: absolute; left: 12px; right: 12px; top: 12px; display: flex; gap: 8px; align-items: flex-start; z-index: 10;
}
.map-title {
  padding: 7px 12px; background: rgba(0, 0, 0, 0.7); border-radius: 4px; font-weight: 600; white-space: nowrap;
}
.weather-badge {
  display: flex; gap: 7px; align-items: center; padding: 7px 12px; background: rgba(0, 0, 0, 0.7);
  border-radius: 4px; white-space: nowrap; font-size: 13px;
}
.search { position: relative; flex: 1; max-width: 460px; }
.search-input {
  width: 100%; box-sizing: border-box; padding: 7px 10px; font-size: 14px; color: #fff;
  background: rgba(0, 0, 0, 0.75); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px; outline: none;
}
.search-input:focus { border-color: #ff9a3c; }
.search-results {
  position: absolute; left: 0; right: 0; top: 36px; background: rgba(20, 22, 26, 0.95); border-radius: 4px; overflow: hidden;
}
.search-result { padding: 6px 10px; display: flex; justify-content: space-between; gap: 10px; cursor: pointer; }
.search-result:hover { background: rgba(255, 154, 60, 0.35); }
.result-coords { opacity: 0.6; font-size: 12px; }
.search-msg { margin-top: 4px; padding: 5px 10px; font-size: 12px; background: rgba(0, 0, 0, 0.75); border-radius: 4px; }
.map-btn {
  padding: 7px 12px; min-width: 34px; font-size: 14px; color: #fff; cursor: pointer;
  background: rgba(0, 0, 0, 0.7); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px;
}
.map-btn:hover { background: rgba(60, 60, 60, 0.9); }
.map-btn.on { border-color: #36a3ff; color: #9fd3ff; }
.map-btn.primary { background: #e0761c; border-color: #e0761c; }
.map-btn:disabled { opacity: 0.45; cursor: default; }
.map-btn.close { margin-left: auto; }
.layers-wrap { position: relative; }
.layers-panel {
  position: absolute; right: 0; top: 38px; z-index: 12; min-width: 190px; padding: 8px 12px 10px;
  background: rgba(15, 18, 22, 0.94); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 6px;
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5); cursor: default;
}
.layer-opt { display: block; padding: 4px 0; font-size: 13px; white-space: nowrap; cursor: pointer; }
.layer-opt input { margin: 0 6px 0 0; vertical-align: -2px; cursor: pointer; }
.layer-hint { font-size: 11px; color: #9aa4ae; }
.players-panel { width: 300px; box-sizing: border-box; overflow-y: auto; }
.map-btn .following { display: inline-block; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom; }
.players-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
.players-head .panel-sub { margin: 0; }
.map-btn.small { padding: 4px 8px; font-size: 12px; }
.map-btn.danger { background: #a3302a; border-color: #a3302a; }

.places-panel {
  position: absolute; right: 10px; top: 56px; z-index: 6; display: flex; flex-direction: column; align-items: flex-end;
  /* ends above the game's info bar (controller hints: 6.5vh, as .map-bottom) */
  max-height: calc(100% - 56px - 6.5vh - 24px);
}
.panel-toggle { margin-bottom: 6px; }
.panel-body {
  width: 340px; overflow-y: auto; padding: 10px 12px; background: rgba(15, 18, 22, 0.88);
  border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 6px; user-select: text; cursor: default;
}
.panel-sub { margin: 10px 0 5px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: #9aa4ae; }
.panel-sub:first-child { margin-top: 0; }
.panel-row { display: flex; gap: 6px; align-items: center; }
.panel-input {
  flex: 1; min-width: 0; padding: 6px 8px; font-size: 13px; color: #fff; background: rgba(0, 0, 0, 0.55);
  border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px;
}
.panel-msg { margin-bottom: 6px; font-size: 12px; color: #9fd3ff; }
.panel-msg.warn { color: #ffb08a; }
.panel-item {
  display: flex; gap: 8px; align-items: center; padding: 6px 4px; border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.item-text { flex: 1; min-width: 0; cursor: pointer; }
.item-text:hover .item-name { color: #ffc28a; }
.item-name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item-sub { font-size: 11px; color: #9aa4ae; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item-btns { display: flex; gap: 4px; flex-shrink: 0; }
.panel-empty { padding: 6px 4px; font-size: 12px; color: #9aa4ae; }
.search-result.active { background: rgba(224, 118, 28, 0.6); }
.search-credit { padding: 4px 10px; font-size: 10px; color: #8a949e; border-top: 1px solid rgba(255, 255, 255, 0.08); }

.ctx-menu {
  position: absolute; z-index: 10; min-width: 190px; padding: 4px 0; background: rgba(15, 18, 22, 0.95);
  border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 5px; box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5);
}
.ctx-where { padding: 4px 12px 6px; font-size: 11px; color: #9aa4ae; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }
.ctx-item { padding: 7px 12px; font-size: 13px; cursor: pointer; }
.ctx-item:hover { background: rgba(224, 118, 28, 0.85); }
.ctx-item.disabled { opacity: 0.45; cursor: default; }
.ctx-item.disabled:hover { background: none; }
.gps-stack {
  position: absolute; left: 10px; top: 56px; z-index: 6; display: flex; flex-direction: column; gap: 6px;
  align-items: flex-start;
}
.gps-chip {
  display: flex; flex-direction: column; gap: 6px;
  padding: 5px 8px 5px 12px; font-size: 13px; background: rgba(15, 18, 22, 0.88); border: 1px solid #36a3ff; border-radius: 5px;
}
.elev-box {
  display: flex; flex-direction: column; gap: 4px; padding: 5px 8px 6px 10px;
  background: rgba(15, 18, 22, 0.88); border: 1px solid #2a3a4a; border-radius: 5px;
}
.elev-title { font-size: 12px; color: #c8d2dc; }
.elev-climb { margin-left: 6px; color: #9aa4ae; }
.gps-chip-row { display: flex; gap: 8px; align-items: center; }
.gps-dist { color: #9fd3ff; }
.elev-profile { display: flex; gap: 8px; align-items: flex-end; }
.elev-svg { display: block; flex-shrink: 0; }
.elev-area { fill: rgba(54, 163, 255, 0.25); stroke: none; }
.elev-line { fill: none; stroke: #9fd3ff; stroke-width: 1.5; }
.elev-marker { stroke: #ff9a3c; stroke-width: 2; }
.elev-minmax { display: flex; flex-direction: column; justify-content: space-between; height: 46px; font-size: 11px; color: #9aa4ae; }
.gps-warn { color: #ffb08a; font-size: 12px; }
.gps-download { display: flex; gap: 6px; align-items: center; color: #9fd3ff; font-size: 12px; }
.gps-download-bar { width: 60px; height: 5px; border-radius: 3px; background: rgba(255, 255, 255, 0.15); overflow: hidden; }
.gps-download-fill { display: block; height: 100%; background: #36a3ff; transition: width 0.2s linear; }
.map-grid polyline { fill: none; stroke-linejoin: round; stroke-linecap: round; }
.map-grid .driven-line-glow { stroke: rgba(0, 210, 255, 0.4); stroke-width: 7; }
.map-grid .driven-line { stroke: #00d2ff; stroke-width: 3.5; opacity: 0.95; }
.map-grid .route-line-casing { stroke: rgba(10, 40, 80, 0.85); stroke-width: 8; }
.map-grid .route-line { stroke: #3aa0ff; stroke-width: 5; }
.map-grid .route-done { stroke: rgba(200, 200, 200, 0.6); stroke-width: 4; }
.odometer-badge {
  display: flex; gap: 6px; align-items: center; padding: 4px 10px;
  background: rgba(0, 180, 240, 0.18); border: 1px solid rgba(0, 210, 255, 0.45);
  border-radius: 4px; font-size: 12px; color: #7fe4ff; font-weight: 500;
  box-shadow: 0 0 10px rgba(0, 210, 255, 0.15);
}
.zoom-level-badge {
  display: flex; gap: 4px; align-items: center; padding: 4px 8px;
  background: rgba(20, 30, 42, 0.85); border: 1px solid rgba(80, 150, 220, 0.35);
  border-radius: 4px; font-size: 11px; color: #a8d0f8;
}
.bottom-left-group { display: flex; gap: 8px; align-items: flex-end; }
.scale-bar-wrap {
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  background: rgba(0, 0, 0, 0.65); padding: 3px 8px; border-radius: 4px; pointer-events: none;
}
.scale-bar {
  height: 4px; border-left: 2px solid #fff; border-right: 2px solid #fff; border-bottom: 2px solid #fff;
}
.scale-label { font-size: 10px; color: #d0d7de; }
.marker.gps { z-index: 3; }
.marker.gps .flag {
  position: absolute; left: -9px; top: -9px; width: 18px; height: 18px; border-radius: 50% 50% 50% 0;
  transform: rotate(-45deg); background: #36a3ff; border: 2px solid #fff; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.6);
}

.map-notice {
  position: absolute; left: 50%; top: 60px; transform: translateX(-50%); z-index: 9;
  padding: 7px 14px; background: rgba(120, 40, 30, 0.85); border-radius: 4px; font-size: 13px;
}
.map-toast {
  position: absolute; left: 50%; bottom: calc(6.5vh + 40px); transform: translateX(-50%); z-index: 13;
  padding: 6px 14px; background: rgba(20, 22, 26, 0.9); border-radius: 4px; font-size: 13px; pointer-events: none;
}
.confirm {
  position: absolute; left: 50%; bottom: 56px; transform: translateX(-50%); z-index: 11; min-width: 300px;
  padding: 12px 16px; background: rgba(15, 17, 20, 0.92); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 6px;
}
.confirm-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.confirm-where { font-size: 13px; opacity: 0.85; }
.confirm-warn { margin-top: 6px; font-size: 12px; color: #ffb27a; }
.confirm-btns { display: flex; gap: 8px; margin-top: 10px; }
/* above the game's own footer bars (player / version bottom left, button hints bottom right: ~5 % of the height,
   scaling with it), which covered the cursor readout and the credits (the user, 2026-10-14) */
.map-bottom {
  position: absolute; left: 10px; right: 10px; bottom: calc(6.5vh + 10px); z-index: 10; display: flex;
  justify-content: space-between; align-items: flex-end; gap: 16px; pointer-events: none; font-size: 11px;
}
.cursor-pos, .attribution {
  padding: 3px 8px; background: rgba(0, 0, 0, 0.65); border-radius: 4px;
}
.cursor-pos { white-space: nowrap; }
.attribution {
  text-align: right; opacity: 0.9; max-width: 60%; pointer-events: auto; cursor: pointer;
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden;
}
.attribution.full, .attribution:hover { -webkit-line-clamp: unset; display: block; }
/* the Places panel open (340 px + its padding and margin): the credits keep left of it */
.map-bottom.beside-panel { right: 376px; }
.map-bottom.beside-panel .attribution { max-width: 100%; }
.map-view { bottom: 0; }
.earth-map.embedded .map-bottom { bottom: 6px; }
</style>
