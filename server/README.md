# earthd — BeamNG.Earth companion server (protocol 4)

Implements the server side of [`docs/V1_3_SPEC.md`](../docs/V1_3_SPEC.md) §A, §B, §D, §E, §F over
[`docs/V1_2_DESIGN.md`](../docs/V1_2_DESIGN.md) phases 1a, 1b, 1c (server bits), 2 and 3 (on top of
[`docs/V1_1_SPEC.md`](../docs/V1_1_SPEC.md) and [`docs/V1_SPEC.md`](../docs/V1_SPEC.md) where later
versions do not change them). It turns real-world elevation data into 1024² TerrainBlock heightmaps
on a world-locked 2 m lattice that follow the player (physics only in the default `mesh`
near-visual mode), and a dynamic-LOD render-only tile field of binary `.cdae` tiles, each with its
own satellite texture, streamed as swap groups, for the `beamng_earth` mod. In mesh mode the tiles
go down to 64 m (L-2) on the terrain block's own physics lattice, so they draw the ground under
the car too.

## Run

```sh
python3 server/earthd.py                    # listens on 127.0.0.1:47613
python3 server/earthd.py --port 47613 --user-dir ~/.local/share/BeamNG/BeamNG.drive/current \
                         --cache-dir ~/.cache/beamng-earth --workers 3 --far-format cdae -v
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `47613` | TCP port on 127.0.0.1 (`0` picks a free port and prints it) |
| `--user-dir` | `~/.local/share/BeamNG/BeamNG.drive/current` | BeamNG user folder. Output goes to `<user-dir>/temp/beamngEarth/<sessionId>/`, which the game sees as `/temp/beamngEarth/<sessionId>/` |
| `--cache-dir` | `~/.cache/beamng-earth` | Download cache root. DEM tiles: `<cache-dir>/dem/<source>/<z>/<x>/<y>.<ext>`; per-host request counters of each UTC day: `<cache-dir>/netstats/<YYYY-MM-DD>.json`; the layout version `VERSION` and the last-use index `index.tsv` (see "Download cache") |
| `--cache-cap` | the last cap set on the mod's page, else `20` | Size cap of the download cache in GB (`cache.py`; 0 = unlimited). Over it, a background thread removes the least recently used files down to 90 % (at most every 3 min); never what this session used within the hour, the gravity model or `netstats/`. The mod's settings page can change it (`cache` messages); the server keeps that cap in its index for the next start, and this flag overrides it |
| `--workers` | `3` | Far-tile worker threads. Keep ≤ 3 so tile building cannot starve the game (§4.5) |
| `--far-format` | `cdae` | `cdae`: server-written binary shapes (default). `dae`: Collada fallback (the engine then compiles each tile itself, ~5× slower to load) |
| `--lod-k` | per level | One K for every level. Default: the layout's K table, K(L1) = K(L2) = 3, K(L3+) = 2.5 with 32 cells (§3); K' = {L1 0.75, L2 1.5, L3 1.5, L4+ 1.25} with 64 |
| `--hysteresis` | `0.25` | A refined node stays refined until d ≥ (1 + h) × its switch distance (v1.3 §D: band ≥ 25 %) |
| `--far-radius` | `80000` | Far field half-size (m) around the player |
| `--nice` | `10` | Niceness increment of the whole process (0 = leave as is) |
| `--imagery` | `usgs,eox2023,eox2016` | Satellite imagery source chain (first source with data wins, per source tile), or `off` for the v1.2 palette look (tiles without `material`) |
| `--near-visual` | `mesh` | `mesh`: the ground near the player is drawn by L-2 (64 m) / L-1 (128 m) tiles and the TerrainBlock is physics only; `terrain`: the v1.2 look (the block is drawn, L0 tiles under it are covered) |
| `--cells` | `32` | Tile cells per edge (docs/V1_4_SPEC.md §3.1, M1). `32`: the v1.3.1 layout, FORMAT 3. `64` (mesh mode only): 8704-tri tiles, FORMAT 4, exact level L-1 (128 m, the 2 m lattice), L0 within 400 m, `hello.cells`, `far_tile.tex`, tile cache 2500. The default stays 32 until the in-game probe P8 passes |
| `--block` | `512` (mesh mode), `1024` (terrain mode) | Terrain block samples per edge (docs/V1_4_SPEC.md §3.5, M4). `1024`: the v1.3.1 block (256 m centre grid). `512` (mesh mode only): a 512² physics block, collidable [c−512, c+510], centres on a 128 m grid, `hello.blockSize: 512`, `terrain.size: 512`, the prefetch predicts the mod's 512 follow table (prepare at 128 m, lead 2 s, fast mode above 150 m/s: lead 3 s, prepare test 0.75 s ahead). The default in mesh mode since the in-game probes P2, P7 and P11 passed (`tools/m4_probes.py`, 2026-10-10); `Server()` keeps 1024 |
| `--tex` | per layout | Texture edge (px) per level from the finest, the last value repeating. Default with 64 cells `256,256,256,512` (L-1, L0, L1 256 px; L2+ 512 px); with 32 cells from `--tex-near` / `--tex-far` |
| `--tex-near` | `256` | 32 cells: texture edge (px) of the two finest levels (mesh: L-2, L-1; terrain: L0, L1) |
| `--tex-far` | `256` | 32 cells: texture edge (px) of every other level |
| `--tone` | `1.0` | Strength of the imagery tone curve (0 = imagery as downloaded) |
| `--deshadow` | `off` | Undo the capture sun's terrain shading in the USGS tiles (`deshadow.py`): `lambert` = slope shading only, `full` = slopes and the terrain's cast shadows (relit texels then get half the tone curve's shadow lift). `off` = textures byte-identical to before |
| `--colormatch` | `s2` when the imagery chain has USGS (since 2026-10-11), else `off` | Remove the colour steps between survey flights in the USGS tiles, e.g. at state lines (`colormatch.py`, the "seam feather"): `s2` = detected seams only, against Sentinel-2 (EOX) as the reference, each side moved half way and faded out over ~1.5 km; gain exactly 1 elsewhere. `off` = textures byte-identical to before |
| `--retex-rate` | `5` | With `--colormatch` / `--deshadow`: a tile on screen whose texture was rendered before its region was ready is rendered again (under its next revision's name) once the region is, and the mod gets `far_retex`; at most this many starts per second, one at a time on the tile pool, nearest the player first. `0`: no live swap (the tile is re-rendered when it is sent again, e.g. after a teleport) |
| `--near-radii` | `150,400` | Mesh mode: everything within 150 m of the player is the exact level, within 400 m at least the next one (32 cells: L-2, L-1; 64 cells: L-1, L0) |
| `--horizon` / `--no-horizon` | on in mesh mode (off in terrain mode) | The far field out to the line-of-sight horizon (docs/V1_4_SPEC.md §3.6 "Radius", M1h; mesh mode only). The eye's height above sea level comes from the pose's camera z; per anchor a coarse DEM pass (z6 block maxima on 16 km cells, its own worker) gives each L6-L10 node its highest terrain; the selection is a disc of radius clamp(sqrt(2R'h_eye) + sqrt(2R'zmax_region), 80 km, 1500 km), R' = 6371 km / 0.87, with L10 (262 km) roots, and beyond 80 km every node past sqrt(2R'h_eye) + sqrt(2R'zmax_node) is dropped. 256 px textures at L8/L9, 128 px at L10. Sends `hello.horizon` and `status.radius`. Off until the in-game probe P12 (extended) passes |
| `--status-interval` | `0` | Re-send the last status with the current far-tile backlog every S s while it changes (0 = v1.3.1: on events only; `tools/drive.py` runs use 1) |
| `--weather` | `on` | Real weather at the player from Open-Meteo.com (keyless, non-commercial; `weather` messages, see below). `off`: no requests, no message |
| `--gravity` | `on` | Real gravity at the player (`gravity.py`: WGS84 normal + EGM2008 anomaly from `<cache-dir>/gravity/`; `gravity` messages, see below). Normal gravity while EGM2008 is not cached yet; it then downloads once (74 MB) in the background. `off`: no message (the mod falls back to normal gravity by latitude) |
| `--offline` | off | No network at all (`netfetch.Fetcher(offline=True)`): elevation and imagery come from the disk cache only; an uncached imagery tile leaves its far tile with the palette material, uncached terrain fails (`terrain_error`), no weather fetch, no EGM2008 download. Every `status` text then ends in "Offline - using cached data" |
| `--roads` | `on` | Overture roads near the player (`roads.py`): 256 m tiles within ~640 m, announced as `roads_tile` (a JSON file of DecalRoad chunks in the session folder) and dropped with `roads_drop`; the roads are also painted into the surface masks. |
| `--background-duty` | `0.25` | Share of the time the background imagery analyses (colour seams `colormatch.py`, de-shadow `deshadow.py`) may run (`pacer.py`): they sleep between steps. Their memory traffic made the game's rendering up to 2x slower in ~15 s bursts while the player explored (in-game 2026-10-13: one numpy thread streaming large arrays made the shadow pass 5-6x slower; a pure CPU loop changed nothing). Their results are cached per region, so pacing only delays them. `1`: unpaced. |
| `--road-processes` | `2` | Processes that build the road tiles (`blockworker.roads_tile`, with `--block-process on`); `0` = threads in the server. In the server their Python held its GIL for most of its busy time and the block photos waited (photo stage p90 ~2.5 s, max ~4.6 s; with processes 0.6 / 0.75 s). The cost: the server keeps up better and the game ran ~7 % lower fps on the same drive (Fells Point, 58 m/s: 101 -> 94). |
| `--airports` | `off` | Runways graded into the physics block, the far tiles and the road tiles (`airportgrade.py` over OurAirports + `runwaygeom.py`; the data is downloaded once, ~5 MB, and refreshed weekly). KBWI 10/28: the ground along the centreline 3x smoother than the DEM, within 16 cm of it; a plan costs ~0.14 s per airport and process, ~0.05 s per block over it. Off until the planes / markings check. |
| `--detail-ring` | `128` | Detail mode: metres of L-1 tiles around the drawn block (its own 2 m lattice and NAIP texels), so its edge meets them; `0` = none (~6 % more fps, a step and coarse tiles at the edge). |
| `--sea` | `off` | Sea water (`seacarve.py`, `seatiles.py`): the ground carved under Overture's `ocean` polygons (1 : 6.7 from the shore to 6 m), and `sea` messages with the WaterBlock tiles the mod draws around the player (512 m near to 8192 m far, out to 8 km, each at the sea level of its centre and tilted with the Earth's curve). |
| `--trees` | `off` | Trees, bushes and rocks (`vegetation.py`): tree tops from the 1.2 m canopy height model, bushes and rocks by land cover, sent in 256 m tiles (`veg_tile`) and drawn by the mod as Forest items of stock East Coast USA shapes. A tile's JSON is `{groups, items: [[group, x, y, z, yaw, scale, height, rank], ...]}`, or for a mod whose hello has `vegFlat` `{groups, stride: 8, flat: [group, x, ..., rank, group, ...]}` (`vegetation.flat_json`, `veg_<a>_<tx>_<ty>.flat.json`): one array of numbers, no table per item in the game's Lua, which every garbage collection walks (trees.lua). |
| `--buildings` | `off` | 3D buildings (`buildings.py`) from the Overture footprints with the road tiles, heights and roofs predicted where the data has none, stock window sheets; also street lights and utility poles (`furniture.py`). Needs `--roads on`. |
| `--gas-stations` | `off` | Gas stations with refuelling (`gasstations.py`; on in `start.py`): fuel stations from Overture places (the `gas_station` / `fueling_station` categories, or a fuel brand's Wikidata id; a convenience brand such as Sheetz or Wawa only with a canopy or a gas-station pin), pump islands under the mapped canopy or between the forecourt's mapped drive lanes (the canopy outline is left out of the buildings) in the region's style, or a pump pair, sent with the road tile as `fuel`; the mod spawns them with the stock refuel prompt. Needs `--roads on`. |
| `--sidewalks` | `on` | Raised sidewalks (curbs and slabs, `sidewalks.py`) with the road tiles; `off`: the road tiles carry no `walk` mesh (the ground keeps its flat sidewalk band). For comparisons: in-game 2026-10-12 they did not change the frame rate measurably. |
| `--geo-tiles` | `on` | Origin-independent content (`geotiles.py`, earthfast `geotiles.rs`): with a mod whose hello has `geoTiles: 17`, the vegetation comes in the tiles of a global Web-Mercator zoom-17 grid (~240 m at 39 deg, key `g17_<x>_<y>`), each in its own frame (`veg_tile` carries `frame: [lat, lon]` of its centre and `pivot`, m); the mod places a tile into its anchor's world (`geotile.lua`) and keeps it across rebases, so a rebase sends no tree tile again. With road processes (`--road-processes` > 0) and a mod whose hello also has `roadGeo: 1`, the road tiles are geotiles too: their content (buildings, decks, tunnels, rails, wires, street furniture, guard rails, parking, gas stations, pump jacks, sound) built once in the tile's own frame (revisit kind `roadsg`, no anchor in its key; `roads_tile` with `frame`, `h` its content hash, each part's `pivot` and `relief`), the draped surfaces (road mesh, plazas, sidewalks) a skin per anchor (`roads_skin`, cut exactly at the tile's quad), a tall part that rigid placement would leave off by more than 3 cm (colliders) / 10 cm sent as its exact variant for that anchor (`roads_exact`); a rebase sends skins only (`roads.GeoTracker`, `blockworker.roads_geo`). With the far buildings' process too and a mod whose hello has `farGeo: 1`, the far buildings, the skyline and the far bridges are geotiles (`blockworker.far_geo`, revisit kinds `bldfar` / `bldsky` / `brfar` with no anchor in the key): the far buildings in the road geotiles' own zoom-17 grid (a far tile holds exactly the buildings the road geotile of its key holds; the mod hides it once that one's are made: each building shown once at the near / far seam), sent as whole zoom-15 squares (4 x 4: `buildings.SKY_UNIT`); the skyline in those zoom-15 squares (shown while not all 16 far tiles of its square are in); the far bridges' jobs per zoom-15 square, their slabs a piece per road geotile (earthfast `bridge_slabs`; hidden where that road geotile shows its own deck and road mesh); their street lamps grouped per road geotile key in its frame. Each is placed rigidly at its pivot (render-only: no skin, no exact variant) and kept across rebases: a rebase sends no far content again. `off`: the anchor's 256 m tiles, as before (A/B). |
| `--grading` | `on` | Ground graded under roads (`grading.py`): smooth along the road, flat across the carriageway and a 1 m shoulder, 1:2 slopes back to the natural ground; in the physics block, the level -1 / 0 mesh tiles and the road heights alike. Needs `--roads on`. |
| `--block-process` | `on` | The terrain-follow and prefetch physics blocks build in two processes of their own (`blockworker.py`: one for the mod's requests, one for the prefetch), off this process's interpreter lock, each with its own DEM reader, road plans (looked up ahead of the player), street widths and 128 m height chunks over the same disk caches. A block that moved one step reuses 7/8 of its heights. `off`: built in this process's threads. In-game 2026-10-12 this took a 58 m/s drive from falling off the block after 0.5 km to 10 km without a fall. |
| `--surface` | `on` | Ground models under the physics block (`surface.py`, ROADMAP v1.5): every 2 m sample gets a material from the ESA WorldCover class under it (asphalt for built-up, grass, dusty / loose dirt, snow, mud, dirt) and rock where steeper than 35°; one 0/255 mask per material is written next to the heightmap (`hm_<a>_<cx>_<cy>_m<i>.png`) and named in `terrain.layers` / `terrain.materials`. Land cover comes from the map-data cache (`<cache>/mapdata/`), prefetched 3 km around the player; a block never waits for it (an uncached cell reads as dirt until the next block). |
| `--credits` | `on` | Data credits for the mod's on-screen overlay (`credits.py`; `credits` messages, see below): the imagery / elevation / weather sources of the tiles within 2 km of the player and of the far field (with `--colormatch s2`, a texture the seam feather corrected also names the S2 references), sent on change, at most once a second. `off`: no message (the mod shows `hello.attribution`). `Server()` has none |
| `-v` | off | Debug logging, with the event loop's stalls over 0.25 s and the main thread's stack at that moment ("event loop stalled") |

Requirements: Python 3 (3.10+), numpy, Pillow (≥ 11, for its DXT1 encoder) with WebP support. No
API keys, no other packages (the `.cdae` writer has its own msgpack encoder). The map-data pipeline
(`mapdata.py`, "Map data" below; not used by earthd yet) also needs `duckdb` (≥ 1.5) and `fsspec`:
`pip install -r server/requirements.txt`.

The v1.2 behaviour is `--near-visual terrain --imagery off` (the integration drive and `test_e2e.py`
run that way).

On startup the server prints `earthd listening on 127.0.0.1:<port> session <sessionId> -> <dir> (far tiles: cdae, near visual: mesh, imagery: usgs,eox2023,eox2016)`.
Session folders left by earlier runs (only folders that contain its `.earthd-session` marker) are
deleted at start when their marker is more than a day old (`SESSION_STALE_S`). A younger one may still
be on the game's screen (a crashed or restarted server: the game watches loaded files, and a shape
deleted under a live tile renders as nothing), so it is kept until the game says (`sessions` in its
`hello` / `pose`) whether its tiles still use it: named ones are **adopted** (kept, their markers
touched) and deleted, through the rate-limited delete queue with the folder last, once no pose names
them any more; one nobody claims goes (removed in a worker thread) `SESSION_ORPHAN_S` (10 min) after the server found it and after
its marker was last touched (a live server touches its own marker every `SESSION_HEARTBEAT_S`, 60 s,
so a second server never deletes a running one's folder). A connection that closes makes its claims
orphans again, with a fresh grace. The mod resyncs at its current origin meanwhile (a new session:
anchor ids start at 1 again; the mod keys the old session's tiles by session, and sends no `pose` or
`prepare_terrain` until the resync commits: its anchor id is the old session's and may name one of the
new server's anchors), so nothing on screen loses its files and no level reload is needed
(docs/ROADMAP.md "Robustness" 3).
CPU (§4.5, phase 1c): `main()` lowers the process priority with `os.nice(10)`, and
`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1` are set before numpy is imported. Threads: 2 heightmap
workers, `--workers` (3) tile workers, 8 I/O-bound DEM fetchers, 6 imagery fetchers. The in-memory
DEM LRU holds 512 decoded 512² tiles (~512 MB at most), the imagery LRU 512 decoded 256² tiles
(~100 MB).

## Protocol 4 (as implemented)

Changes from protocol 3 (V1_3_SPEC §E): `hello.protocol = 4`; the server's `hello` gains
`nearVisual` and `attribution`; `far_tile` gains `material`, `texture`, `detail` (all three, or
none when imagery is off or failed for that tile: then it is a v3-style tile with the static
palette material); `far_tile.sink` is sent only in terrain mode; keys can have negative levels
(`-2_i_j`, `-1_i_j`) in mesh mode. Everything else is unchanged.

Frames: 4-byte little-endian length + UTF-8 JSON (unchanged). Numbers for `cx`/`cy` come back
as JSON floats (`512.0`). All positions are in the ENU plane of the anchor named by `anchorId`.

### mod → server

| Message | Notes |
|---|---|
| `{"type":"hello","protocol":4,"format":4,"modVersion":"…","sessions":["…"]}` | `format` = the newest tile format the mod reads (the v1.4 mod reads 3 and 4; a v1.3.1 mod sends 3). A different `protocol`, or a `format` below the server's, gets a `status` warning. `sessions` (optional; a list, or `{}` for an empty Lua table): the session folders whose files the mod's objects still use (shapes of every tile object, textures of its imagery materials; `farfield.heldSessions`). An earlier run's folder named here is adopted (see "Run"); the server's own id and unknown names are ignored. The server's `hello` is unchanged. `vegFlat` (optional, `true`): the mod reads a vegetation tile as one flat array (`--trees`); without it the server sends the rows. `geoTiles` (optional, `17`): the mod places geotiles itself (`--geo-tiles`); the server's `hello` answers `geoTiles: 17` when it sends them. `roadGeo` (optional, `1`): it places road geotiles too (`roads.lua`); the server answers `roadGeo: 1` when its road tiles are geotiles. `farGeo` (optional, `1`): it places far geotiles too (far buildings, skyline, far bridges: `roads.lua`); the server answers `farGeo: 1` when they are (with `roadGeo` only). |
| `{"type":"prepare_anchor","requestId":N,"lat":d,"lon":d,"cx":m,"cy":m}` | `cx`,`cy` optional (default 0): centre of the anchor's first terrain in the **new** anchor's frame, snapped to 256 m (`--block 512`: 128 m; clamped to ±21 km). |
| `{"type":"commit_anchor","anchorId":A}` | Switches the far field to A and commits A's first terrain (from `anchor_ready`). Tiles of the previous anchor that were sent but never swapped in are forgotten: the mod drops them. |
| `{"type":"prepare_terrain","requestId":N,"anchorId":A,"cx":m,"cy":m}` | `cx`,`cy` multiples of 256 (`--block 512`: 128; other values are snapped). For the active anchor this block becomes the **pending** block: in terrain mode the far field is refined to L0 over it at once and its 64 L0 tiles are built first (mesh mode: no tile depends on a block). |
| `{"type":"commit_terrain","anchorId":A,"cx":m,"cy":m}` | The committed block. **No far-tile key changes**; in terrain mode only the forced-L0 area moves, in mesh mode nothing at all changes. For a newer known anchor it implies `commit_anchor`. |
| `{"type":"gas_stations","on":b}` | The settings page's "Gas stations" switch (additive; sent after every `hello` and on change): off, the server lays out no stations, paints no forecourts and names no shapes, and sends the road tiles again (canopies back as buildings); on, the same with them. Never on without `--gas-stations on`. |
| `{"type":"graphics","lodScale":x,"treeScale":x,"buildingScale":x,"photoPx":n,"viewM":m}` | The settings page's graphics (additive; sent after every `hello` and when one changes; every field optional, scales clamped to 0.5-3, `Connection.set_graphics`): `lodScale` times the layout's K (the far field's terrain detail: each level splits that much farther out; the near radii and the exact level under the block are unchanged), selected again at once; `treeScale` times the tree tiles' reach (`vegetation.SEND_M`, drop 256 m beyond); `buildingScale` times the far buildings', skyline's and far bridges' reach; `photoPx` the drawn block's photo size (1024 / 2048 / 4096, `earthd.BASE_SIZES`: the next photos at that size, every layer's AO map the one of that size, built blocks' photos again); `viewM` the fixed view distance (m, at least 10 km; 0 = the real horizon): the far field's radius at most. |
| `{"type":"cache","capGb":n,"clear":true}` | The download cache (`server/cache.py`; additive like `weather`): both fields optional. `capGb` = the settings page's size cap in GB (0 = unlimited; sent after every `hello` and on change), `clear: true` = "Clear cache" (everything but the files this server session used and the gravity model, on an executor), neither = fresh usage numbers (the page asks every 10 s while it is open). Always answered with a `cache`; ignored by a `Server()` without a `Cache`. |
| `{"type":"pose","anchorId":A,"x","y","z","vx","vy","lat","lon","speed","cam":{"x","y","z","fx","fy","fz","fovDeg"},"held":[a,…],"sessions":["…"]}` | ~2 Hz. Drives LOD selection, the predicted block and the terrain prefetch. `cam` is optional and only stored for now (the camera-weighted priority is phase 4). A pose with a newer known anchor implies `commit_anchor`; a late pose of the previous anchor is transformed into the active frame. `held` (optional): previous anchors of this session whose tile objects the mod still has (their files stay; the mod sends none while its tiles belong to another server's session). `sessions` (optional): as in `hello`; an adopted folder this connection named before and names no longer is released (deleted through the delete queue) unless another connection still names it. After a server restart the mod sends no pose (and no `prepare_terrain`) until its resync commits: its anchor id is the earlier session's. |

### server → mod

| Message | Notes |
|---|---|
| `{"type":"hello","protocol":4,"format":3,"serverVersion":"0.4.0","sessionId":"…","farFormat":"cdae"\|"dae","material":"bngEarthFar_v3","nearVisual":"mesh"\|"terrain","attribution":"…","cells":64,"blockSize":512,"horizon":true,"deshadow":"full","colormatch":"s2"}` | `format`: 3 with `--cells 32` (v1.3.1), 4 with `--cells 64`; `cells` (v1.4 M1) only with format 4: the tiles' cells per edge (the mod's crossover fallback error and the exact level of its `exactMiss` metric; 32 without it). `blockSize` (v1.4 M4) only with `--block 512`: the terrain block's samples per edge; the mod configures its lattice, terrain-follow table, layer / flat maps and rebase staleness rule from it before anything else (1024 without it); every `terrain.size` of the session is this size. `horizon` (v1.4 M1h) only with `--horizon`: every `status` then carries `radius`; the mod sets the sky's planet radius to 6371 km and its view distance from the radius. `deshadow` only with `--deshadow lambert|full`: the mode (the mod's settings page shows it; it is server-side only). `colormatch` only with `--colormatch s2`: the same for the colour-seam removal. `farFormat` tells which shape type the tiles are; `material` is the static palette material (tiles without imagery). `nearVisual`: `mesh` = disable the TerrainBlock's rendering (physics only), the tiles draw all the ground; `terrain` = v1.2. `attribution`: the credit line for the sources in use (with `--colormatch s2` also the S2 reference layers when the chain lacks them), e.g. "Imagery: USGS The National Map (public domain); EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023), CC BY-NC-SA 4.0; EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016 & 2017), CC BY 4.0; Weather data by Open-Meteo.com; Elevation: Mapterhorn (see mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen". |
| `{"type":"status","text":"…","backlog":n,"radius":m}` | Progress text, and protocol/format mismatch warnings; while the network is degraded or `--offline`, the text ends in " \| Degraded: …" / " \| Offline - using cached data" (see "Network"; re-sent when that changes). `backlog` (v1.4 M0): desired far tiles of the current anchor not shown yet; with `--status-interval S` the last status is re-sent every S s while it changes. `radius` (v1.4 M1h, `--horizon` only): the far-field radius in m (80 km to 1500 km); the last status is re-sent when it moves by more than 2 %. The mod sets `LevelInfo.visibleDistance` = max(100 km, 1.1 × radius), with 10 % hysteresis. |
| `{"type":"anchor_ready","requestId":N,"anchorId":A,"lat":d,"lon":d,"groundZ":m,"terrain":{"cx","cy","heightmap","size":1024,"squareSize":2.0,"zBase","maxHeight","q"}}` | `size`: 1024, or 512 with `--block 512` (`hello.blockSize`). `groundZ` = terrain height at (0, 0) of the new frame (bilinear on the quantised grid, i.e. what the game will report), or `null` if (0, 0) is outside the block. `q` = height quantum, `maxHeight = q·65536`, `zBase` a multiple of 64 m (so of `q`). No `anchorKey` yet (lattice anchors are phase 5). With `--gas-stations on`, `fuelShapes`: the shapes the anchor's region's gas stations use (`gasstations.style_shapes`), which the mod preloads (only that region's). |
| `{"type":"anchor_error","requestId":N,"error":"…"}` | |
| `{"type":"terrain_ready","requestId":N,"anchorId":A,"cx","cy","heightmap","size":1024,"squareSize":2.0,"zBase","maxHeight","q"}` | Same terrain fields as in `anchor_ready`. With `--surface on` the terrain (here and in `anchor_ready.terrain`) also carries `layers` (one mask VFS path per material) and `materials` (`[[TerrainMaterial name, ground model], ...]`, same order; the last is the level's dirt). |
| `{"type":"terrain_error","requestId":N,"anchorId":A,"error":"…"}` | Unknown anchor, bad fields or a DEM failure (so a request never hangs). |
| `{"type":"far_tile","anchorId":A,"key":"L_i_j","level":L,"shape":"/temp/beamngEarth/<sid>/far_<A>_<L_i_j>.cdae","pos":[x,y,z],"err":m,"skirt":m,"sink":m,"state":"group","material":"bngEarthSat_<A>_<key>","texture":"/temp/beamngEarth/<sid>/far_<A>_<L_i_j>.dds","detail":true,"tex":256}` | A tile to **load hidden**; it becomes visible only through a `far_swap`. `level` −2…9 (−2, −1 in mesh mode only; up to 10 with `--horizon`); footprint `[pos.x, pos.x + 256·2^level]²`. `pos` = SW corner, z = rounded mean height (an integer, so L-2 vertex heights stay exact multiples of `q`). `err` = max deviation of the tile from a 2× finer DEM reference (m). `skirt` = its skirt depth, `max(2, 0.02·S, 2.5·err + 1)`. `sink` (**L0 in terrain mode only**) = how far to lower the tile so it lies ≥ 0.3 m under the terrain block everywhere (measured against the block's own 2 m Q-rounded lattice). `state` is always `"group"` (`"prefetch"` is phase 4). Keys never depend on the terrain block. **Imagery** (all three or none): `material` = the material name written into the shape (create a Material with `mapTo` = it and `colorMap` = `texture` before the tile; the key's `-` signs are written `m`, e.g. `bngEarthSat_3_m2_10_m4`); `texture` = DDS DXT1 with a full mip chain next to the shape, row 0 = the tile's north edge; `detail` = the shape has UV1 (world-locked detail coordinates, one unit per `DETAIL_M` = 2 m). Without them the shape references the static `bngEarthFar_v3` palette material. `tex` (v1.4 M1, format 4 only, with the material) = the texture's edge in px: the mod's governor unit kind (`cdaeTex128/256/512`). |
| `{"type":"far_swap","anchorId":A,"id":G,"add":[keys],"remove":[keys],"dIn":m,"dOut":m}` | A swap group, sent once every `add` has been sent as `far_tile`. Run it once all adds are loaded: show the adds, remove the removes (C2 crossover: spawn adds at z − dIn, raise them, then sink the removes by dOut, then delete). `add` and `remove` cover exactly the same footprint (a parent and its descendants). One-sided groups: `remove: []` for the initial load / tiles entering the 80 km radius, `add: []` for tiles leaving it. `dIn` = max(new − old), `dOut` = max(old − new) over the group (both ≥ 0, evaluated at every vertex of both sides, +0.1 m). Group ids increase per connection. |
| `{"type":"far_remove","anchorId":A,"key":"L_i_j"}` | For the **active** anchor: eviction of a tile sent as `far_tile` that no group needs any more (the desired set changed before its swap); delete it, it was never shown. For a **previous** anchor (after a rebase): remove that shown tile; it is sent only after every `far_swap` whose adds cover its area (footprints compared in the new frame), so apply it once the swaps received before it have run. |
| `{"type":"far_retex","anchorId":A,"key":"L_i_j","texture":"/temp/beamngEarth/<sid>/far_<A>_<L_i_j>_r<n>.dds"}` | `--colormatch s2` / `--deshadow lambert\|full` only (additive like `weather`): the texture of a tile the mod shows, or holds (sent, not swapped in yet), under the active anchor was rendered again because a colour-seam / de-shadow region it was rendered without became ready; `n` = the revision (the engine caches textures by path). Point the tile's material (`far_tile.material`) at it: `colorMap` + `flush` + `reload`, time-sliced (the mod: at most 2 a frame, governor unit `retex`). Ignore it for a key not shown or loading under that anchor, and for an older revision than the material has. The shape, the material name and the old file stay; the server deletes old revisions with the tile's other files (eviction only when no connection shows or holds the tile; `prune` once no mod holds the anchor). Sent nearest the player first, at most `--retex-rate` per second. |
| `{"type":"far_done","anchorId":A,"count":K}` | Every swap group for the current desired set has been sent. K = number of tiles the mod shows once those swaps have run. Sent again after each later change completes. |
| `{"type":"credits","v":1,"near":"…","far":"…","ids":[…],"farIds":[…]}` | Data credits (`server/credits.py`; additive like `weather`, `--credits on` only). Every built tile records its sources: `img:<source>` for each imagery source with texels in its texture (`Imagery.render`'s counts; the fallback colour and a palette tile name none), `img:eox2023` + `img:eox2016` (the colour references) for a texture whose USGS tiles `--colormatch s2` changed (`render`'s `colormatch` count; a re-rendered stale texture takes its new render's sources), `dem:mapterhorn` / `dem:aws-terrarium` for the provider of each DEM tile under its footprint at its DEM zoom (`dem.Dem.served`, also after a netfetch failover from Mapterhorn to AWS), and `wx:open-meteo` once weather went to the mod. Shown tiles (previous anchors' included) whose footprint is within 2 km of the player are near, the rest far (a near source stays near while one of its tiles is within 2.2 km, so a player rocking across the radius does not flip it); `far` names only sources the near part does not; nothing is sent before some shown tile's sources are known (the weather alone would replace the mod's full attribution line). `near` = "Imagery: … \| Elevation: … \| Weather data by Open-Meteo.com", `far` = "Far imagery: … \| Far elevation: …" (or ""), in the order USGS, EOX 2023, EOX 2016, Mapterhorn, AWS, weather; EOX in its prescribed wording, Mapterhorn as "Mapterhorn (see mapterhorn.com/attribution)" (CREDITS.md's wording) (its underlying dataset is not in the tiles). Checked after a pose or a swap at most once a second; sent only when it changed; re-sent on `hello`. The mod (`credits.lua`) draws `near` and `far` as two lines in a screen corner. |
| `{"type":"cache","v":1,"usedBytes":n,"capBytes":n,"files":n,"ready":true,"managed":true,"clearedBytes":n}` | The download cache's usage (`server/cache.py`; additive, only with a `Cache`: the command line has one, `Server()` none). After `hello` and in answer to every mod `cache`. `usedBytes` / `files`: what is tracked (`dem/`, `imagery/`, `colormatch/`, `deshadow/`, `mapdata/`, and `gravity/`, which is never removed); `capBytes` 0 = unlimited; `ready` false while the start-up walk is still counting; `managed` false when the cache's `VERSION` is a newer layout (then it is left alone: counted, never evicted or cleared); `clearedBytes` only in the answer to a clear (bytes removed). The settings page shows "1.2 GB of 20 GB, 34877 files". |
| `{"type":"gravity","v":1,"g":m/s²,"lat","lon","h":m,"source":"egm2008"\|"normal"}` | Real gravity (`server/gravity.py` Provider / Tracker; additive like `weather`). \|g\| at the player's pose (geodetic lat/lon from the pose's anchor frame, `h` = metres above sea level, `msl=True` for EGM2008): computed on the provider's own thread (never the event loop) after the first pose on a known anchor, again when the player moved > 2 km or > 200 m in height from the last value, or when EGM2008 became available; re-sent on `hello`. `source` `"normal"` = WGS84 normal gravity (EGM2008 not on disk yet: a daemon thread downloads it, a failure is retried after 10 min). The mod (`weather.lua`) writes `g − 3.086e-6 × (z − h)` through `core_environment.setGravity` in steps of ≤ 1e-3 m/s² per second; it clears the value on a new `hello`. |
| `{"type":"weather","v":1,"t":epoch,"lat","lon","code":wmo,"isDay","cover":{"total","low","mid","high"},"baseM","baseAglM","rainMmH","snowCmH","visM","fog","thunder","wind":{"ms","fromDeg","gustMs"},"aloft":{"ms","fromDeg"},"tempC","dewC","elevM","slTempC","pressureHpa","utcOffsetS","tz","credit"}` | Real weather (`server/weather.py`; additive, no protocol bump: both sides ignore unknown types). Open-Meteo's current (15-min) conditions at the 0.1° cell of the player's `pose.lat/lon`, fetched when there is none yet, every 10 min, or after 25 km (one request in flight; a failure keeps the last weather and retries after 2 min, or after the host's Retry-After / open circuit if longer; concurrent requests for one cell share a fetch); cached per (cell, 15-min slot) for all connections; re-sent on `hello`. Rates per hour from the 15-min sums (`× 3600 / interval`); a weather code with a zero sum gets a light floor (drizzle 0.3, rain 1, thunder 4 mm/h; snow 0.5 cm/h). `visM` (null = unknown) is ≤ 800 m with fog codes 45/48. `baseM`/`baseAglM`: the drawn deck's base (= max(low, mid)): a low deck at the LCL (125 m/K × (T − Td)), a mid deck ≥ 3000 m ASL; clamped 200–6000 m above the model ground `elevM`. `slTempC` = `tempC` + 0.0065 × `elevM` (the game lowers it with height). `aloft` = 700 hPa wind (cloud drift). `utcOffsetS`/`tz` = the place's real time zone (`timezone=auto`). `credit` = "Weather data by Open-Meteo.com" (CC BY 4.0), also in `hello.attribution` while weather is on. The mod's mapping is `beamngEarth/weather.lua`. |

Far-field state per connection (`earthd.Connection`): **D** the desired set, **C** the tiles of
emitted swaps (what the mod shows), **S** the tiles sent but not yet swapped in. Groups are
`farfield.swap_groups(C, D)`, recomputed whenever D changes (pose, `prepare_terrain`,
`commit_terrain`, predicted block). Build order: L0 tiles under the committed or pending block
first (the mod's cover and commit gating need them), then tiles of groups that are already partly
sent, then the rest, nearest first (in mesh mode there is no block class: the L-2 tiles around the
player come first by distance). The imagery of the next 12 queued tiles is fetched ahead of their
build.

## Terrain block (§1, §12 P1)

- `hm_<anchorId>_<cx>_<cy>.png`: 1024² 16-bit grayscale, row 0 = north, `squareSize` = 2.0.
  Sample (gx, gy) is at (cx − 1024 + 2·gx, cy − 1024 + 2·gy), computed as integer lattice index × 2.0,
  so every sample is an even-metre point of the anchor frame and a 256 m move is exactly 128 samples.
  Row/column 1023 hold real DEM data. Physics covers (N − 1)·s = 2046 m: [cx − 1024, cx + 1022]; the
  2 m N/E strip up to cx + 1024 is covered by the (sunk) L0 tiles under the block.
- Heights: the engine computes `h = zBase + q16·maxHeight/65536`. The server sends
  `maxHeight = Q·65536` and `zBase = floor((min − 8)/64)·64`, and writes `q16 = round(u/Q) − zBase/Q`,
  so every stored height is `Q·round(u/Q)` whatever the block: **overlapping samples of two blocks
  are bit-identical** (tested on synthetic rough ground, through the DEM tile mosaic, and on real
  Mt Whitney data). Q is chosen once per anchor from a ±15 km relief survey at DEM z10: the smallest of
  {1/256, 1/128, 1/64, 1/32, 1/16} m whose range 65535·Q covers 1.1 × relief + 72 m. A block whose
  relief does not fit uses the next coarser Q (for that block only).
- Heightmaps are generated once per (anchorId, cx, cy) on two worker threads and kept in an LRU
  (6 entries). Eviction deletes the file but never touches the current terrain, the two committed
  before it, the last two handed to the mod, the terrain just built, or the first terrain of an
  anchor no connection has committed yet. (Protecting every recent anchor's first terrain made the
  protected set larger than the cache after a rebase, so each new prefetch was deleted as soon as it
  was built and rebuilt on every pose; found by the integration drive.)
- Prefetch: on each pose ≥ 200 m (Chebyshev) from the block centre, the server predicts the mod's
  next `prepare_terrain` (`predict_request`, REACH = 420 m as in `follow.lua`), builds it in the
  background and forces L0 over it. While a request is pending the prediction starts from the
  pending block (the mod commits that one before it asks again).

## Far field (§2, §3)

- Quadtree in the anchor plane: tile (L, i, j) is 256·2^L m (L-2…L9; L-2 = 64 m, L-1 = 128 m in
  mesh mode, L0 is the floor in terrain mode), key `L_i_j`. Selection (`farfield.select`): a node
  of level L ≥ 1 refines while d < K(L)·S; in mesh mode nodes also refine to L-1 within 400 m and to
  L-2 within 150 m of the player (min-level radii, `--near-radii`). A refined node stays refined until
  d ≥ 1.25 × its switch distance (`--hysteresis 0.25`: the band is 25 % of the switch distance).
  Terrain mode only: nodes overlapping the committed, pending or predicted block refine to L0 (the
  64 L0 tiles under a block are **kept**, never excluded). Mesh mode has no block forcing, cover,
  sink, apron or 2 m strip handling: the block is invisible. A node changes state at most once per
  2 s. Everything intersecting the 80 km square around the player is kept; tiles cover it exactly
  once. About 700 tiles near a block in terrain mode (64 of them under it); 784 in mesh mode
  (48 L-2, 52 L-1, 128 L0, 108 L1, 92 L2, 96 each L3–L5, 64 L6, 4 L7 around the origin).
- Each tile: 32×32 cells (2048 tris) + outward-facing skirts on all four edges (2304 tris). With
  `--cells 64` (v1.4 M1, `farfield.Layout(64)`): 64×64 cells (8704 tris, 4485 verts, 348 KB textured),
  every level keeps its size so its vertex spacing halves, the K' table and the radii L-1 ≤ 150 m /
  L0 ≤ 400 m keep the vertex spacing at a distance (300 tiles at a pose instead of 784; L-1 is the
  exact level: every lattice sample, L0 every 2nd). The server makes one `Layout` and passes it to
  `select`, `build_tile`, `zoom_for_level` and `heightmap`; no module setting is read at call time.
  Tiles of level ≤ 0 use the heightmap's DEM zoom and Q rounding, so their vertices (every 4th / 2nd /
  every lattice sample for L0 / L-1 / L-2) equal the terrain's samples bit for bit; an L-2 tile's
  vertices are exactly the terrain PNG's heights (also in the engine's float32 arithmetic). DEM zoom
  for L1+: pixels ≈ half the vertex spacing, capped at z14.
- **Triangle diagonals = the TerrainBlock's** (`farfield.torque_split45`): cell (i, j) is split
  SW–NE when i + j is even, SE–NW otherwise. Source: Torque3D `TerrainFile::_buildGridMap`
  (Engine/source/terrain/terrFile.cpp, development 4c44642, ~l. 213): for level-0 squares
  `split45 = ((squareX ^ squareY) & 1) == 0`; `TerrainBlock::getHeight`/`getNormal` (terrData.cpp
  ~721/772) and the collision convexes (terrCollision.cpp ~440, ~842) use Split45 = the SW–NE diagonal
  (x − y = 0), the renderer's index buffer (terrCell.cpp ~649) the same parity. Blocks start at an
  even lattice index and L-2 tiles at multiples of 64 m, so local and world parity agree. So the
  visible L-2 surface equals the collision surface between the vertices too (`tests/test_mesh.py`
  checks it against an independent port of `getHeight`). **BeamNG's physics may triangulate its
  terrain itself; this is the Torque rule and needs the in-game check below.**
- Colour, imagery on (default): every tile has its own DDS texture of its footprint (see Imagery)
  and UV0 = (x − x0, y − y0)/S, UV1 = (x − x0, y − y0)/2 m (world-locked: tile origins are multiples of
  64 m, so UV1 is continuous across tiles; skirts reuse their edge's UVs).
- Colour, `--imagery off`: one palette texture shared by every tile (u = height, water at the left
  edge; v = slope, mirrored around 0.5). Below 1500 m elevation the land colour is the terrain material's dirt
  (0.39, 0.35, 0.27) without slope tint, blending into the height palette by 2500 m (§2.3).
- **Material (§6.2): shipped with the mod**, not written by the server. Every tile references
  `bngEarthFar_v3`, defined in `mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/main.materials.json`
  (a v1.5 Material, `mapTo` = its own name, `baseColorMap` = the shipped `far_palette.png` there).
  The level loader loads it with the level; in another level the mod loads it with
  `loadJsonMaterialsFile`. Both files are generated by `python3 server/farfield.py` (writes into
  `mod/beamng_earth`), and `tests/test_farfield.py` checks the shipped copies match the code.
- Tiles are cached per (anchorId, key) (up to 6000 shapes and their textures); a superseded anchor's files are deleted
  once the new anchor's set is complete and none of its tiles are still shown, 30 s later. Every
  delete (pruned anchors and LRU evictions alike) goes through one queue, one file at a time: 5
  files/s in terrain mode (the v1.2 rate that removed the post-rebase freeze, P9). Mesh mode with
  imagery writes ~4x as many files per km (~380 tiles = ~760 files per km: 64/128 m tiles plus a
  texture each; a 10 km anchor leaves ~11k files), so there a long queue drains faster: backlog /
  60 s files/s, at most 40/s, still evenly spaced. An evicted tile that is built again before its
  queued delete keeps its new file.

## `.cdae` far tiles (§6.1, `server/cdae.py`)

Format decoded from engine-written files (golden pairs in `tests/fixtures/`):

- `u32` version word `0x007C001F` (exporter 0x7C, shape version 31), `u32` header size, a msgpack
  header map `{info, compression: false, bodysize, objectNames: ["tile"]}`, then an uncompressed
  body that is a **flat sequence** of msgpack values (TSShape v31 write order): shape bounds/radius,
  19 shape vectors (each `count, elementSize, bin`), names (`tile`, `detail2`), one standard mesh
  (bounds, centre, radius, verts 12 B, tverts 8 B, tverts2 8 B, colors, norms 12 B, encodedNorms,
  primitives 12 B, u32 indices, tangents 16 B, vertsPerFrame, flags 0), 0 sequences, one material
  (name, flags S_Wrap|T_Wrap, reflectance 0, bump/detail −1, 1.0, 1.0).
- Engine conventions reproduced: integral floats are packed as ints; vertices are numbered in
  order of first use by the triangle list; triangles are stored with **reversed winding**; texture
  **v is flipped** (`1 − v`, in float32); one indexed-triangle primitive (`0x20000000`); normals
  as given (or, without normals, the normalised sum of unit face normals, like the engine);
  tangents by Lengyel's method (identical to the engine's for regular UVs; far tiles use the tile's
  planar xy so they are well defined). Shape radius = tubeRadius = half-diagonal of the bounds; mesh
  radius = max vertex distance from the bounds centre; one detail `detail2` (size 2, polyCount = tris).
- Golden test (`tests/test_cdae.py`): for the same geometry the writer reproduces the engine's file
  byte size, header, every shape vector, the vertex/uv/normal arrays exactly and the triangle set
  (the engine reorders triangles for its vertex cache).
- Second UV set (v1.3, textured tiles): `tverts2` holds UV1 in the same convention as `tverts`,
  `(u, 1 − v)` in float32, one per vertex, mesh flags unchanged (0). Verified against a stock shape the
  engine compiled from a two-UV-set Collada file (`art_shapes.zip`,
  `…/string_course/G_C_SC_01_300x20`: max |tverts2 − (u1, 1 − v1)| = 8.5e-8 over all 30 corners;
  `tests/test_mesh.py` repeats the check when the game is installed). A textured tile is ~94 KB
  (85 KB + 8 B UV1 per vertex). The DAE fallback writes UV1 as `TEXCOORD set="1"`.
- Version check: at startup the server reads the version word of the engine's own cached `.cdae`
  files under `<user-dir>/temp/temp/`; if it is not `0x007C001F` it falls back to DAE tiles.

## Imagery (V1_3_SPEC §A, `server/imagery.py`)

| Source | URL (all Web-Mercator XYZ, 256 px, no key) | Zooms used | Licence |
|---|---|---|---|
| `usgs` | `basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` | 10–16 (~1.9 m/px at 36° N at z16) | public domain |
| `eox2023` | `tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2023_3857/default/g/{z}/{y}/{x}.jpg` | ≤ 14 (Sentinel-2, ~10 m native) | CC BY-NC-SA 4.0 |
| `eox2016` | `tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg` | ≤ 14 | CC BY 4.0 |

- **Rendering**: per tile, a texture of exactly the tile's footprint. Texel centres → ENU (the
  anchor frame) → lat/lon (the same mapping the geometry uses) → Web-Mercator pixel at the zoom whose
  ground resolution is closest to the texel size, capped at the source's max zoom → bilinear. The
  mapping is evaluated exactly on an 8×8 grid of texture cells and bilinearly in between (Pillow
  MESH transform; error < 0.01 texel even on 131 km tiles). Row 0 = north, column 0 = west.
  `--tex-near` (512) for the two finest levels of the mode, `--tex-far` (256) beyond.
- **Coverage**: each texel takes the first source of the chain whose four bilinear taps have data.
  A source tile counts as missing when it is 404 or blank (every channel within 2 levels — USGS
  answers with white tiles outside the US at low zooms) unless it is the last source (EOX paints the
  open ocean one uniform deep blue). USGS is used only from z10 up (it serves a global low-res
  basemap at z ≤ 9, which would put a different look on the far rings abroad) and only where its
  z10 and z12 ancestor tiles have data (coverage probes; without them a start in the Alps sent it
  1132 requests that 404'd). US borders: a partly covered USGS tile keeps its white fill (no
  blending at coverage edges, per spec). No source anywhere (beyond ±85°): the dirt colour.
- **Caching / fetching**: raw tiles in `<cache-dir>/imagery/<source>/<z>/<x>/<y>.jpg`, 404s as
  `<y>.404` markers (valid 90 days, see "Network"), decoded tiles in a 512-entry LRU, a pool of 6
  fetch threads, every request through `netfetch` (the host's rate and concurrency cap, 3 attempts,
  its circuit breaker; one persistent HTTPS connection per host and fetch thread). Once a host's
  circuit is open its tiles fail at once, so an outage costs each texture nothing instead of ~3.5 s
  (offline) to ~60 s (timeouts) per source and the tile workers keep up; `eox2023` and `eox2016` share
  one host and so one breaker. A 400 is an error, not a miss (no marker). An unreachable source falls
  through to the next one; if no source can fill a texel because of network errors (not 404s), the
  tile is sent without imagery (palette material) rather than with a baked fallback colour, and
  counts as degraded in the status note. The far-tile prefetch asks with kind `prefetch` (only while
  the host's bucket is above half; dropped requests are fetched on demand at build time, once for
  every render waiting on them; a source whose circuit is open is skipped, so the prefetch warms the
  source render fails over to), the de-shadow region fits and the colour-seam regions
  (`--colormatch s2`) with kind `background`.
- **Tone curve** (`--tone`, LUT per channel): +0.06 at black, −0.10 at white, ~0 at mid-grey: lifts
  baked shadows a little and stops bright rock/sand glowing under the engine's own lighting.
- **De-shadowing** (`--deshadow lambert|full`, default off; docs/research/2026-10-10-deshadow-prototype.md,
  method A): per USGS z12 region (~8 km) a background thread fits the capture sun against the DEM
  (z14 imagery, DEM z13 on the same pixels), gates it (r² ≥ 0.25, shadow-class separation ≥ 0.9),
  measures haze, sky/sun ratio and the shadow classifier on a few z16 tiles at the shadow edges, and
  caches the values (`<cache>/deshadow/usgs/12/x/y.json` + `.npz` grids). `Imagery._mosaic` then
  relights each USGS z12–z16 source tile with an 8 px margin from its neighbours; results live in a
  memory LRU and `<cache>/imagery/usgs-ds<ver>-<mode>/` (raw tiles untouched). Tiles of a region that
  is not ready stay raw (renders wait up to 30 s for the regions within 2 km of a new anchor).
- **Colour seams** (`--colormatch s2`, default off; docs/research/2026-10-10-imagery-colormatch.md §3,
  §7): USGS is a mosaic of NAIP flights flown per state and year, so flight boundaries are colour
  steps (Monument Valley's AZ/UT line: ΔE 17.6). Per USGS z12 region (~8 km) one background thread
  detects the survey seams on the z14 grid against EOX S2 2023 (2016 per tile where 2023 has
  nothing): long, consistent steps of the USGS/S2 ratio that S2 does not have and USGS does (a
  step in S2 only, e.g. at the edge of the 2016 fallback, is not imported; a 1-tile halo of
  neighbouring tiles; the sub-pixel line from z16 tiles fetched along the seam), then builds the
  feather field from the seams of the region and its 8 neighbours: each side of a seam moves half
  its local step, faded out with the distance (σ 1.5 km, 0 beyond 6 km), clamped to ×2; gain
  exactly 1 elsewhere (S2's palette is never imported). Cached as
  `<cache>/colormatch/v1/usgs/12/x/y.seams.npz` and `.npz` (a `.none` marker where there is nothing:
  the common case). `Imagery._mosaic` then corrects each USGS z12–z16 source tile (after
  de-shadowing, before the tone curve; the 3 px blend row on the line is inpainted from the
  neighbours, relit too with `--deshadow`) into a second memory LRU; the raw tiles stay untouched. Until a region is ready its
  tiles render raw and `render(pending=)` reports it; the server marks those far tiles stale when
  the region turns out to change them and renders their texture again (only the texture, under a
  new `_r<rev>` name): at once for a tile the mod shows (`far_retex`, `--retex-rate`), else the next
  time the tile is sent. `--deshadow` regions go the same way. z10–11 source tiles stay raw.
- **Texture format: DDS DXT1 (BC1) with a full mip chain** (512²: 10 levels, 174 904 B; 256²: 9 levels,
  43 832 B). Pillow 12 writes DXT1 but always `mipMapCount = 0`, so `imagery.encode_dds` box-reduces
  each level, BC1-encodes it with Pillow's encoder and writes its own header (`DDSD_MIPMAPCOUNT`,
  `DDSCAPS_MIPMAP|COMPLEX`). Pillow's BC1 on real imagery: 31–40 dB PSNR. Encoding a 512² chain
  takes ~4 ms. (PNG was the alternative: the engine would have to decode it on the main thread
  and build mips itself; DDS needs neither.)

## Download cache (`server/cache.py`; docs/ROADMAP.md "Caching" 1, parts of 3 and 5)

`<cache-dir>` holds `VERSION` (the layout, `1`; written atomically when missing or empty, so an older cache is adopted as
it is; any other value leaves the cache alone), `index.tsv` (`<unix s>\t<bytes>\t<path>` per tracked
file, header `# beamng-earth cache index 1\tcap=<bytes>`), and the data directories:

| Directory | Tracked | Evicted / cleared |
|---|---|---|
| `dem/`, `imagery/` (incl. `usgs-ds1-<mode>/` relit tiles), `colormatch/`, `deshadow/` | yes | yes (empty `.404` / marker files: cleared only) |
| `mapdata/` (`mapdata.py`: layer `.npz` files, the sources' `.blk` range blocks; not used by earthd yet) | yes (by mtime: no use recorded, so never protected as this session's) | yes (a missing file is built / fetched again; `404` markers: cleared only) |
| `gravity/` | counted | never |
| `netstats/`, anything else | no | never |

- **Last use** = the server's own record: every disk hit and write in `dem.py`, `imagery.py`,
  `deshadow.py`, `colormatch.py` goes through `disk.read()` / `disk.used()` / `disk.wrote()` (the
  `disk` attribute, `cache.NONE` by default, set by `Server(cache=...)`): one dict update under a lock,
  no stat per request, no reliance on atime (relatime). `read()` marks the file used before opening it,
  so an eviction pass running at that moment keeps it; a file evicted a moment earlier reads as a miss
  and is downloaded again.
- **Start-up walk** (background thread): sizes, and the file's mtime (its download time) as the last use
  of files the index does not know (e.g. a cache from before this module: 43k files in ~0.15 s, warm);
  index entries without a file go. Until it is done `cache.ready` is false and nothing is evicted.
- **Eviction**: when the tracked bytes exceed the cap, least recently used first, down to 90 % of the
  cap, at most every 3 min (`EVICT_S`), checked every 30 s and after a write over the cap. Protected:
  files this server process used within the last hour (`PROTECT_S`), `gravity/`, empty markers.
  Directories stay (a producer may be about to write into one). The index is saved every 10 min when
  something changed, after a clear and at shutdown (atomic replace; a damaged line, e.g. a `nan`
  time, is skipped, a damaged header drops the index: last uses then come from the mtimes).
- **Clear** (`cache` with `clear: true`): every tracked file of the four data directories but the
  ones this server process used (the current session's, however long ago: tiles read at its start may
  still be in the producers' memory caches), markers included; returns the bytes freed.
- **Lifetimes** (`LIFETIMES`, path prefix → max age after download): enforced by the start-up walk.
  Empty today: none of the current sources limits caching time (the 404 markers expire through
  netfetch's `miss_ttl`).
- **Versions**: per-producer versions live in the paths (`colormatch/v1/`, `imagery/usgs-ds1-<mode>/`):
  after a generator update the old directory is never read again and ages out through the LRU.
- Memory: about 200 B per tracked file (a full 20 GB cache of ~30 KB imagery tiles: ~130 MB).

## Elevation sources

| Use | Zoom | Source |
|---|---|---|
| Heightmap and L0 tiles | z15 below 58° latitude, z14 at 58–74°, z13 nearer the poles (always ≤ 2.5 m pixels) | Mapterhorn; on 404 → AWS z15 |
| Far tiles L1+ | z13 (L1) down to z5 (L9) at mid latitudes | Mapterhorn; on 404 → AWS (above z12 that is normal outside lidar areas; at z ≤ 12 a 404 is open ocean and negatives are clamped to 0) |
| Anchor relief survey | z10 | same |

Mapterhorn (`tiles.mapterhorn.com`, Terrarium WebP) uses 512 px tiles. It is global on
land up to z12 and deeper where lidar exists. AWS Terrain Tiles (256 px PNG, z ≤ 15) fill a 404
and are resampled onto the 512 px grid. A 404 is cached on disk as a `<y>.404` marker (Mapterhorn's
expire after 90 days, so lidar it adds later is picked up; AWS's never). When Mapterhorn cannot be
asked (circuit open, rate limited, failing after its attempts) the same tile comes from AWS as it is
(failover, `mapterhorn:failover` in the DEM stats); if AWS fails too the tile raises
`dem.DemUnavailable` (the terrain request gets `terrain_error`). A downloaded tile that does not
decode (an empty body, or one cut short where the server sent no length) is removed from the disk
cache and counts as that source failing (`<source>:undecodable`), so it is asked again later instead
of failing every session.

## Network (`server/netfetch.py`; docs/research/2026-10-10-rate-limits.md §5)

Every download (DEM, imagery, weather, the EGM2008 archive) goes through one `netfetch.Fetcher`,
built in `main_async` and handed to `Dem`, `Imagery`, `weather.fetch` and `gravity.Provider`.

| Host | rate / burst | at once | daily soft (hard) | prefetch | .404 marker TTL |
|---|---|---|---|---|---|
| `basemap.nationalmap.gov` (USGS) | 15/s, 60 | 6 | 60,000 | above half a bucket | 90 days |
| `tiles.maps.eox.at` (both EOX layers) | 6/s, 30 | 4 | 20,000 | above half a bucket | 90 days |
| `tiles.mapterhorn.com` | 20/s, 80 | 8 | 30,000 | above half a bucket | 90 days |
| `s3.amazonaws.com` (AWS Terrain Tiles) | 50/s, 100 | 8 | — | above half a bucket | never |
| `api.open-meteo.com` | 0.1/s, 2 | 1 | 500 (500 = 1,000 calls) | no | — |
| `downloads.sourceforge.net` (EGM2008) | 1/s, 1 | 1 | — | no | — |
| `stac.overturemaps.org` (map data: the Overture STAC index) | 5/s, 10 | 2 | 5,000 | above half a bucket | 30 days |
| `overturemaps-us-west-2.s3.us-west-2.amazonaws.com`, `overturemapswestus2.blob.core.windows.net` (Overture GeoParquet, AWS then the Azure mirror), `esa-worldcover.s3.eu-central-1.amazonaws.com`, `dataforgood-fb-data.s3.amazonaws.com` (canopy height) | 20/s, 40 | 4 | — | above half a bucket | never |
| `overpass-api.de`, `vector.openstreetmap.org`, `build.protomaps.com` | never at runtime (`PolicyDenied`) | | | | |
| any other host | 2/s, 4 | 2 | 5,000 | no | 30 days |

- **Priorities**: `demand` (a texture or heightmap being built) waits for a token (up to 30 s, then
  `RateLimited`); `prefetch` takes one only while the bucket holds more than half its burst and a
  request slot is free, `background` (de-shadow region fits, weather) only above three quarters;
  both are dropped (`Deferred`), never queued. The concurrency cap is a semaphore per host, apart
  from the thread pools. With these rates the cold-start burst (about 1,430 USGS or 1,190 EOX
  tiles and 180–400 DEM tiles) runs at ≤ 15 req/s to USGS (91 s; it was up to ~30 req/s), ≤ 6 req/s
  to EOX (193 s, so an EU cold start fills its imagery 2–3× more slowly than before) and ≤ 20 req/s
  to Mapterhorn (it was up to ~80); measured in `test_netfetch.ColdStartTest`.
- **Retries**: 429, 500, 502, 503, 504 and network errors, 3 attempts with full-jitter backoff
  (`uniform(0, min(60, 0.5 · 2^k))` s). A `Retry-After` (seconds or an HTTP date, clamped to
  1 s – 15 min) on a 429/503 pauses the host: no retry, the bucket is emptied and its breaker opens
  for that long; demand requests already waiting for a token give up at once (`RateLimited`, so
  their chains fail over) instead of sleeping out the pause. A 3xx from a tile host counts as rate limiting (EOX redirects throttled clients to
  its home page); a 200 with an HTML page from a tile host is an error (not cached); 404/410 is
  "no data" (the caller's `.404` marker); other 4xx are errors without a marker and without retries.
- **Circuit breaker** per host: 5 failed attempts within 30 s open it (requests fail at once with
  `HostUnavailable`, no network); after 30 s one probe goes through; a failed probe re-opens it for
  twice as long (up to 15 min), a success closes it (only the probe's: a late answer to a request
  sent before the breaker opened leaves it open). The chains fail over by themselves: imagery
  usgs → eox2023 → eox2016 → palette, DEM Mapterhorn → AWS.
- **Connections**: one keep-alive connection per host and worker thread; a request that fails on a
  reused connection (the server may have closed it while idle) is sent once more on a fresh one, a
  failure on a fresh connection (timeout, refused) is the attempt's result, so a timeout costs one
  timeout and the next attempt goes through the bucket and the backoff.
- **Budgets**: counted per host and UTC day, persisted in `<cache-dir>/netstats/<date>.json` (every
  60 s and at exit), so a restart keeps counting. At 80 % of the soft budget prefetch and background
  stop (one warning); at 100 % demand runs at half rate (a warning per hour); a hard budget raises
  `BudgetExceeded`.
- **Log**: one line a minute with each host's requests, MB, status classes, retries, dropped and
  refused requests and open circuits (`net: ...`); nothing when idle.
- **Status**: while a host's circuit is open, or far tiles fell back to the palette, every `status`
  text ends in " | Degraded: <hosts> unreachable; <n> tiles without imagery"; with `--offline`, or
  when every imagery and DEM host is open, in " | Offline - using cached data". The note is re-sent
  as soon as it changes (on the next pose, or within a minute). The text is unchanged otherwise.
- **User-Agent**: `BeamNG.Earth/<server version> (+https://github.com/TecnaGamer/BeamNG.Earth)`
  (`netfetch.USER_AGENT`, the only one); no user name, e-mail, machine name, location or session id.

## Timings v1.3 (this machine, warm DEM cache, 2026-10-10, real server process)

| Mesh mode + imagery, initial set (784 tiles) | anchor_ready | first tile | 48 L-2 tiles shown | far_done | imagery fetches |
|---|---|---|---|---|---|
| Mt Whitney, cold imagery cache (USGS) | 0.30 s | 0.13 s | 0.71 s | 8.7 s | 876, keep-alive latency median 25 / p95 35 ms |
| Mt Whitney, warm imagery cache | 0.30 s | 0.06 s | 0.64 s | 8.0 s | 0 (876 from disk) |
| Dolomites 46.4, 11.8, cold (EOX; cold DEM too) | 2.26 s | 0.80 s | 1.36 s | 41.2 s | 745 EOX, median 259 / p95 273 ms (their server); 66 USGS probe 404s |
| Pyrenees, warm | 0.30 s | 0.06 s | 0.59 s | 8.3 s | 0 |
| v1.2 baseline (`--near-visual terrain --imagery off`, Whitney) | 0.30 s | 0.02 s | – | 2.7 s (700 tiles) | – |

Per tile (median, 3 workers in parallel, warm): geometry + `.cdae` write 12–17 ms, texture
(reprojection + DXT1 chain + write) 18–23 ms at 512² (L-2, L-1), 8–13 ms at 256². Files: shape 94 KB,
texture 171 KB (512²) / 43 KB (256²); the initial mesh set is 72 MB of shapes + 45 MB of textures.
Cold EOX starts are bound by EOX's ~260 ms per tile at 6 concurrent requests (the near tiles come
first and are ready in ~1.4 s).

## Timings v1.2 (this machine, Mt Whitney 36.5785, −118.2923, warm DEM cache, 2026-10-10)

| | |
|---|---|
| Anchor relief survey (z10) | 0.04 s → Q = 1/16 m (Whitney relief ~3 km) |
| 1024² terrain | 0.24–0.27 s |
| One far tile, build (DEM + geometry + err/sink) | L0 4.1 ms (129² lattice), L1+ 2.0–2.2 ms (median) |
| One far tile, write | `.cdae` 1.5 ms / 85 KB; `.dae` 2.4 ms / 158 KB |
| Initial far set (block at the origin) | 700 tiles; 2.6 s wall with 3 workers at nice 10 (3.1 s on one thread); first tile after 0.02 s |
| Swap-group deltas | 0.3 ms for 1 ↔ 4, 11 ms for an L3 ↔ 64 L0 group |
| Integration drive (11.4 km at 30 m/s with a 50° bend, rebase at 10 km; modelled .cdae import 2 ms) | 24 terrain-follow commits, all at 450–456 m (never gated, none forced at 700 m); overlapping samples identical (0 m in float64 and float32, 0.69–0.80 M samples per commit); 0 far keys changed by a commit; 0 holes in 1533 coverage checks incl. the 2 m N/E strip, rebase gap 0; 2905 tiles (193/km), 727 parsed with `cdae.read`; governor ≤ 2 imports / 4.05 ms per frame, steady streaming p99 2.39 ms, 0 units within 2 frames of the 26 heavy slots; 26 + 2 terrains requested, 30 built (one overtaken by the bend, one by the stop) |

L0 `sink` on Mt Whitney: median 8.7 m, max 40 m (cliffs between the 8 m vertices); on gentle
synthetic ground it is the 0.3 m margin plus a few cm.

## Gravity (`server/gravity.py`; earthd sends it as the `gravity` message, `--gravity on|off`)

Real gravity at a point: WGS84 normal gravity plus the regional anomalies of the EGM2008 Earth
gravity model. The normal field alone runs from 9.780 m/s² on the equator to 9.832 at the poles
and loses about 0.3086 mGal per metre of height (1 mGal = 1e-5 m/s²). The anomalies on top of it
are −350 to +600 mGal: high mountain summits +100 to +350 (Hawaii +590), deep-sea trenches −300,
Hudson Bay −35 to −50. The mod's `setGravity` threshold of 1e-4 m/s² is 10 mGal, so they matter.

```sh
python3 server/gravity.py 36.5785 -118.2923 4421 --msl     # Whitney summit; h above sea level
```

```python
import gravity
f = gravity.gravity(lat, lon, h_msl, msl=True)   # GravityField; f.g is |g| in m/s^2
f.east, f.north, f.up         # gravity vector, local geodetic ENU, m/s^2 (up < 0, as GeographicLib)
f.normal, f.disturbance       # WGS84 normal gravity at the same point; g - normal
f.anomaly, f.xi, f.eta        # free-air anomaly (m/s^2); deflection of the vertical (arc seconds)
f.geoid                       # EGM2008 geoid height N (m): h_ellipsoid = H_msl + N
gravity.normal_gravity(lat, h)  # Somigliana + 2nd-order free-air series, numpy, no data
```

**Model.**
- `normal_gravity(lat, h)` uses Somigliana's formula with the published WGS84 γe = 9.7803253359 and
  γp = 9.8321849378 (NIMA TR8350.2 eq. 4-1). Height uses the second-order series
  `1 − 2/a (1 + f + m − 2f sin²φ) h + 3h²/a²` (eq. 4-3). The series is first order in f and m, so
  it is 1.1e-10 m/s² per metre off the exact value: 0.05 mGal at 4.4 km.
- `normal_gravity_exact` is the closed form in ellipsoidal coordinates. It is the reference for the
  tests and for `GravityField.normal`.
- `GravityModel` synthesises the disturbing potential T = W − U from fully normalised spherical
  harmonics, together with its gradient:
  - **Legendre functions:** the standard forward column recursion, with a log scale kept per
    order. Sectoral terms near the poles underflow above degree ~1900 (P₂₁₉₀,₇₆₀ at 68° has
    P_mm ≈ 1e-329); the scale keeps them, so the result is exact at every latitude.
  - **Latitude derivative:** taken from the same row, with the coefficients pre-shifted by one
    order, so there is no division by cos φ.
  - **Radius:** all the per-degree sums are independent of the radius. One pass therefore gives
    the field at the point and T on the ellipsoid below it (Bruns: N = T/γ + NGA's zeta-to-N
    correction − 0.41 m). `msl=True` reuses that pass.
- Gravity vector:
  - It is ∇(U + Φ + T): the WGS84 zonal series J2…J20, centrifugal force and T, rotated from
    geocentric to geodetic ENU.
  - `disturbance` is |g| − γ(h).
  - `anomaly` is −∂T/∂r − 2T/r (spherical approximation, as NGA defines it).
  - The sums run in float32 against float64 Legendre values. Against float64 sums this changes
    nothing above 0.001 mGal or 0.01 mm of geoid (30 points). It halves the memory.

**Data.**
- EGM2008 (Pavlis et al. 2012, NGA), degree and order 2190, tide-free. The source is
  GeographicLib's binary repackaging of NGA's `EGM2008_to2190_TideFree` and
  `Zeta-to-N_to2160_egm2008`:
  `https://downloads.sourceforge.net/project/geographiclib/gravity-distrib/egm2008.tar.bz2`
  (74,456,990 B, SHA-256 `54c1f511…5074ed91`, pinned in the code).
- Download: once, on first use, into `~/.cache/beamng-earth/gravity/`, with resume across
  SourceForge mirrors. Some mirrors stall at 1 kB/s; others close the connection early. The
  archive is verified, then converted to `egm2008_rowmajor.npy`: row-major triangles of C, S and
  the correction, 77 MB, read with mmap so that a truncated model loads only its prefix. The
  archive stays next to it (145 MB in all); deleting the archive is safe. First use takes about
  10 s plus the download (11 s in all on a fast mirror); later loads take 0.2 s. The only request
  is the GET, with the project User-Agent (`netfetch.USER_AGENT`), through `netfetch` (one
  request at a time, Retry-After and the circuit breaker honoured; none with `--offline`).
- Licence: EGM2008 is a US Government (NGA) product released for public use, and NGA attaches no
  licence terms. GeographicLib (MIT) only repackages it.
- The tests ship a 23 kB degree-36 truncation, `tests/fixtures/egm2008_n36.npz`.

**Degree.** The default is the full 2190, which resolves 9 km half-wavelengths. Override it with
`load_egm2008(nmax)` or `BNGE_GRAVITY_NMAX`. Timings are from this machine (numpy 2.5.3,
Python 3.14, one thread, 2026-10-10). The memory column is the model alone; the process RSS after
loading at 2190 is 227 MB, with a 386 MB peak while building. "Omission" is the |g| difference
from the full model:
- "6 named": the worst of Whitney, Quito, Denver, the Alps at 2.5 km, Everest and Baltimore, all
  at their surface.
- "30 random": surface points 0–3 km, RMS / max.

| nmax | half-wavelength | one point | model memory | load (cached) | omission, 6 named (mGal) | omission, 30 random (mGal) |
|---|---|---|---|---|---|---|
| 360 | 56 km | 3.6 ms | 4 MB | 5 ms | 97 | 18.6 / 87 |
| 720 | 28 km | 9.2 ms | 15 MB | 19 ms | 39 | 10.7 / 49 |
| 1080 | 19 km | 15.5 ms | 33 MB | 42 ms | 31 | 4.5 / 19 |
| 1440 | 14 km | 23.4 ms | 58 MB | 94 ms | 13 | 2.8 / 12 |
| 1800 | 11 km | 32.0 ms | 91 MB | 145 ms | 12 | 2.2 / 7 |
| **2190** | **9 km** | **45 ms** | **134 MB** | **0.23 s** | – | – |

A point costs one O(N²) pass: 2190 numpy steps of row recursion, then one small matmul per
degree. The other approach was a precomputed 5′ global grid. It would need one grid per height
level, because the high degrees shrink by e^(−n·h/R) (×0.25 at 4.4 km for n = 2190). Per-point
synthesis is exact at any height. Driving changes the anomaly by up to about 1 mGal per 100 m
(climbing changes gravity by 0.31 mGal per metre). A caller can therefore evaluate the model about
once a second on a worker thread (the function is thread-safe) and add the free-air term for height
changes in between.

**Accuracy.**
- The implementation reproduces published EGM2008 values:
  - GeographicLib `Gravity -n egm2008` on Everest (27:59:17N 86:55:32E, 8820 m):
    −0.00001 0.00103 −9.76782 (ours −0.0000060 0.0010327 −9.7678245).
  - GeographicLib's EGM2008 geoid at 10 places (Timbuktu, Baltimore, Denver, Quito, Whitney, the
    Indian Ocean low −106.9 m, Hudson Bay, the North Pole, Everest, the Southern Ocean): within
    1.3 mm.
- The model itself is good to a few mGal over flat, well-surveyed land. It is worse in high
  mountains, where terrain shorter than 9 km is missing: roughly 10–30 mGal there.
- Heights must be ellipsoidal or flagged `msl=True`. Ignoring the geoid (−107 to +86 m) would be
  a 33 mGal error.

| Sample (H above sea level) | geoid N | g (m/s²) | normal | disturbance | free-air anomaly | ξ, η |
|---|---|---|---|---|---|---|
| Whitney summit 36.5785, −118.2923, 4421 m | −25.17 m | 9.786692 | 9.785137 | +155.6 mGal | +162.9 mGal | +1.4″, +15.0″ |
| Baltimore 39.2904, −76.6122, 10 m | −33.60 m | 9.801040 | 9.801139 | −9.9 | +0.4 | −3.2″, +7.4″ |
| Denver 39.7392, −104.9903, 1609 m | −17.28 m | 9.796161 | 9.796555 | −39.4 | −34.3 | +2.6″, +10.3″ |
| Quito −0.1807, −78.4678, 2850 m | +26.76 m | 9.772474 | 9.771449 | +102.5 | +94.0 | −3.5″, +12.0″ |
| North Pole, sea level | +14.90 m | 9.832248 | 9.832139 | +10.9 | +6.2 | – |
| Kansas 38.5, −98.0, 10 km | −27.99 m | 9.769524 | 9.769665 | −14.1 | −5.6 | −1.6″, +4.1″ |

## Map data (`server/mapdata.py`; docs/ARCHITECTURE.md "Map data")

The shared input of every content feature (roads, buildings, water, land use, trees, ground
types): per global cell, compact numpy structures in the cell's local frame plus a provenance
record. Not wired into earthd yet; the API is the integration surface.

```python
md = mapdata.MapData(cache_dir, fetcher)          # the server's netfetch.Fetcher
cell = md.cell(mapcells.cell_of(lat, lon))        # CellData; md.prefetch(key) / kind="background"
cell.layers["roads"].attrs["class"]               # mapgeom.Features: ids (GERS), parts(i, xy), attrs, rules, own
cell.layers["landcover"].sample(lon, lat)         # maprasters.Raster (WorldCover classes); "canopy": CHM metres
cell.provenance, cell.credit_line(), cell.digest()
```

- **Cells** (`mapcells.py`): Web-Mercator z14 tiles `14/x/y` (1.2–2.4 km), independent of the
  anchor; `cells_for_bbox()` lists the cells under any footprint (v1.4 M6 global tiles, generators).
  A point on an edge belongs to the cell east / south of it, exactly as `bounds()` puts the edge.
- **`own`**: build the feature in this cell. A whole feature (road, building, line...) is owned by
  the cell holding its bbox centre; a water / land-use polygon clipped to the cell is that cell's
  piece, owned there. Building the owned features of every cell builds each feature once and
  every polygon whole.
- **Layers**: `roads` (Overture segments: class, subclass, name, surface / flags / width / level /
  subclass / max speed rules along the segment, connector ids), `connectors`, `buildings` (height,
  min height, floors, roof and facade fields, the dataset the height came from), `water` and
  `land_use` (clipped to the cell), `infrastructure` (power, barriers, bridges...), `landcover`
  (ESA WorldCover 2021 10 m, native pixels), `canopy` (Meta/WRI CHMv2 1.19 m, 2048² per cell).
- **Fetch**: the Overture release's STAC index picks the files whose bbox touches the cell; the
  footers, then the bbox columns of the row groups whose statistics touch it, then the used columns
  of the row groups with a match are fetched in parallel through netfetch (4 at once), and DuckDB
  (no extensions, its own prefetching off) reads them from the block cache. The rasters are COG
  range reads (`cog.py`, no GDAL). Overture comes from AWS, the Azure mirror when AWS cannot deliver.
- **Cache**: `<cache-dir>/mapdata/v<VERSION>/<source>/<release>/<layer>/14/<x>/<y>.npz` per layer
  (written once, read without pickles), `<cache-dir>/mapdata/raw/<source>/<release>/...` for the
  source files' 256 KiB range blocks (plus a `size` file for the COGs) and the STAC JSON (immutable,
  kept across VERSIONs). A layer file that does not load, or a block of the wrong length, is a miss
  (rebuilt / fetched again), never a lasting error. Hot blocks stay in one in-memory LRU for the
  whole process (`rangeread.MEM_BYTES`, 64 MiB).
- **Overture release**: pinned in `overture.RELEASE` (`2026-09-23.1`); `python3 server/mapdata.py
  --latest` prints the newest. Bump the constant to update (cells are cached per release). Once
  Overture has deleted the pinned release, uncached cells raise `overture.ReleaseGone`.
- **CLI** (development): `python3 server/mapdata.py --cell 39.2834,-76.5934 --dump --png out.png`
  (`--key 14/x/y`, `--around N` for a block of cells, `--layers`, `--offline`, `--cache-dir`).

Measured 2026-10-11 (this machine, home connection, one z14 cell, nothing cached, STAC included):

| place | cold | warm | downloaded | cell files | contents |
|---|---|---|---|---|---|
| Fells Point 39.2834, −76.5934 (14/4706/6244) | 3.6 s | 0.06 s | 69 MB, 63 requests | 3.3 MB | 3,013 road segments, 12,458 buildings (1.9 % height, 5.1 % height or floors), 25 water, 204 land use, 4,113 infrastructure; 84 % built-up; canopy to 25 m |
| Monument Valley 36.7818, −110.1393 (14/3179/6389) | 2.9 s | 0.02 s | 36 MB, 49 requests | 59 KB | 10 roads (tertiary, track, unclassified; 4 with surface), 1 building (Microsoft ML, no height); 92.5 % bare |
| Kayenta solar plant 36.7874, −110.2382 (14/3174/6389) | 2.9 s (0.45 s after Monument Valley) | 0.02 s | 36 MB (0 after Monument Valley) | 83 KB | 14 roads (track, service), 11 buildings, 52 infrastructure (the power plant, substations, towers, lines, fences) |

The neighbours of a fetched cell cost 1–2 s (mostly DuckDB and parsing) and 0–9 MB. Previews and the
full dumps: `docs/research/mapdata/` (local, git-ignored). A dense city cell (review 2026-10-11,
Midtown Manhattan 14/4824/6157): 4.8 s cold, 90 MB / 64 requests, 2.5 MB of cell files, peak RSS
381 MB (DuckDB, 4 layers at once), ~40 MB in memory per loaded cell (16 MB of it the
infrastructure `tags` JSON column).

### Gas stations (`server/gasstations.py`; the user, 2026-10-23: "the refueling is the main part")

A road tile (`roads_tile`'s JSON) carries `"fuel": [station, ...]` for the stations whose position lies in it; each
`{id, name, brand, ll: [lat, lon], props: [{shape, pos, yaw, scale, collide}], areas: [{pos, yaw, half, icon}]}` in
the anchor frame (yaw in degrees; `half`: a `BeamNGGameplayArea`'s scale; `icon`: its `BeamNGPointOfInterest`).
Additive: an older mod ignores it. Stations: Overture places (`mapdata` layer `places`), merged within 60 m; a
station's shop is the building nearest its pins (never a roof, a home or a public building: `NOT_SHOP_CLASSES`;
a name match and a mapper's outline first), its canopies every tagged `roof` within 30 m of the shop (or of another
canopy), else the one untagged imagery outline (Microsoft / Google ML) between the shop and a road that is canopy-sized
and rectangular. Most exact first, the pumps go on islands between the forecourt's mapped drive lanes (three or more
short parallel service roads 5-10 m apart near the shop), else on islands across each canopy's short side about 7 m
apart; otherwise no building is touched and two pumps stand on open ground clear of roads (3 m) and buildings (1 m):
between the shop and its road, else at the pin (within 20 m, not behind the shop), and with no shop at the pin when a
road is within 60 m. The style is the station's region (`style_of`, `buildings.region_style`): Europe (`alpine`,
`europe_north`, `nordic`, `mediterranean`) Italy's petrol station set (awning sections stretched over the forecourt,
Italy's pumps and brand sign); the US East and South (`us_east`, `us_south`) East Coast USA's units (three islands,
dispensers and roof in one model, stretched so its islands fall between the lanes); elsewhere West Coast USA's roof
slab over the islands, Trilobite pumps and pillars. One refuel area per pump (per island for East Coast's units). The
prompt's name is the place's, or its brand when the name does not mention it (an operator's name). Every road tile
lays out the stations within reach of it, so a canopy outline is left out by whichever tile owns it (`tile_buildings`
`skip`). Far building tiles still draw it as a box until the road tile arrives.

The forecourt is painted asphalt (`forecourt_pad`): the box around the station's refuel areas, props and canopies
with 6 m to drive round them (less where a neighbour's wall or a road's edge is nearer), reaching the shop's front
when it is within 10 m, and an 8 m driveway to the nearest public road unless a mapped service road already joins
them or a building stands between (none at all where mapped service roads already give the forecourt access). The
road tiles hand their stations' pads out (`tile_sites` `pads`); the server keeps them (`surface.Surface.add_fuel_pads`)
and gives a block's process those around it with its job, so no other process reads buildings or places for them (a
Steam Deck's memory), and the ground's three painters use them: the ground models (`paint_pads`, asphalt), the
photo (`photo_pads`, the paved roads' colour, under the roads) and the near tiles' asphalt mask (`road_mask`). Only
with `--gas-stations on` (`Surface.gas_pads`, set by `Server` and `blockworker.init`).

## Airports and runways (`server/airports.py`, `runwaygeom.py`, `runwaymarks.py`; docs/research/2026-10-11-airports.md)

Not used by earthd yet. OurAirports' `airports.csv` / `runways.csv` (public domain) are downloaded through
netfetch (`POLICIES["davidmegginson.github.io"]`, gzip) into `<cache-dir>/airports/ourairports/<gen>/` with a
`current.json` pointer, refreshed weekly and kept as the last good copy offline (`cache.py` never evicts it).
`AirportDB` gives runways (both ends, length, width, surface and BeamNG ground model, elevations, headings,
displaced thresholds, quality surveyed / derived / arp), helipads and spatial queries (`near`, `in_cell` per
z14 cell, `group`). `runwaygeom.plan_group(runways, elev_fn)` fits each runway's profile and
`graded_elev_fn(elev_fn, plans)` is the graded drop-in for `heightmap.surface`; `runwaymarks.markings(rw)`
gives the painted markings as strokes (2-node DecalRoads).

```sh
python3 server/airports.py --near 39.1754,-76.6683 --radius 5      # runways around a point
python3 server/airports.py --airport KBWI --json                     # one airport's runways
python3 server/airports.py --preview KBWI kbwi.png                   # graded field + markings + profiles (DEM via netfetch)
python3 server/airports.py --stats                                   # data-quality counts of the whole file
```
(`--cache-dir`, `--offline`, `--refresh`, `--all` for closed and estimated runways, `--px` m per preview pixel.)

## Tests

```sh
python3 -m unittest discover -s server/tests -v        # BNGE_SKIP_DRIVE=1 skips the drive test
python3 tools/run_tests.py [module ...]                # the same, a process per module, 6 at a time, live
                                                       # progress, an ETA, a timeout per module
```

`test_airports.py`, `test_runwaygeom.py`, `test_runwaymarks.py`: the airport data (parsing, qualities, surfaces,
determinism, queries, the download / weekly refresh / last-good-copy store behind a fake transport and clock,
the CLI), the grading on synthetic DEMs (limits, data ends, steep / relaxed / coarse, crossings, no steps,
footprint, determinism, `heightmap.grid_u` with the graded sampler) and the markings (classes, stripe counts,
designators, on the pavement, no overlaps, mirrored ends, displaced thresholds, closed runways).
`test_gasstations.py` the gas stations: categories and the two brand lists, merged pins and their names, the shop and the canopies (a car wash behind the shop is not one), mapped drive lanes, the layouts and their clearances, the region styles, the road tile's records and dropped canopies across tiles (`BNGE_GAS_REAL=1`: the user's stations near Pittsburgh from the real Overture data). `test_cdae.py` holds the golden `.cdae` tests. `test_heightmap.py` the lattice/quantum identity
tests. `test_farfield.py` selection, hysteresis, swap groups, err/sink/skirt and the shipped mod
assets. `test_mesh.py` mesh mode: selection down to L-2 with the min-level radii (no gaps/overlaps on
64 m cells), the 25 % hysteresis at 150/400 m, L-2 vertices == terrain PNG heights (float64 and the
engine's float32), the Torque diagonal rule against a port of `TerrainBlock::getHeight`, footprint
UV0 / world-locked UV1, and the engine's UV2 convention (stock shape, if the game is installed).
`test_deshadow.py`: box / guided filter / horizon march against brute force, hue-preserving gain
cap, sun recovery, relit ratio, halo bound and 20 px misregistration on a synthetic ridge scene,
the gates (flat DEM, low r², separation), the region cache (round trip, version bump, lambert reuse),
the imagery hook, and `--deshadow off` byte-identical to the pre-hook renders (`DESHADOW_TIMING=1`:
per-tile cost). `test_colormatch.py`: the cut blur, seam detection at every angle (noise, natural
edges, a 600 m crop-change patch and steps in S2 only rejected, also the edge of the per-tile 2016
fallback end to end), the sub-pixel line, the feather end to end on a
synthetic world (a +40 % flight seam: residual < 2 %, texture kept, blend row inpainted, gain 1 far
away), per-tile == whole region, the no-seam identity, the disk cache, the same bytes whatever order
the regions were computed in, the order with `--deshadow` (and the relit inpaint margin), the field
at the production scale (fade, clamp, z12–z15 against z16), the per-tile cost (< 10 ms),
`--colormatch off` byte-identical, no field decoded on the event loop and the server's stale-texture
bookkeeping (also for a tile evicted while its region was pending). `test_retex.py`: the live
retexture (`far_retex`): a region turning ready re-renders exactly the shown / held tiles that used
it (also a de-shadow region, and one ready while the tile rendered), under new file names, nearest
first, one render at a time on the tile pool, starts at most `--retex-rate` per second, nothing for
tiles no longer shown or of a previous anchor (stale: re-rendered when sent again, which the session
test checks too), a coarse tile waiting for 12 regions re-rendered for each, the loop guard (a render
still waiting for a region whose status is ready: at most `RETEX_REPEAT` in a row), a failed region
over an unusable cache file reporting not ready (de-shadow and colour match), a failing re-render
logged, `prune` emptying the anchor's waiting lists, eviction deleting every revision, nothing at all with
`--colormatch off` / `--retex-rate 0`; a 64-cell mesh session with a fake colour match checks the
files and messages end to end. `test_imagery.py`: a landmark lat/lon lands on its texel (within 0.2 m, two frames), orientation,
zoom choice, the source chain (404, blank, last-source blank, unreachable, undecodable, coverage
probes, min zoom), disk cache and remembered 404s (their TTL, a 400 leaves none), the EOX breaker
shared by both layers, a dropped prefetch fetched on demand, the User-Agent, prefetch, tone curve, DDS mip chain (each level
the BC1 of the box-reduced previous one, readable by Pillow); plus three real-network checks (USGS
at Whitney, EOX in the Alps, the open ocean), skipped offline. `test_server.py` runs full
protocol-4 sessions against an in-process server with a synthetic DEM (no network): terrain mode
with imagery off (v1.2) in both `cdae` and `dae` formats, and mesh mode with synthetic imagery
(material/texture fields, DDS files, no sink, terrain commits change nothing, a drive with exact
cover at every step, hysteresis near the 150 m threshold, palette fallback when imagery fails). `test_e2e.py` and `test_integration_drive.py` start
a real server and download real tiles, so they need network access; their DEM cache is
`<tmp>/beamng-earth-test-cache`.
`test_demand.py` (v1.4 M0, M1) reproduces table 2.1 of docs/V1_4_SPEC.md (tiles added per second along a
straight drive at 4 Hz poses with hysteresis and HOLD_S), the v1.3.1 and the 64-cell (M1) rows; hooks
for the M3a / M5 rows. The layout-dependent tests (`test_farfield`, `test_mesh`, `test_server`,
`test_protocol_agreement`, `test_integration_drive` mesh) run for both 32 and 64 cells.
`test_gravity.py`: WGS84 normal gravity against its published constants (γe, γp, k, m, C20) and
its exact closed form (heights, free-air gradient, plumb-line curvature); the spherical-harmonic
engine against the closed form (the normal field fed in as a model: magnitude and direction to
2e-9 m/s²), against finite differences of T, against 40-digit Decimal Legendre functions beyond
the float64 underflow (P₂₁₉₀,₇₆₀ at 68.4°), pole continuity, MSL heights, the `.cof` layout and the
CLI; the long-wavelength field from the degree-36 fixture (geoid within 3 m, Hudson Bay / Indian
Ocean negative, Andes positive, global ranges). `BNGE_GRAVITY_REAL=1` adds the full EGM2008 checks
(GeographicLib's Everest vector, 10 published geoid heights to 3 mm, summit/trench/Hudson Bay
anomaly ranges, worldwide ranges, one point < 0.2 s); it downloads 74 MB once if not cached.
The earthd side: the fixture as the provider's model, the normal fallback, a faked background
download (never blocks, one download, then EGM2008), the retry after a failed one, the 2 km / 200 m
recompute rule, and an in-process server (sent after a pose, recomputed, re-sent on hello, none off).
`test_weather.py`: the Open-Meteo responses recorded 2026-10-10 (`fixtures/openmeteo_baltimore.json`,
`openmeteo_whitney.json`) into messages, synthetic codes (fog, rain/drizzle floors, snow, thunder,
rates from `interval`), the request (no key, the project User-Agent through `netfetch`), the
cell/slot cache and its shared in-flight fetch, the 600 s / 25 km / retry rule (and a longer
Retry-After), the in-process server (sent after a pose, re-sent on hello, the credit in
`hello.attribution`, nothing on failure or with weather off), the mod's `weather.lua` mapping of the
same messages under luajit, and the level's weather objects (each once, the names the mod uses, the
committed files = `tools/make_level_assets.py`'s output). No network: the fetch is injected.
`test_mapdata.py` (no network: synthetic sources from `tests/mapfixtures.py`, generated at test time
by DuckDB and a small TIFF writer, served with Range requests by a fake transport; the Overture
tests are skipped without duckdb / fsspec): cell keys (benchmark cells, exact edges, the
Web-Mercator limits, antimeridian, owner), WKB and clipping (the no-area test independent of the
first vertex), the COG reader (Deflate, predictor 2, raw, big-endian, sparse tiles), range reads
(block cache, coalescing, mirror failover, 404 markers, one fetch per block across threads,
request kinds, damaged blocks, one memory budget), STAC filtering, a deleted release, roads /
buildings / water / land use / infrastructure parsing, "build the owned features" covering every
feature once and every polygon whole, row-group skipping, WorldCover / CHM windows, determinism
(the same digest cold, warm, layer by layer, after blocks fetched in reverse, with the rows
shuffled inside the files, from concurrent calls), cache layout and hits, damaged layer files
rebuilt, invalid keys refused, offline, prefetch refused then demand, predownload, the licences
(whole) in the provenance and in CREDITS.md, the CLI (dump and PNG).
`test_netfetch.py` (no network: a scripted fake transport, a fake clock and sleep, a seeded rng): the
token bucket (the burst at once, then 1/rate apart; prefetch only above half), the concurrency cap
(16 threads, never more than 2 in flight), full-jitter backoff, Retry-After (the host paused for
every thread, HTTP dates, clamping, a 429 without it, a waiting demand request giving up), the
breaker (open, probe, doubling, close; a late success leaves it open), the transport (a fresh
connection's failure is not repeated, a closed keep-alive one is reopened once), the DEM failover to
AWS and `DemUnavailable` (also for an undecodable tile, which leaves the cache), response classes and `.404` marker TTLs (old-style
markers too), the host policies (§5.2 numbers, denied hosts, interactive-only, pre-download),
budgets (80 % / 100 % / hard, persisted per UTC day, reset the next day), the per-minute summary,
the User-Agent (no login, host name, home directory or e-mail in any header or URL), the gravity
download through a session, `--offline` / all hosts open (the status note, a degraded tile), and the
cold-start bursts of §2.2 through each host's bucket (peak per second and per minute, drain time; a
pool of 8 threads cannot outrun it). `test_demand.NetDemandTest` (§5.4 case 12) counts the upstream
requests per minute and host of 30 and 134 m/s drives from Baltimore and Munich (the real
`farfield.select`, the imagery / DEM zoom rules, a rebase every 10 km, cold cache) and checks the
busiest minute against each host's policy rate.
`test_credits.py`: the data credits (`credits.py`): the overlay wording against the sources' own
credits and CREDITS.md word for word (ASCII only), the DEM keys of a footprint against what `Dem.sample` reads (antimeridian,
pole, too many), `Dem.served` per provider, each tile's sources in `make_tile` (none for a palette
tile's imagery, none without `--credits`; the S2 references for a colour-matched texture, also when
a stale one is re-rendered; `Dem.served` through the netfetch failover), the near / far split around the player (no
message for the weather alone, no flip-flopping at the near radius), change-only
sending with the one-per-interval throttle (an injected clock and loop), and in-process sessions
(sources near and far, nothing on quiet poses, re-sent on hello, messages at least one interval
apart, none from a default `Server()`). No network: synthetic DEM with providers, fake weather.
`test_cache.py`: the download cache's cap (`cache.py`; fake wall clock, temporary roots): LRU order
down to 90 % of the cap, a hit moving a file to the back, unlimited / raised caps, the protection
rules (this session's last hour, `gravity/`, empty markers, `netstats/` and unknown directories not
tracked, a use during a pass winning, a previous session's uses not protecting), index persistence
(last uses and the page's cap across a restart, `--cache-cap` overriding it, vanished files, newer
downloads) and damage (header, lines, non-finite times and caps, paths outside the data directories),
the `VERSION` file (written when missing or empty; a newer or unreadable one leaves the cache alone),
per-source lifetimes, clear
(what stays, every file this session used even hours ago, directories kept, waiting for the walk),
eight producer threads writing and reading while passes evict and save (byte count = entries, every
entry's file exists, no torn reads), uses during the start-up walk, the message not waiting for the
lock, the background thread's pacing (`EVICT_S`, `SAVE_S`), the hooks in `dem.py`,
`imagery.py`, `colormatch.py`, `deshadow.py` (a de-shadow region whose grids were evicted is computed
again) and earthd's `cache` messages (after hello, cap, usage, clear; none and ignored without a
`Cache`; bad caps refused; `--cache-cap` parsing). No network: a stub fetcher.
`test_drive_tool.py` checks `tools/drive.py` and `tools/drive_driver.lua` offline (profile rule on both
sides, nominal speed, the summary, the driver under luajit, `run_ge` with a fake MCP: polls >= 1 s
apart and an uninstall on every failure path). `test_benchmark_tool.py` checks the visual benchmark
`tools/benchmark.py` offline (docs/DEVTOOLS.md "Visual benchmark"): `docs/benchmarks.json` against
docs/PLACES.md and its coverage (continents, data-rich / poor, flat / steep, held-out places), the
plan (views, local mean solar time), the readiness and safety rules, whole runs against a fake game
(load numbers, files, the restore order and that it runs after an MCP error, Ctrl-C also during the
restore, a refused or failed home teleport, a timed-out place, a failed or uncommitted teleport with
no shots, a lost screenshot, the user driving off mid-run, retried MCP timeouts, a Lua reload), the
compare metrics (SSIM against a brute-force version)
and report (failed / never-ready places flagged, moved cameras noted), and runs `mod/tests/test_benchmark.lua` (the GE chunk under luajit). `test_v131_default.py` checks that a default
`earthd.Server` reproduces the v1.3.1 selection, geometry and heightmap (`fixtures/v131_golden.json`,
written by the v1.3.1 code). `bench_build.py` (not discovered: run it
by hand, it downloads unless `--synthetic`) times `Server.make_tile` per level, cold and warm.

Cross-component (server ↔ mod):
- `test_protocol_agreement.py`: the protocol 4 table above as data (sessions in both terrain/no
  imagery and mesh/imagery modes), checked from both sides: the
  mod's `net.send` constructors and every `msg.X` / `tr.X` it reads (parsed from its Lua), every field
  `earthd.py` reads, every message a real in-process session sends (types, nullability, the L0-only
  `sink`, path formats), the shared constants (protocol/format, lattice, Q ladder, `q·65536`,
  terrain-follow rule, tile grid, material name, file and `mapTo`) and the mod's material loading
  order (the material before the first tile / the terrain block); protocol 4: the `nearVisual`
  values, the imagery material name (`earthd.sat_material`) inside the shape, the texture next to
  the shape (DXT1, full mip chain, the level's size), UV0 = footprint with v flipped (row 0 =
  north), UV1 = tile-local metres / `DETAIL_M` (world-locked), and the mod's Material fields
  (`mapTo`, `colorMap`, detail map on UV1 at `detailScale 1 1`, a stock detail texture).
- `modsim.py`: the mod's runtime rules in Python, every threshold read from the Lua: `follow.lua`,
  `rebase.lua`, `governor.lua`, `crossover.lua`, the `farfield.lua` state machine (with server
  sessions: `heldSessions`, `heldAnchors`), `edgeguard.lua`'s step and core.lua's `serverHealthy`.
  `test_modsim.py` runs the Lua itself under luajit (with `mod/tests/stubs.lua`) on the same
  schedules (`tests/lua/*.lua`) and requires identical traces, frame by frame.
- `test_integration_drive.py`: the real server and the modelled mod at 60 fps over a 10 km drive with
  a rebase, twice: `--near-visual terrain --imagery off` (v1.2) and `--near-visual mesh` with
  imagery from a deterministic offline source (`tests/fake_imagery_earthd.py`). The §9 / V1_3
  acceptance checks that work offline: (a) bit-identical overlapping samples across every commit,
  (b) far keys stable across commits (terrain mode), (c) no hole at any step including the 2 m N/E
  strip (mesh mode: without the invisible block, plus every frame within 512 m of the player),
  (d) `.cdae` shapes (and textures) that parse and never vanish under a live tile / Material,
  (e) the governor's per-frame budget (no frame over it), heavy-slot isolation and streaming p99
  against the server's real message stream with texture/material costs, no pops, (f) mesh mode:
  an L-2 tile under the player at every pose, every shown L-2 tile over the block equal to the
  block's heightmap (float64 exactly, float32) with the TerrainBlock's diagonals, (g) Materials
  exactly those the tile objects use (no leak), and no burst of deletes of files the mod loaded.
  `BNGE_DRIVE_SPEEDUP` (4), `BNGE_DRIVE_REBASE_M` (10000), `BNGE_DRIVE_IMPORT_MS` (2.0),
  `BNGE_DRIVE_TEX_IMPORT_MS` (3.0), `BNGE_DRIVE_MODES` (`terrain,mesh`),
  `BNGE_DRIVE_KEEP_LOG=file` (`file.<mode>`), `BNGE_SKIP_DRIVE=1`.
- `test_restart.py` (docs/ROADMAP.md "Robustness"): the session folders (stale ones deleted at start,
  younger ones adopted from `hello` / released by `pose`, rate-limited deletes with the folder last,
  the sweep of unclaimed orphans and the heartbeat, a closed connection's claims) and a fault-injection
  drive: in-process 64-cell / 512² servers (synthetic DEM and imagery), the modelled mod drives east,
  the server is killed (a crash: nothing cleaned up), the mod keeps driving and the edge guard holds the
  car `STOP_M` inside the block, a new server adopts the dead session and the mod resyncs at the same
  origin; on every frame no hole within 512 m and no vanished file, the guard only between the kill and
  the resync commit, and the old folder deleted once the mod lets go of it. Run with the mod's anchor 2
  and with its anchor 1 (the new server's resync anchor id too; the resync commit waits the governor's
  `CALM_TIMEOUT`, so poses would go out meanwhile).
  `test_protocol_agreement.SessionsAgreementTest`: `sessions` on both sides (the mod's constructors,
  `farfield.folderOf` against the server's paths, adoption and release over the protocol).

## Attribution

Show this in the mod UI (the server sends the line for the sources in use as `hello.attribution`):
"Imagery: USGS The National Map (public domain); EOxCloudless https://cloudless.eox.at by EOX IT
Services GmbH (Contains modified Copernicus Sentinel data 2023), CC BY-NC-SA 4.0; EOxCloudless
https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016 &
2017), CC BY 4.0; Weather data by Open-Meteo.com; Elevation: Mapterhorn (see
mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen". Open-Meteo data: CC BY 4.0,
<https://open-meteo.com/>. EOX credit in its June 2026 wording (docs/research/2026-10-10-rate-limits.md):
2023 CC BY-NC-SA 4.0 (personal, non-commercial use), 2016 CC BY 4.0; see <https://cloudless.eox.at>. Elevation source details: <https://mapterhorn.com/attribution>,
<https://github.com/tilezen/joerd/blob/master/docs/attribution.md>.
