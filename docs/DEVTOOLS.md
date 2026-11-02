# BeamNG.Earth dev tools (spec, 2026-10-10)

User request: "add some dev tools so I can visualise the LODs, the loading and anything else that
might be useful". In-game, opt-in, zero cost when off. Built after v1.4 M0/M1 (they touch the same
files). Engine-only (imgui + debugDrawer), like the existing `ui.lua` window.

## Pause-menu integration (BeamNG 0.39 "Mods" tab)
The pause menu has a mod API (lua/ge/extensions/ui/pause/actions.lua):
`ui_pause_actions.registerModButton{id, tabId = "mods", label, icon, componentName = <.vue path>}`
puts a button in the shared **Mods** tab, and `registerModTab{id, label, icon}` adds a whole tab of our
own. Mods call it from a Vue mod entry `/ui/ui-vue/mods/<mod>/index.js` (`onLoad` / `onUnload`, via
`import { lua } from "@/bridge"`), e.g. the user's taa_standalone mod (index.js + TaaSettings.vue,
using BngSwitch / BngSlider / BngButton from `@/common/components/base`).

BeamNG.Earth uses that:
- a tab of our own, **"BeamNG.Earth"** (since 2026-10-28, as BeamMP does): one side button per settings page
  (EarthSettingsPage.vue with props {page}), EarthSettingsSide.vue (connection, help) as the tab's second card; the
  same pages as a full page of their own (route beamngEarthSettings, EarthSettings.vue) for the main menu. The
  schema and the shared state are in earthSettingsStore.js;
- **Settings page** (Vue, `/ui/ui-vue/mods/beamngEarth/EarthSettings.vue`): server connection and
  session, rebase distance, view distance mode (real horizon / fixed km) and haze strength, detail
  level / streaming budget (when the M3 controller exists), sky mode (real time / manual) and the time
  offset, imagery source preference, surface physics on/off (v1.5), attribution text; buttons to open
  the Dev Tools and (v1.7) the World Map. Values persist in the mod's own settings file
  (`/settings/beamngEarth.json`), applied through `beamngEarth_core` functions.
- **Dev tools page**: the toggles and presets below (the in-world overlays and the stats/mini-map
  panels themselves stay imgui: they must draw over the 3D view while driving, cheaply).
- The existing imgui window becomes a compact HUD (status line + quick toggles), hidden by default
  once the settings page exists.

## Data credits overlay (user, 2026-10-10)
Small, unobtrusive text in a screen corner (default bottom-left, semi-transparent, ~11 px, no
window chrome; corner, size and on/off in the settings page; on by default because CC BY and
similar licences ask for visible attribution) naming the sources that built what is around the
player right now, e.g.:
  "Imagery: USGS NAIP 2022, Maryland iMAP 2024 · Elevation: Mapterhorn (USGS 3DEP 1 m) ·
   Weather: Open-Meteo.com · Map data © OpenStreetMap contributors"
- The server knows each tile's sources (imagery source and capture year per tile, the DEM tile's
  underlying dataset from the provider's attribution data, weather, OSM/Overture once used). It
  sends a compact `credits` message whenever the set for the tiles within ~2 km of the player (plus
  a short "far: Sentinel-2 cloudless" part) changes; the mod draws it. No per-frame work beyond one
  imgui text draw.
- The full credit list stays in CREDITS.md and on the settings page.
- Built after the dev tools branch is merged (shares ui.lua / settings.lua).

### Every source, short names (the user, 2026-10-18)
- "Show all sources", not only those that ask for credit: besides the tiles' imagery and elevation and the
  weather, `credits.feature_ids` adds what built the world near the player: the map data's datasets
  (the cached cells' provenance within 1.5 km once road tiles are shown: OpenStreetMap, Overture,
  Microsoft's ML buildings, TomTom, Esri, Google, ...; `mapdata.layer_provenance` reads only that entry
  of the cached file, never fetches), ESA WorldCover (surface on), Meta & WRI canopy (trees shown), NOAA
  NCEI (sea tiles), FHWA NBI / Wikidata (a typed bridge record near), OurAirports (a runway near) and
  NGA EGM2008 (real gravity).
- "Trim the text per source": the overlay names each source by a short name (`credits.SHORT`), once,
  grouped: `Imagery: USGS, NYS ITS | Elevation: Mapterhorn | Map: Overture Maps, OpenStreetMap, Microsoft
  | Land cover: ESA WorldCover | Trees: Meta & WRI | Weather: Open-Meteo | Gravity: NGA EGM2008`. The
  full wordings (`credits.full_label`, CREDITS.md) stay the attribution of record on the settings page.
- Imagery's leaf-off share ("leafon", a count of other sources' texels) is no source (it showed raw).

### Built (2026-10-10, merged to main; offline-tested, in-game check pending)
- **Server** `server/credits.py` + small hooks: `dem.Dem.served` records which provider served each
  DEM tile (Mapterhorn / AWS fallback / none; also when netfetch fails over from Mapterhorn to AWS);
  `Server.make_tile` stores every tile's source ids in `TileInfo.credits` (`img:<source>` for each
  imagery source with texels in its texture, from `Imagery.render`'s counts; with `--colormatch s2`
  also the S2 references for a texture the seam feather changed (the counts' `colormatch` share), and a
  stale texture rendered again takes its new sources; `dem:<provider>` for each DEM tile under the
  footprint at the tile's DEM zoom); `Connection` has a `credits.Tracker`. The near set = sources of
  the shown tiles (previous anchors' included) whose footprint is within 2 km of the player (a near
  source stays near while one of its tiles is within 2.2 km: no flip-flopping when the player rocks
  across the radius), plus Open-Meteo once weather went to the mod; the far set = the other shown
  tiles' sources not already near. Nothing is sent before some shown tile's sources are known (the
  weather alone, e.g. while the first tiles load or after a server restart, would replace the mod's
  full attribution line with one name). The wording is CREDITS.md's, word for word (tested). Message `{"type":"credits","v":1,"near","far","ids","farIds"}`, e.g. near "Imagery: USGS
  The National Map | Elevation: Mapterhorn (see mapterhorn.com/attribution) | Weather data by
  Open-Meteo.com", far "Far imagery: EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH
  (Contains modified Copernicus Sentinel data 2023)". Checked after poses and swaps at most once a
  second, sent only on change, re-sent on hello. `earthd --credits on|off` (default on);
  `Server()` has none, so the golden message streams (test_cli_default, test_v131_default) are
  unchanged. Agreement table, server/README.md protocol table.
- **Mod** `beamngEarth/credits.lua` (driven by core.lua): one background-draw-list rectangle
  (black, 30 %) and one `ImDrawList_AddText1` per frame (white, 80 %, the UI font at its own size),
  no window; the text is wrapped (line breaks between words) and measured with `CalcTextSize` only
  when it, the font size or the screen width changes, at 60 % of the screen width. (In-game
  2026-10-11 the first build used `GetFont()` + `AddText2`: the 0.39 binding's `GetFont()` throws
  "bad argument #3", every frame, which flooded the log and stalled the MCP server. Any imgui error
  now turns the overlay off once, logged.) Shown while our world is active (`st.active`), hidden in the pause menu (UI
  state `menu*`, via core's `onUiChangedState`), on loading screens and while the big map is open
  (`freeroam_bigMapMode.bigMapActive()`). Until the first credits message (an older server, or the
  first seconds of a session) it shows the session's attribution line (`hello.attribution`, or the
  built-in one). A new hello clears the previous session's credits.
- **Settings** (settings.lua + EarthSettings.vue, "Interface"): `creditsVisible` (on),
  `creditsCorner` (bottom-left / bottom-right / top-left / top-right; bottom-left), `creditsSize`
  (small 11 px / normal 14 px; small; **no effect yet**: AddText1 draws at the UI font's size; a
  sized font needs a working ImFont handle or a borderless window with SetWindowFontScale), persisted in `/settings/beamngEarth.json`; the
  "Attribution" section shows the credits on screen now.
- Tests: `server/tests/test_credits.py` (wording against the sources' own credits, DEM keys vs what
  `Dem.sample` reads, `Dem.served`, per-tile sources in `make_tile`, the near / far split, change-only
  and throttled sending, re-send on hello, an in-process session, the default server sending none),
  `test_protocol_agreement.py` (the table, both sides), luajit `mod/tests/test_credits.lua`,
  `test_core_stubbed.lua`.
- Measured offline: a tile's sources cost ~0.12 ms in `make_tile` (a 3 x 3 point DEM-key lookup;
  tile builds take tens of ms); a credits check over 2000 shown tiles ~0.9 ms (at most one a second);
  a message is ~0.3-0.6 KB. Mod: `credits.draw` allocates nothing per frame (0 bytes over 10000
  frames, counted in the interpreter, test_credits.lua; the attribution fallback is a constant string
  from farfield's counters, not a `farfield.stats()` table), ~0.3 us of Lua per frame in the
  interpreter with a stub imgui, plus the two draw-list calls.
- Deviations: the far part uses the same wording as the near one (EOX asks for its exact wording,
  so "far: Sentinel-2 cloudless" became "Far imagery: EOxCloudless ... (Contains modified Copernicus
  Sentinel data 2023)"). No capture year for USGS (ImageryOnly tiles carry no date) and no
  underlying dataset for Mapterhorn (its tiles do not say which survey they come from): the credit
  names Mapterhorn with its attribution page. OSM / Overture are not used yet, so not credited.
  Shown whenever our world is active, also in another level after a teleport (our tiles are on
  screen there too).
- Still to check in-game: the text size and readability on bright and dark ground, that the
  background draw list sits above the 3D view and below the game's own HTML UI as expected, the
  UI state names of the 0.39 pause menu (`menu*` assumed from the stock Lua) and the big map.

## Connection banner (docs/ROADMAP.md "Robustness" 1; built offline 2026-10-11, branch feature/robustness)
One or two lines at the top centre of the screen (`beamngEarth/banner.lua`, drawn by core.lua) that
say what is wrong with the world server and what the player can do; hidden while all is well, in the
pause menu and the big map (like the credits overlay), and outside our world (our level, or another
level after a console teleport).
- States: `starting` ("looking for the BeamNG.Earth server on this PC", the first 6 s, or connected and
  waiting for the hello), `noserver` ("BeamNG.Earth server not running on this PC - start BeamNG.Earth with
  its launcher (or:  python3 server/earthd.py)", red), `lost` ("Lost the BeamNG.Earth server on this PC -
  reconnecting (attempt n). Is its launcher window still open?", amber; n = connection attempts since the
  loss, net.lua), `offline` ("Server OK, internet data sources unreachable - using cached data (areas never
  visited have no terrain)", from the server's status note), `degraded` ("Server OK, some internet data
  sources not answering: <the server's note>"), `recovered` ("BeamNG.Earth server connected", green, 4 s
  after a lost or missing server came back; not after a normal start), `ok` (hidden). Every line says
  which side is at fault: the program on this PC, or the internet data sources behind it (2026-10-17).
- The edge guard (`edgeguard.lua`, "Robustness" 2) adds a second line and raises the level: "Near the
  edge of the loaded ground: the car will be held there until the world server is back" (amber),
  "Held at the edge of the loaded ground until the world server is back" (red); "... answers terrain
  requests again" when the server is connected but cannot move the block yet (terrain requests
  unanswered, the resync after a reconnect not committed, or only the level-start flat block).
- Cost: no window; while nothing changes, no allocation (the text is rebuilt only when an input
  changed: field-by-field compare), one background draw-list rectangle and one `AddText1` (the UI
  font: `GetFont()` throws in 0.39), measured only when the text or the viewport width changes. An
  imgui error turns it off once (logged), never every frame.
- Settings page: the same line in a bar under the header (`status.banner` = `core.banner()`:
  `{ id, level, text, guard }`; text "BeamNG.Earth server connected" while all is well), coloured by level
  with a left border (a plain div, nothing focusable; no glyphs).
- Tests: `mod/tests/test_banner.lua` (states, the note parser, the "connected" line, drawing,
  hiding, the error path, no per-frame allocation), `mod/tests/test_core_robustness.lua` (the whole
  path in core with a server that is down, comes, dies and restarts).
- In-game check pending: readability and placement at 1080p / 4K, hidden in the pause menu and the
  big map, the settings page bar.

## Entry point
- A **"Dev"** collapsible section / tab in the existing BeamNG.Earth window (ui.lua), plus a console
  API `beamngEarth_dev.set('<overlay>', true|false)` and `beamngEarth_dev.preset('lod'|'loading'|'off')`.
- A key binding (default unbound; user picks) to toggle the last-used preset.
- State persists per session only (no settings file writes).
- When every overlay is off: no per-frame work at all (the extension returns at the top of its hooks).

## Overlays (each a checkbox)
1. **LOD tint**: every far tile tinted by level (L-2/L-1 green = matches physics, L0 cyan, L1 blue,
   L2 purple, L3+ orange -> red), so the quadtree is visible on the ground and the horizon.
   Implementation: per-tile Material `diffuseColor` multiply (the tile materials are ours, one per
   tile), applied/restored in time-sliced batches (governor unit kind `devTint`, ~50 materials per
   frame) so toggling never hitches. Tiles created while it is on get the tint at creation.
2. **Tile outlines**: debugDrawer outlines of tile footprints at ground height (colour by level),
   only for tiles within N km (default 3 km; slider), so the cost stays bounded (<= ~150 boxes).
   States drawn differently: shown, hidden-loaded (waiting in a swap group), in crossover, old anchor
   (after a rebase), doomed.
3. **Loading heat**: queued / importing tiles highlighted (outline flashes), the next ~10 imports
   numbered in order, so priority problems (near tiles waiting behind far ones) are visible.
4. **Physics block**: the TerrainBlock's collidable square drawn (and the pending/next block dashed),
   with the terrain-follow commit thresholds; optional **"show physics terrain"** that turns the
   invisible TerrainBlock's rendering on (rendered with a checker material) to compare visuals vs
   collision.
5. **Rings**: near radii (150/400 m today), rebase circle (15 km) and the prefetch circle, the
   far-field radius / horizon distance (M1h), drawn as ground circles.
6. **Under-car probe**: a vertical line at the car: physics ground height vs visible tile height and
   the tile level under the car; turns red when the visible tile is not the exact level or differs by
   > 5 cm (the `exactMiss` metric of V1_4_SPEC).

## Panels (imgui)
- **Stats**: fps, frame ms p50/p95/max (last 5 s), sim ratio, governor budget and per-kind estimates,
  imports/s, deletes/s, queue length, waiting groups, crossovers running, live/old/doomed objects,
  materials, pops, failed, exactMiss count, server backlog, server status line, anchor, distance to
  rebase, last follow / rebase commit ms.
- **Graphs**: rolling 60 s plots (imgui PlotLines) of frame ms, queue length, imports/s, speed.
- **Mini-map**: top-down imgui draw-list view of the tiles around the player (zoom 1-80 km), coloured
  by level and state, with the car arrow, physics block, rings and the server's desired-but-not-shown
  tiles. Click a tile -> its key, level, state, texture size, build/import ms (from the server log
  fields the mod already has).
- **Controls**: freeze LOD (stop applying swaps, for inspecting), hide far tiles beyond X km, force
  rebase now, rebase distance slider (exists), teleport (exists), reload far field, toggle sky mode
  (real/manual) and time of day, motion-blur hold test, screenshot-with-overlays.
- **Log**: last 50 mod events (commits, rebases, swaps blocked at the cap, failures) with timestamps.

## Cost rules
- Off: nothing per frame. On: <= 0.3 ms per frame with every overlay (budget checked with perfmon,
  the panel shows the dev tools' own cost).
- No loops over all tiles every frame: outlines and the mini-map iterate a spatial index updated on
  tile add/remove, limited to the view radius; stats come from counters.
- Material tint changes are time-sliced; never a full reload in one frame.

## Tests
- luajit: the dev module with stubs (toggles, zero work when off, tint batching restores every
  material exactly, spatial index add/remove/query).
- In-game (when the user is not driving): toggle each overlay at the Kern River spot, screenshot,
  perfmon cost per overlay.

## Built (2026-10-10, branch feature/devtools; offline-tested, in-game checks pending)
- `beamngEarth/dev.lua` (extension `beamngEarth_dev`, loaded by the modScript, everything off):
  overlays tint / outlines / loading / physics / physicsTerrain / rings / probe and panels
  stats / graphs / map / controls / log in one imgui window "BeamNG.Earth Dev"; presets off, lod,
  loading, physics, all; `toggleLast()` on an input action ("BeamNG.Earth: toggle dev overlays",
  Options > Controls > Debug, unbound). With everything off `onUpdate` returns at its first line
  and farfield has no watcher (test_dev "off: the hook touches nothing").
- `beamngEarth/tileindex.lua`: per-level hash grids (cell = max(tile, 512 m)); a query visits
  min(cells in range, occupied cells) per level (<= ~900 cells for 3 km, any tile count).
- farfield.lua hooks (small, for the merge): `setWatcher` (onAdd in `spawn`, onRemove in
  `deleteById`, onReset after `transform` and in `forget`), `setFrozen` (swap groups wait),
  `eachTile`, `queuedTiles`, `counters`, `anchorId`, and `inst.importMs`.
- Tint: Material `diffuseColor` slot 0 + `flush()` + `reload()` (as stock trackBuilder), the
  original value kept per material (an empty field restores as '1 1 1 1'); at most 50 materials
  and 0.25 ms per frame, never in a governor heavy or quiet frame. Deviation from the spec: not a
  governor unit kind - the governor's budget ramps to 0 whenever streaming is idle, so a tint
  through it would stall on a parked car; the tint keeps its own slice instead.
- Probe: the visible height is the physics ground + the tile's crossover offset when the tile is
  the exact level (exact tiles match the lattice bit for bit); otherwise only the level is shown.
- Not done: the checker material on the shown physics terrain (it renders with its own dirt
  material), "hide far tiles beyond X km" (it would fight farfield's own hide state), the motion
  blur hold test and screenshot-with-overlays buttons, "reload far field" (Controls has
  reconnect / resync instead).
- Pause menu: `ui/ui-vue/mods/beamngEarth/index.js` registers the button "BeamNG.Earth" (id
  `beamngEarth-settings`, icon globe) in the shared Mods tab and opens `EarthSettings.vue`
  (structure and bridge pattern of taa_standalone: BngSwitch / BngSlider / BngButton, custom
  dropdown, `bngApi.engineLua` with callbacks). `beamngEarth/settings.lua` (extension
  `beamngEarth_settings`) persists rebase distance, view distance mode / fixed km, haze
  (placeholder), sky mode, clouds, weather mode (real / manual, `beamngEarth_weather.setMode`; a
  saved `auto` from before 2026-10-11 loads as real without a warning, `settings.MIGRATE`),
  real gravity (on, `setGravityEnabled`), HUD, dev radii in `/settings/beamngEarth.json`; every default
  is today's behaviour, the view distance is applied only in our level and only when it is not
  the default. Weather and Sky stay on Real and switch themselves to Manual when the weather or the
  time is changed in the game (the pause menu, another mod; ARCHITECTURE.md "Real weather"):
  weather.lua / sky.lua save `manual` through `settings.set` and show one toast; a short note under
  both dropdowns says so. Choosing Real weather is always applied (even when it already is: it clears
  a gravity change made in the game); after a Lua reload both read the saved mode back. The page's 2 s poll (`uiState`) refreshes the values too, so the dropdown
  shows Manual within 2 s (a poll answered within 1 s of the page's own write is not applied to the
  values, so it cannot flip a choice back). The dev overlays stay per session. The Connection section also shows the server
  features as status lines: the weather line, the gravity in use and its source, and the imagery
  de-shadowing mode (`hello.deshadow`, server-side only; "off" without it).
- HUD: once the page exists (index.js says so, or the pause menu lists the button) the imgui
  window is a compact status HUD, hidden at level start unless "Status HUD" is on; "Full window"
  brings the old window back for the session. Without the page nothing changes.
- Places (quick win 1, 2026-10-11, branch feature/places; ARCHITECTURE.md "Saved places"): a
  "Places" section (second, closed by default): "Save current place" (name field, the position
  shown; Save off until there is one), a "Latitude, longitude" field (Google Maps' "lat, lon" paste,
  N/S/E/W, degrees-minutes-seconds) with Go and Save (under the name above), the saved places with
  Go / Rename (inline field, OK / Cancel) / Delete (the button reads "Confirm" for 4 s), and the
  recent places (name or lat/lon, "level start" tag) with Go / Save (Go sends the row's lat/lon
  with its index, so a double click cannot land on the row that moved up). Go is disabled with a note
  ("Companion server not connected", "Load the BeamNG.Earth level first") when it cannot teleport; errors and
  "Going to ..." show in a line at the top. BngInput fields (`v-bng-text-input`, so typing does not
  drive the car), BngButton text buttons, no glyphs, no ::before / ::after. The 2 s poll also
  refreshes the lists (`uiPlaces`), so a teleport from the console or the level start shows up.
- Download cache (2026-10-11, branch feature/cache-cap; server `cache.py`, docs/ROADMAP.md "Caching"):
  a "Download cache" section on the page with the usage line ("1.2 GB of 20 GB, 34877 files"; "counting
  files..." while the server's start-up walk runs, "no information from the server" without a
  server or with one that has no cache), the size cap (select: 5, 10, 20, 50 GB, Unlimited; setting
  `cacheCapGb`, default 20 = the server's default) and a "Clear cache" button that asks in the page
  first (a text with Clear / Cancel buttons, no browser dialog), then shows "Clearing..." and "Freed
  1.1 GB" (or "No answer from the server" after 2 min). The cap goes to the server after every `hello`
  and on change (`core.cacheSetCap`; the server keeps it for its next start); the page's 2 s poll asks
  the server for fresh numbers every 5th poll (`core.cacheQuery`, `settings.CACHE_POLL`); the server's
  `cache` message is kept in core.lua (`core.cacheState()`, outliving levels, forgotten on a disconnect
  or a new hello). Bridge: `beamngEarth_settings.uiCacheClear()` -> `{ ok, error }` (error "server not connected", or
  "the server has no download cache" when it sent no `cache` after its hello: no answer would come);
  `uiState().status.cache` = `{ line, clears, clearedBytes, clearedText }`.

## Visual benchmark (tools/benchmark.py; built offline 2026-10-11, in-game run pending)
ROADMAP "Automated visual checks" and "Principle: improvements must generalise": every tuning
change is checked on a fixed, diverse set of places with before/after screenshots and numbers, plus
a held-out set that is not looked at while tuning.

**The set** is `docs/benchmarks.json`: docs/PLACES.md's table (group `places`, 6), its photo
benchmark (group `photo`: the Whitney summit view south with the photo's camera, 10:00 PDT on the
render's date) and a global group of 22 (17 for tuning: Lauterbrunnen, Amsterdam, Erg Chebbi, Serengeti,
Lagos Island, Everest Base Camp, Shibuya, the Gobi, Punjab farmland, Uluru, Milford Sound,
Salar de Uyuni, Rio, the Amazon, Moraine Lake, Kilauea, the Greenland ice edge; held out: Geiranger,
Cape Town, the Dead Sea, Mexico City, McMurdo), tagged by continent, biome, data-rich / data-poor,
flat / steep, city / wilderness, farmland (the tuning set has farmland and a data-poor city of its
own; a test checks the coverage). Each place has a heading and one or more views (default: `ground`,
1.7 m above the ground looking along the heading, and `aerial`, 250 m up and 600 m back, pitched
-18 degrees), a fixed date and hour (default 2026-09-22, the equinox, at 10:00 local mean solar
time: UTC offset = lon / 15, so the sun stands the same everywhere; a place may give its own
`utcOffset`, `date`, `hour`) and a weather (`clear`: no clouds, 60 km visibility; or `keep`). Once
runs exist, places and cameras stay put (shots are paired by place id and view name); add new ones.
A new place in docs/PLACES.md goes into the set too (`test_benchmark_tool` checks the table).

**Use** (the game in our level with the server running, the car parked; ~1-2 min per place):

    python3 tools/benchmark.py plan [--set tuning|heldout|all] [--places a,b]   # the shots, no game
    python3 tools/benchmark.py check                                           # preconditions, read only
    python3 tools/benchmark.py run --label before-feather                      # tuning set (default)
    ... make the change, deploy, restart what it needs ...
    python3 tools/benchmark.py run --label after-feather
    python3 tools/benchmark.py compare docs/research/benchmarks/2026-10-12-before-feather \
                                       docs/research/benchmarks/2026-10-12-after-feather
    python3 tools/benchmark.py run --label after-feather-heldout --set heldout # only to accept it

A run teleports to each place (`beamngEarth_core.teleport`) and polls the core status once a second
until it is ready on two polls in a row: the new anchor is committed, `far_done` came, nothing is
queued, no swap group waits, no transition runs and (mesh mode) the tile under the car is the exact
level of the current anchor (`--no-exact` skips that; `--wait-backlog` also waits for the server's
backlog, which is only fresh with `earthd --status-interval 1`; `--server-log <earthd log>` also
waits until no colour-match / de-shadow region line appeared for `--region-quiet` s). A place whose
anchor is committed but not ready after `--timeout` (300 s) is shot anyway and marked. A teleport
the server answers with an error (the request is gone, the anchor unchanged) fails the place at once,
and one not committed by the timeout fails it too: neither takes shots, which would show the
previous place under this one's id. A view whose screenshot is never written fails that view only.
Once ready, the inputs are zeroed again (the teleport reset the vehicle). Then the sky goes to manual with
the place's fixed time (clock stopped) and the weather to manual with the clear preset; per view the
free camera goes there, waits `--settle` (3 s) and until nothing loads, measures the frame times for
`--fps-seconds` (5 s, `beamngEarth_perfmon`, loaded for the run if it was not) and takes a
screenshot (JPG; `--png` for lossless, ~15 MB each at 4K).

Recorded per place (run.json): load time to the anchor commit, far_done, the exact ground and ready;
tiles (live, covered, materials, and imports / failed / textured / pops during the load); exactMiss
frames while loading and after; the status line; per view the camera as placed (`groundFrom`: the
static-collision ray, or the player's ground beyond the physics block), fps, avg / p50 / p95 / p99 /
max ms, frames over 33 / 50 / 100 ms, and whether the game window had the focus (in the background
the game caps itself at 30 fps: keep it focused; compare gives no frame-time verdicts for a shot
taken without focus). Also the git commit and dirty flag of this checkout, the
set's digest and the options. The run folder is `docs/research/benchmarks/<date>-<label>/`
(git-ignored like every docs/research subfolder; `-2`, `-3` if it exists): the images, `run.json`
(rewritten after every place, so a crashed run keeps what it had) and `state.json`.

**Safety.** It refuses to start while the player vehicle moves faster than 1 m/s (`--force`
overrides), and stops before the next teleport when the car moves faster than that on two polls a
second apart (the user took it back; not with `--force`): then the restore leaves the car alone (no
teleport home, no inputs) and run.json says `aborted`. It zeroes throttle / brake / steering / clutch
and sets the parking brake (the car is teleported onto slopes). `state.json` (what the run found:
camera, sky mode and TimeOfDay fields, weather mode, cloud coverage and fog, perfmon, the player's
lat/lon) is written before anything
changes. At the end, and on any error, Ctrl-C or SIGTERM (finally), it restores the weather and sky
(values, then their modes; 'real' re-syncs itself), unloads perfmon if it loaded it, teleports the
player back to where it started (waits for the anchor commit, `--home-timeout`), zeroes the inputs
and gives the camera back last (the game camera; a user's free camera at the same place relative
to the car), and the free camera's own zoom (its FOV and default, which the views set and which
outlives the switch back) as it was. Each restore step runs even if an earlier one failed, or was
interrupted by a second Ctrl-C; the errors are printed and kept in run.json. An MCP request that
times out (the game stalled, e.g. compiling shaders at a new place) is sent once more (every call
is idempotent); a game that refuses the connection is not retried. After a hard kill: `python3 tools/benchmark.py restore <run folder>`. Sky and weather
go to manual through their own `setMode` (not saved to the settings file) and the fields are written
directly, so no `onEnvironmentChanged` hook fires (nothing switches the user's setting to Manual).
Screenshots stay in the game's `screenshots/mcp/` too (copied, like the other tools).

**Compare** pairs the shots by place and view and writes `<out>/index.html` (A | B | heat map of
|delta luminance| x4 per shot, thumbnails 960 px) and `compare.json` (default out:
`docs/research/benchmarks/compare-<A>-vs-<B>/`). Per image, at `--metric-width` px wide (1280;
both resized the same way, B to A's size if they differ): MAD (mean |A - B| over RGB, 0-255), SSIM
(mean over 7 x 7 box windows on luminance, Wang et al. 2004 constants; 1 = identical) and the share
of pixels whose luminance moved by more than 16. A shot is "changed" when its SSIM is below
`--ssim-flag` (0.98: a change all over) or more than `--changed-flag` (1 %) of its pixels changed
(a local change the mean hides: a 20 x 40 px object moved 20 px in a 300 x 200 image is SSIM 0.980,
1.3 % changed).
Numbers: per place load / tiles / exactMiss, per view fps / p50 / p95 / p99 / max / over50, as A,
B, delta and %; "worse" / "better" when they moved beyond the tolerance (load +-20 % and 2 s, fps
5 % and 1 fps, p95 10 % and 1 ms, max 25 % and 10 ms, any new failed tile or exactMiss frame after
ready); per place also `ok` and `load.timedOut` (a place that failed or never got ready in B but
did in A is "worse", although it has no load time to compare). A shot whose camera differs between
the runs (the set changed it; run.json keeps each shot's camera) is marked "camera differs", and the
page says so when the set's digest differs. Cost: ~0.5 s per 4K pair at 1280 px (3 s at full size). **Noise floor:** two runs of the
same build differ (TAA, animated water and grass, streaming order, JPG): run the same build twice
first and set `--ssim-flag` just under the lowest SSIM and `--changed-flag` just over the highest
changed share that comparison shows.

GE side: `tools/benchmark.lua` (sent through MCP `run_lua`, global `BNGE_BENCH`: info, zeroInputs,
teleport, progress, setTime, setWeather, view, perfStart / perfRead, restore). No mod file and no
mod change: it uses `beamngEarth_core.teleport` / `_state()`, `farfield.stats()`, the sky and weather
modules' `setMode` / `getMode`, `weather.fogSigma` / `fogDensityFor`, `beamngEarth_perfmon`, the
stock `commands.setFreeCamera` / `setGameCamera`, `core_camera.setPosition` / `setRotation` /
`setFOV` and the MCP `screenshot` tool. A Lua reload during a run is survived (the chunk is sent
again once).

Tests: `server/tests/test_benchmark_tool.py` (the set against docs/PLACES.md, its coverage and
validation, the plan, readiness and safety rules, whole runs against a fake game: numbers, files,
the restore order and that it runs after an MCP error, Ctrl-C (also during the restore), a refused
or failed home teleport, a timed-out place, a teleport answered with an error or never committed (no
shots), a lost screenshot, the user driving off mid-run, MCP timeouts (retried once; transport errors
mapped), a Lua reload; SSIM against a brute-force implementation, the compare report) and luajit
`mod/tests/test_benchmark.lua` (the chunk with stubs and the real sky / weather / geodesy modules:
every saved value and mode comes back, the free camera relative to the moved car and its own zoom,
a failing part).

Not done yet: the first in-game run (the readiness rules, whether a vehicle reset releases the
parking brake, the screenshot path, the free camera at
`agl` beyond the physics block, the time it takes, the noise floor are all unmeasured), hiding the
credits overlay and HUD for the shots (they are in every image, the same in both runs), a per-view
sun / weather override, matching the real photo itself (the photo benchmark is compared run to run;
the photo stays on Commons).

## Reports (GitHub issues: map / performance / bug / idea; `report.lua`, 2026-10-13, kinds 2026-10-14)
- **Ctrl+Shift+E:** captures the moment at once: a screenshot, the location (lat/lon, anchor), the
  vehicle and camera, the last ~10 s of frame times, the engine's per-system frame metrics, the fps limit
  and VRAM, and the mod's whole status (far tiles, trees, sea...). Then a window: the kind (Map issue /
  Performance issue / Bug / Idea / suggestion), a title and a description; "Save report", for a
  performance issue "Save + record 30 s profile", or "Cancel". After saving a small window shows the
  folder with "Open folder" and "Open GitHub issue page".
- **Ctrl+Shift+Y:** the **performance profiler** (was "benchmark"): 30 s of frame times while driving,
  one sample per second (position, speed, far tiles, trees, engine metrics) and every frame over 50 ms
  with its position, then the statistics (fps, p50 / p95 / p99, worst, frames over 33 / 50 / 100 ms).
  The report is saved at once, then the window opens (kind Performance issue) for the title and text.
- Saved in `<user folder>/beamngearth/reports/<date>_<time>_<report|profile>_<n>/` (older:
  `_note_` / `_bench_`): report.json, screenshot.jpg, **hardware.json** (OS, CPU, RAM, GPU, graphics API,
  video mode, BeamNG version, the key graphics settings, other mods, mod version, the server's hello
  options: `report.collectHardware()`, every engine call guarded) and **issue.md** (the GitHub issue:
  `issuefmt.lua`, title on line 1). One line per report in `reports/index.jsonl`. The folder name is
  lower case: the game's file system lowercases new folders.
- `tools/reports.py` lists them; `show N` prints one; `issue [N]` edits issue.md ($EDITOR, else the
  system editor) and opens the GitHub new-issue page (title, body, label map / performance / bug /
  enhancement; body cut below 7000 URL characters) and the folder. Issue forms: `.github/ISSUE_TEMPLATE/`.

