# BeamNG.Earth — Architecture (v1 design)

Target: BeamNG.drive 0.39.4 (native Linux, Vulkan). Research date 2026-10-09.
Raw research + adversarial verification: `docs/research/2026-10-09-research-raw.json`.

Game install: `~/.local/share/Steam/steamapps/common/BeamNG.drive`
User folder (VFS root overlay): `~/.local/share/BeamNG/BeamNG.drive/current`

## Core idea: "globe in the companion, flat bubble in the game"

Same pattern as MSFS "anchor space", Cesium origin-shifting, and KSP floating origin.

- **The game** only ever sees a local East-North-Up (ENU) frame of a few km
  around an *anchor* point. +X east, +Y north, +Z along the ellipsoid normal.
  Gravity stays the stock uniform -Z (error is d/R ≈ 0.07° at 8 km).
- **The companion server (`earthd`)** holds the real WGS84 Earth in float64 ECEF.
  It emits every tile as vertices already transformed into the current anchor's
  ENU frame. Curvature (drop ≈ d²/2R: 7.8 cm @1 km, 7.85 m @10 km, 785 m @100 km)
  and correct polar geometry fall out automatically. Nothing is Web-Mercator-warped.
- **Floating origin.** When the player gets ~8 km from the anchor, the mod
  *rebases*: it picks a new anchor and applies one rigid transform (translation
  plus a rotation of ~d/R) to every tile, vehicle, camera and road. Then it
  rebuilds collision and the navgraph once.
- **Tile index.** A cube-sphere quadtree (S2-style) on the server, so tiles are
  near-uniform everywhere, poles included.

## Hard engine facts (verified in game files / binary)

| Fact | Evidence |
|---|---|
| `ProceduralMesh` builds lit, textured, **collidable** meshes at runtime from Lua tables `{ {verts,uvs,normals,faces{v,n,u}(0-based),material}, ... }` per LOD | `lua/ge/extensions/flowgraph/nodes/environment/planet.lua:301-321`, `util/trackBuilder/proceduralPrimitives.lua:472-575`, `roadArchitect/roadMesh.lua:353` |
| Static collision is ONE global kd-tree, rebuilt only by `be:reloadCollision()` (~0.07 s per 2M tris on this machine; 1.5 s @37M) | user `beamng*.log`, `splineTrack.lua:978` |
| `TSStatic` can load runtime-written `.dae/.dts/.cdae`; `collisionType='None'` = render-only | `util/procTrack.lua:1269-1293` |
| glTF/GLB only with the `-assimp` launch flag (assimp has Draco/basisu/webp built in) | exe string "glTF shapes require the -assimp flag" |
| Materials are creatable at runtime (`createObject('Material')`, `colorMap`, `groundType`) | `planet.lua:77-91` |
| Scene positions are float32; Lua math and vehicle ref-node are double. There is **no runtime origin shift** (`-levelOffset x y z` is load-time only) | `cameraUpdate.lua:145`, `parseArgs.lua:119-131`, `server.lua:61-83` |
| Gravity: uniform scalar on Z (`core_environment.setGravity`) + additive point-mass planets per vehicle (`obj:setPlanets{x,y,z,r,mass}`) | `core/environment.lua:1194`, `vehicle/beamstate.lua:592-663` |
| Vehicle move without reset: `veh:setClusterPosRelRot(-1, x,y,z, qx,qy,qz,qw)` (keeps velocity) | `tech/techCore.lua:909-915`, `spawn.lua:532` |
| **GE Lua cannot reach the internet.** luasocket host allowlist (localhost/LAN/beamng.com only), no working LuaSec TLS, CEF CSP limited to localhost, `ffi.load` and `os.execute` sandboxed | exe strings `luasocket_sandbox`, `unable to parse IP: {} - DENIED` |
| Localhost IPC: `BNGWebWSServer` (native thread, mutex queue) or luasocket TCP `settimeout(0)` | `lua/ge/extensions/mcp/server.lua:109-116`, `wsUtils.lua:27-75` |
| TerrainBlock: flat, axis-aligned, power-of-2 ≤8192², heights ≥0, probably a single physics block. At most a local patch | `util/terrainGenerator.lua`, `tech/terrainImporter.lua` |
| AI navgraph can be injected: `map.reset(customMapNodes)` | `lua/ge/map.lua` |
| Sky follows real position: `core_celestial.setLocation(lat, lon)`, ScatterSky planet-radius atmosphere | `core/celestial.lua` |
| Built-in MCP server (Options › Advanced › enable MCP) → `http://127.0.0.1:29292/mcp` with `run_lua`, `get_logs`, `screenshot` (dev/test harness) | `lua/ge/extensions/mcp/` |
| Mods: `scripts/<mod>/modScript.lua` → `setExtensionUnloadMode(n,'manual')`; `extensions.load(n)` | `core/modmanager.lua:704-711` |

## Phase 0 probe results (measured in-game 2026-10-09, via MCP, scripts in `probes/`)

| Probe | Result |
|---|---|
| Internet from GE Lua | `socket.tcp():connect('example.com'/'93.184.215.14')` gives **"connect restricted"**. Localhost works. `ffi.load` is denied. |
| Localhost TCP throughput | Binary-safe (NUL/0xFF intact), **~700 MB/s** (16 MiB in 23 ms) with blocking receive. **Stream mesh data over TCP; no temp files needed.** |
| FFI under sandbox | **`ffi.cast` is a stub that returns its argument unchanged.** Pointer arithmetic on cdata fails. `ffi.new` + `ffi.copy(dst, luaString, n)` works (16 MiB into `float[]` in 2.8 ms). Parse binary payloads by `ffi.copy`-ing into typed arrays/structs. |
| ProceduralMesh winding | Triangles must be wound so the normal points **up** in BeamNG's frame: for grid quad a=(i,j), b=(i+1,j), c=(i,j+1), d=(i+1,j+1) use **(a,c,b),(b,c,d)**. Static collision is **one-sided**, so wrong winding = fall through the floor. |
| ProceduralMesh collision | Not collidable until `be:reloadCollision()`. After it, `castRayStatic` hits immediately (next call). |
| ProceduralMesh cost | 512 tris: 0.9 ms; 2 048: 2.1 ms; **8 192: 8.9 ms** (createMesh; Lua table build about the same again). Roughly linear. |
| **ProceduralMesh size cliff** | **A single 32 768-tri mesh (129² grid, 98 304 face corners) hangs the game forever** (100% CPU, needs a kill). Twice reproduced (also 131k tris). Suspected limit is 65 535 face corners (a 16-bit index); unconfirmed. **Rule: keep each ProceduralMesh ≤ 8 192 tris (verified safe). Never go above ~20k.** |
| reloadCollision | 2 048 tris: 0.5 ms; 8 192 tris: 2.3 ms (in an otherwise empty smallgrid). |
| Ray tools | `castRayStatic(pos, dir, len)` returns the physics-collision distance (use it to verify drivability). The MCP `raycast` tool only sees render geometry. |
| **Collision rebuild scales linearly with ALL collidable tris, and it blocks the frame** | ProceduralMesh: 131k tris 41 ms, 524k 162 ms (**~0.31 ms per 1k tris**). TSStatic 'Visible Mesh' costs about half (~0.15 ms per 1k). `reloadStaticCollision()` and `reloadCollision(false,true)` cost the same. There is **no cheaper path**. Budget: keep collidable geometry around ~100k tris (≈20–30 ms per rebuild). |
| ProceduralMesh render-only? | **No.** `disableCollision()` does NOT remove it from physics (ray still hits, rebuild cost unchanged). It has no collisionType field. **Far/render-only tiles must be TSStatic with `collisionType='None'`.** |
| TSStatic from runtime-written .dae | Works. A `.dae` written by an external process into `<user>/temp/beamngEarth/` loads immediately via `shapeName='/temp/beamngEarth/x.dae'`, with its PNG texture (relative `init_from`). 8 192 tris: 51 ms spawn; **131k tris: 415 ms** (XML parse, main thread). Large single meshes do NOT hang (unlike ProceduralMesh). **Its collision appears only on a reload in a later frame**, not in the same frame as the spawn. Writer: `server/dae.py`. |
| Drivability | A car drives on ProceduralMesh tiles normally (follows the surface; ground clearance stays constant). |
| **Floating-origin rebase** | Moving 64 tiles (`setPosition`) + `be:reloadCollision()` + `veh:setClusterPosRelRot(-1, p+D, 0,0,0,1)` in one GE call: a 3 km jump at 20 m/s keeps **speed (20.0→20.1) and height continuous, zero damage, zero instability**. The cost is the collision rebuild alone (tile moves 0.4 ms). Script: `probes/p6_rebase.lua`. |
| Time of day | `TimeOfDay.time` **0 = noon, 0.5 = midnight** (the MCP tool description saying 0.5 = noon is wrong). |

### TerrainBlock probes (the faster path, 2026-10-09)
| Probe | Result |
|---|---|
| Runtime create | `TerrainBlock()` + `setTerrFileLvlFolder` + `registerObject` + `MissionGroup:addObject` + `importMaps(png, metersPerPixel, maxHeight, '', {}, {}, false)`: 256² in 67 ms, 2048² in 213 ms. Works on smallgrid. |
| Re-import into existing block | 2048²: **115–138 ms** (PNG compression level makes little difference); **1024²: ~38 ms**. `reloadCollision` after it: 7 ms (2048²), 1.5 ms (1024²). |
| **Collision cost** | `be:reloadCollision()` with only a 2048² terrain: **~7 ms** (with 256²: 0.25 ms), versus 162 ms for 524k ProceduralMesh tris. Terrain does not go through the triangle kd-tree. |
| Live edits | `tb:setHeight(gx,gy,h)` ≈ 0.1 µs per sample (1M samples 108 ms); `updateGrid(min,max)` 1.7 ms per 128², 37 ms per 1024². **Physics picks up edits BY ITSELF within ≤0.5 s, with NO `be:reloadCollision()`** (probe p8: a 41² patch raised to 60 m, the car rests at 60.22). An immediate raycast still sees the old height. This is the hitch-free path for live refinement or patching. (Lead from the BrokenBrainCells terrain-deformation mod, see docs/research/2026-10-09-prior-art-live-updates.md.) |
| Orientation | `importMaps(flipY=false)`: PNG row 0 = **north** (max y). setHeight grid (gx,gy) is relative to the block's SW corner, with gx east and gy north. |
| Render-disabled terrain | `tb:setRenderEnabled(false)` stays **fully collidable** for vehicles, so it can serve as invisible physics under our own visual meshes. |
| GroundPlane | **Once any TerrainBlock exists, smallgrid's GroundPlane no longer collides for vehicles** (castRayStatic still reports it, which is misleading). Outside the block, vehicles fall forever. |
| **Multiple TerrainBlocks** | **Not supported by physics.** A second block (registered, rendered, `getHeight` correct) does not collide at its real heights: vehicles sink through to ~z=0.4. Only one TerrainBlock can be used; deleting the second leaves the first working. |
| Materials | With `materials={}` the terrain renders the pink "NO MATERIAL" placeholder; it needs a TerrainMaterial. |
| Vehicle inputs | Arcade gearbox: holding **brake** at standstill engages **reverse** and accelerates. Always zero inputs after scripted drives. |

### Design decisions forced by the probes
- **SUPERSEDED for ground by the TerrainBlock probes:** drivable ground = ONE runtime TerrainBlock re-imported
  on each rebase (~40 ms for 1024², ~130 ms for 2048²). See `docs/V1_SPEC.md`. ProceduralMesh is now only for
  small collidable extras (bridges, buildings near the player).
- (old) **Near ring (collidable):** ProceduralMesh tiles of **32×32 cells at 2 m = 64 m, 2 048 tris**
  (~5 ms each, so one or two spawns per frame without hitching). The companion streams the
  vertex data as binary over localhost TCP (parsed with `ffi.copy`). The collidable area is
  ~7×7 tiles (~450 m) ≈ 100k tris, so a rebuild costs ≈ 30 ms. Rebuild only when the ring
  shifts (with hysteresis), batched with any pending rebase.
- **Far rings (render-only):** TSStatic + companion-written `.dae` with `collisionType='None'`.
  Keep each one coarse (≤ ~8k tris) because DAE parsing runs on the main thread. A binary
  `.cdae` writer is the later optimisation.
- **Rebase** is cheap apart from the collision rebuild, so do it together with a ring change.

## Components

### Companion `earthd` (Python prototype → Rust later), 127.0.0.1
- Geodesy f64: geodetic ↔ ECEF ↔ ENU(anchor).
- Cube-sphere quadtree tile index; screen-space-error LOD driven by the player
  pose, FOV and screen height sent by the mod at ~10 Hz.
- Fetchers with two **separate** caches: a shareable cache for open data, and a
  private, HTTP-Cache-Control-only cache for bring-your-own-key (BYO-key) providers.
- Builders: terrain mesh (RTIN/simplified grid, skirts, flattened under
  roads/buildings), mipmapped DDS imagery, extruded buildings, road ribbons,
  and navgraph node tables.
- Output: atomic files in `<user>/temp/beamngEarth/<session>/` (versioned names,
  because the engine caches by path), plus small JSON control messages.

### Mod `beamng_earth`
- Level `levels/beamng_earth/`: smallgrid skeleton **without GroundPlane**,
  with SpawnSphere, LevelInfo, ScatterSky and TimeOfDay.
- Extension `beamngEarth_core`, with these parts:
  - `net`: the server connection.
  - `anchor`: the ENU frame, in doubles.
  - `tiles`: the spawn/delete queue, time-sliced to ~2 ms per frame.
  - `collision`: the scheduler for collision rebuilds.
  - `rebase`: the floating-origin shift.
  - `navgraph`, `sky`, and `ui`: teleport to lat/lon, server status, attribution.

### LOD rings (indicative)
| Ring | Range | Ground resolution | Collision |
|---|---|---|---|
| R0 | 0–1 km | 1–2 m DEM + buildings | yes (ProceduralMesh, ≤1–2M tris) |
| R1 | 1–5 km | ~8 m | no |
| R2 | 5–30 km | 30–60 m | no |
| R3 | to horizon | ~250 m | no (fog/atmosphere hides the edge) |

The parent tile stays visible until all its children are loaded (no holes).
R0 collision is rebuilt in batches every ~250–500 m travelled, with the next
tiles prebuilt ahead along the velocity vector.

## Data sources

Physics geometry always comes from open data:
- **Elevation:** Mapterhorn (open, Terrarium WebP, z12 global, up to z18 where
  national lidar exists), with AWS Terrain Tiles as fallback. Above ±85° use
  Copernicus GLO-30, because the Mercator sources stop there.
- **Imagery:** EOX Sentinel-2 cloudless 2016 (CC BY, 10 m) far away, NAIP or
  national orthophotos near the player. Esri World Imagery is optional, using
  the user's own key.
- **Buildings and roads:** Overture and OpenStreetMap, from extracts the server
  holds itself. Never query the public Overpass API at runtime.
- **Land cover and vegetation:** ESA WorldCover and Meta canopy height.
- Both are read per global cell by the map-data pipeline (`server/mapdata.py`, "Map data" below).
- **Gravity:** WGS84 normal gravity (Somigliana + free air) plus the EGM2008 anomalies (NGA,
  degree 2190, about 9 km; 74 MB once from GeographicLib's mirror of NGA's coefficients;
  45 ms a point): `server/gravity.py`, described in server/README.md "Gravity". Real g runs
  from 9.76 to 9.84 m/s²; the anomalies are −350 to +600 mGal. earthd sends it to the mod as the
  `gravity` message (`--gravity on`, default), applied through the weather extension's `setGravity`.

**Google Photorealistic 3D Tiles** (v2, optional): visual only, the user's own
key, fetched straight from the user's own companion, no shared cache, Google
logo and attribution on screen. It returns 403 for projects billed in the EEA
since 2025-07-08. Bing Maps is retired for new users. Apple has no usable API.

## Roadmap
0. **Probes** (via MCP `run_lua`): sandbox state, IPC round-trip, `createMesh`
   cost on a 257² grid, `reloadCollision` hitch at 0.5/1/2/5M tris, whether
   `disableCollision` excludes a mesh from the rebuild, whether runtime-written
   files in `/temp` load at once, `setClusterPosRelRot` with trailers, jitter
   at `-levelOffset` 20/100/500 km, `visibleDistance` 50–100 km.
1. Skeleton: the mod, the level, the extension, the companion handshake, the
   anchor module, teleport to lat/lon, and sky location.
2. Drivable terrain: the R0 collidable ring from open DEM data, plus floating-origin rebase.
3. LOD to the horizon: R1–R3 render-only rings, imagery, curved ocean, and
   tests at the poles and the antimeridian.
4. Man-made world: roads, navgraph and AI, buildings, and ground types.
5. Performance: vegetation, LRU disk cache, prediction, budgets.
6. Optional: the Google 3D Tiles visual layer, flight mode, radial gravity, multiplayer anchors.

## Top risks
- Collision-rebuild hitches when streaming at highway speed.
- LuaJIT garbage-collection churn from table-based `createMesh`.
- Rebase must move every subsystem that stores absolute positions.
- Float32 precision and depth precision at long view distances.
- Licence/ToS constraints on Google and on non-commercial data sources.
- Open imagery is only 10 m in much of the world.
- Undocumented engine behaviour that may change with game updates.

## v1.1 in-game results (2026-10-10)
| What | Measured |
|---|---|
| Teleport (anchor + 1024² terrain import) | 66–67 ms in-game; server builds the terrain in 0.24–0.38 s |
| Terrain-follow commit (block moves, nothing else does) | ~67–71 ms. Of that, `importMaps` of a real 1024² heightmap is ~49 ms (level-1 PNG) or **~43 ms (uncompressed, now the default)**, and `reloadCollision` 1.6 ms |
| Origin rebase (2 km test; default 10 km) | 75 ms, with exact rotation (0.022° at 2 km), 15 camera state points remapped, longitude continuous |
| Far field | Initial 384–445 tiles (L0…L7) sent in 1.7–2.8 s. After a rebase all tiles are re-streamed in the new frame at ≤6 ms per frame (~10 s); the old tiles stay visible meanwhile, so there are no holes |
| Server restart | The mod reconnects and resyncs with an identity transform; the car doesn't move |

Next optimisations: anchor-independent far tiles (tile-local frames) so a rebase reuses them instead of re-streaming; a binary `.cdae` writer for faster tile loads.

## v1.2 in-game results (2026-10-10, docs/research/perf-session-v12.jsonl)
| What | v1.1 | v1.2 |
|---|---|---|
| Far-tile import | ~22 ms (Collada) | **~1.9 ms** (server-written .cdae; governor estCdae 1.93) |
| Terrain-follow commit / worst frame | 57–66 ms / 71–93 ms | **45–49 ms / 59–63 ms**; neighbouring frames calm (13–16 ms) |
| Ground under the car across terrain moves (400-point P1b auto-check) | resampled every move | **0.00 mm** on every move (one 0.06 mm) |
| Average FPS driving | 70–90 | **90–100** |
| Post-rebase reload | ~44 fps, p95 25–27 ms, ~400 tiles | **73–81 fps, p95 14–16 ms**, sim speed 1.000, ~770 tiles |
| Rebase commit | 61–65 ms | **50 ms** (one 66 ms frame) |
| Freeze ~30 s after rebase (burst delete of loaded files) | 200–625 ms, every rebase (8/8) | **gone**: worst frame 62 ms; deletes run at 5/s |
| Tile crossovers | n/a | 55+ smooth crossovers, 0 pops, 0 failed imports |
| Server RSS | ~410 MB | ~600 MB (DEM LRU raised to 512 tiles) |

## v1.3 in-game results (2026-10-10, docs/V1_3_SPEC.md; shots in docs/research/v13_ingame, drives in docs/research/sim_v13*)
Satellite imagery on every tile; the near ground is drawn by 64/128 m mesh tiles (L-2/L-1) and the
TerrainBlock is physics only (render disabled, stays disabled across `importMaps`).

| What | v1.2 | v1.3 |
|---|---|---|
| Look | brown terrain dirt next to whitening far palette: a hard colour border, mid-distance | USGS (US, ~1.9 m) / EOX (world, ~10 m) imagery on everything, **no border** near or far |
| Ground change across a terrain-follow commit (fixed camera) | the terrain itself was the visible ground | the terrain is invisible: commits change nothing on screen; remaining diff is far crossovers + sub-pixel jitter (3.4 / 10.6 mean, v1.2 1.5 / 10.8) |
| Ground swap at a rebase (fixed camera, 0.5 s vs 10 s after, ~630 tiles replaced) | n/a | **2.4 mean, 0 % of pixels > 30**: invisible |
| Terrain collision vs visible mesh | n/a | BeamNG terrain collision is **exactly bilinear** per 2 m square (probe `probes/v13/p_diag.lua`: 0.320 at (0.8,0.4) and (0.4,0.8) on both parities), not triangulated. The mesh's triangles differ from it by \|twist\|/4 at a square centre: median 0.4 cm, p99 3.9 cm, max 31 cm on a 145 m-relief block. (Fix if ever needed: a centre vertex per L-2 square cuts it to \|twist\|/16.) |
| Textured tile import (governor estCdaeTex) | 1.9 ms (untextured) | 3.6–4.7 ms; material 0.4 ms; material delete 0.04 ms |
| FPS driving (sim, 30 m/s) | 94 | 92 in Kern canyon, 70–77 on open desert; rendering the 850 textured tiles costs only ~0.6 ms (hidden-tiles test) - the rest is streaming |
| Post-rebase reload | 73–81 fps, ~770 tiles | 56–70 fps for ~50 s, ~880 tiles, sim speed 0.996; old tiles drop as soon as new ones cover them (`oldCovered`) |
| Pops / failed imports / material leaks / pink tiles | 0 / 0 / - / - | 0 / 0 / 0 (materials == live tiles after 8 boundary-crossing passes) / none |
| Server deletes (up to 40 files/s in mesh mode) | 5/s | no effect on frame times (server paused vs running: same) |

Fixed in-game during v1.3 bring-up:
- **Tile-edge seam line**: the engine samples material textures with one WRAP sampler
  (`shaders/common/bng.hlsl` `defaultSampler2D`), so each tile edge blended in the opposite edge.
  Textures now carry an 8-texel border of real imagery (`imagery.gutter*`), UV0 inset to match.
- **Fabric-like pattern on distant hills**: the v1.0 material adds the detail map with no distance
  fade (`defaultMatV0.hlsl:246`), so the stock 2 m detail texture repeated visibly at 300 m. Replaced
  by our own procedural `art/beamngEarth/detail_ground.dds` (tools/make_detail_texture.py) whose mip
  chain fades to flat 0.5 grey: the GPU's mip selection fades the grain out with distance.
- Near textures 512 -> 256 px (64 m tiles were 15x finer than the USGS source): half the build time
  and VRAM, no visible change.
- tools/sim_drive.py kept stepping in the old frame after a rebase (26 rebases in a row in the
  test); it now continues from the car's position in the new frame.

Imagery quality limit: USGS ImageryOnly z16 is soft and JPEG-blocky up close (~6 m blocks). The
public-domain **USGS NAIP ImageServer** (`imagery.nationalmap.gov/.../USGSNAIPImagery/ImageServer/exportImage`,
0.6 m, CONUS) is far sharper, but: no tile cache (exportImage only), 4–29 s per request (a 2048²
request covers ~1.2 km), and a different vintage/colour than the USGS tiles (Lake Isabella: other
water level; mosaic seams), so mixing it near the car with USGS beyond would bring back a colour
border. Candidate for v1.4: NAIP for all US levels via large chunked exportImage requests fetched
ahead of the car, plus a `far_retex` message (swap a live tile's texture) for tiles built before
their chunk arrived.

## v1.4 M0: test harness and baselines (docs/V1_4_SPEC.md §3.0; offline parts done 2026-10-10)
- **GE-side driver** `tools/drive.py` + `tools/drive_driver.lua`: the Lua is sent through MCP
  `run_lua` and hooks `onPreRender` with an extension proxy (`newExtensionProxy` +
  `submitEventSinks`, `lua/common/extensions.lua:1081-1164`; no mod file, removed with
  `proxy:destroy()` in a `finally`). Every frame it moves the player along a profile with
  `setClusterPosRelRot(-1, …)` (a yaw-only relative turn, no reset) and
  `applyClusterVelocityScaleAdd(-1, 0, vx, vy, vz)`, rays the static-collision ground
  (`castRayStatic`, a miss = `fall`), keeps the inputs at zero and records each frame (wall ms,
  heavy slot, rebase, harness MCP work, exact miss, tile level under the car). A rebase moves the
  driver's own frame through the wrapped `rebase.moveVehicles`. Error paths: a failed install step
  undoes the earlier ones; an error in a frame, or no poll for 60 s (a dead harness), stops the run,
  parks the car (velocity 0, inputs zero) and restores `rebase.moveVehicles`; every MCP call after
  the first runs inside drive.py's `finally` uninstall; reloading the driver always uninstalls the
  previous one. MCP is polled at most once a second,
  timing runs take no screenshots (`--shots S` = a separate visual run), `--step-mode mcp` keeps the
  old `sim_drive.py` stepping. The summary has the effective speed (a run below 95 % of nominal
  fails), exactMiss (overall and outside 2 s after rebases), commit / rebase frame ms apart from the
  steady frames, max queued, backlog slope, live / objects max, estCdaeTex first vs last quarter and
  the post-rebase reload time.
- **Mod metrics** (behaviour unchanged): `farfield` finds the shown tile under the player by key
  lookup each frame (previous anchors through the inverse rebase transforms) and reports
  `exactMiss` / `exactFrames` / `underLevel` / `waitingGroups` (exact level −2 for 32 cells;
  `setExactLevel(-1)` for M1's 64 cells); `perfmon.tagHarness()` keeps MCP frames out of its
  statistics; `core` keeps the server's `status.backlog` (`status().serverBacklog`).
- **Server**: every `status` carries `backlog` (desired − shown, current anchor); `--status-interval S`
  (default 0 = v1.3.1) re-sends the last status while it changes — harness runs use 1.
- **Default = v1.3.1** (`server/tests/test_v131_default.py`): a default `earthd.Server`'s selection,
  switch distances, DEM zooms, tile geometry, written .cdae / .dae sizes, swap deltas and heightmap
  match `tests/fixtures/v131_golden.json`, written by the v1.3.1 code.
- **Offline**: `server/tests/test_demand.py` reproduces table 2.1's v1.3.1 rows (14.2 / 33.7 / 50.3
  adds/s at 40 / 90 / 134 m/s against 14.4 / 33.4 / 49.9; M1/M3a/M5 hooks marked);
  `server/tests/bench_build.py` (not discovered) times `Server.make_tile` per level, cold and warm.
  First 64-cell numbers (Whitney, one worker): warm 16-18 ms at 256 px (real and synthetic data),
  27-30 ms at 512 px (synthetic); cold L-1 0.48 s and L4 0.60 s with network fetches; 348 KB `.cdae`,
  8704 tris.
- Still to run in-game (user not playing): P0 (the driver's calls), then the D40 / D67 / D90
  baselines with v1.3.1 behaviour.

## v1.4 M1: 64-cell tiles (docs/V1_4_SPEC.md §3.1; built offline 2026-10-10, behind a flag)
- **Server**: `earthd.py --cells 32|64` (mesh mode only for 64; **default 32** = v1.3.1, FORMAT 3,
  until the in-game gate P8 passes). The server makes one frozen `farfield.Layout(cells, block,
  mesh, tex)` and passes it to `select` (K table, near radii), `build_tile`, `zoom_for_level` and
  `heightmap` (block size); there is no module-level `CELLS` any more and no test changes module
  state. 64 cells: FORMAT 4, tiles 64×64 cells (8704 tris, 348 KB textured `.cdae`), every level
  keeps its size, the K' table {0.75, 1.5, 1.5, 1.25} and NEAR_RADII {-1: 150, 0: 400} keep the
  vertex spacing at a distance; the exact level is L-1 (128 m on the 2 m physics lattice, Torque
  diagonals); `--tex` per level (default 256 px to L1, 512 px from L2); `write_built` allows 9000
  tris; tile cache 2500; `hello.cells` and `far_tile.tex` (FORMAT 4 only, so a 32-cell session is
  byte-for-byte v1.3.1).
- **Demand** (`test_demand.py`, real `select` at 4 Hz with hysteresis and HOLD_S): 64 cells 303 /
  309 / 309 tiles and 4.6 / 10.7 / 16.0 adds/s at 40 / 90 / 134 m/s (spec 300; 4.6 / 10.6 / 16.0),
  against 838-867 tiles and 14.2 / 33.7 / 50.3 adds/s for v1.3.1.
- **Mod**: reads FORMAT 3 and 4 (`core.lua FORMATS`; its hello sends 4, the newest); `hello.cells`
  sets `crossover.setCells` (fallback error) and the exact level of the `exactMiss` / under-level
  metric (`farfield.exactLevelFor`: L-2 for 32, L-1 for 64); the import kind follows `far_tile.tex`
  (`governor.texKind`: `cdaeTex128/256/512`, priors 0.75 / 3 / 12 ms = today's `cdaeTex` scaled by
  texture area until P8; FORMAT 3 tiles stay `cdaeTex`). `tools/drive.py` summarises the 256 px kind
  for 64-cell sessions and every textured kind apart.
- **Integration drive** `MeshDrive64Test` (real server, `--cells 64`, 16.6 km with one rebase; the
  32-cell drives are kept): cost per kind and object count c = prior · (1 + 0.0016 (n − 300)) with
  the 256 px import at 6 ms (128 px 4.9, 512 px 10.5). Results: 300 tiles initially (784 with 32
  cells), 125 tiles/km while driving (383), tile objects max 414 (1047); no hole, no pop, no failed
  import; the tile under the player is L-1 at every pose and 664 L-1 tiles match the heightmap bit
  for bit; between vertices the mesh is within |twist|/4 of the bilinear collision at every one of
  ~5500 random points under the player (median 0.19 cm, p99 12.9 cm, max 60 cm on this route: the
  same p99 as the 32-cell drive, 13.0 cm, since both exact levels are the lattice; the spec's 5 cm
  is the p_diag block's figure); streaming-frame p99 12.4 ms of work, steady frame wall p99 27.6 ms
  with this cost model (the in-game rule is ≤ 25 ms: P8 decides).
- Still to do in-game (user not playing): P8 (+P10), then flip the default to 64 (`CELLS_DEFAULT`
  in `server/earthd.py`).

## v1.4 M1 in-game results (2026-10-10, Baltimore, 4K, tools/drive.py with a stock test car)
| run | layout | exact level under the car | queue max | objects max | fps mean | steady frames > 50 ms |
|---|---|---|---|---|---|---|
| D67 | 32 cells (v1.3.1) | 99.9 % | 44 | 970 | 82.5 | 2 |
| D90 | 32 cells (v1.3.1) | **25.5 %** | **2442** | 1187 | 62.6 | 1 |
| D67 | 64 cells (M1) | 100 % | 17 | 341 | 103.0 | 3 |
| D90 | 64 cells (M1) | **100 %** | **35** | 339 | 97.1 | 1 |
| D134 | 64 cells (M1) | 97.0 % (L0 in 46 frames) | 64 | 370 | 81.5 | 0 |
- P8 gate: 64-cell textured imports 4.03 ms (256 px) and 4.21 ms (512 px) at ~300 objects (gate:
  <= 7.5 / 12 ms); the shape (348 KB) dominates. Governor priors set to these values.
- 64 cells is now the command-line default in mesh mode (`--cells 32` keeps the v1.3.1 layout;
  `Server()` itself still defaults to 32, pinned by test_v131_default).
- Terrain-follow commit frames are still 55-80 ms (M4 addresses them); no rebase in these 12 km runs.
- Harness lesson: per-frame placement of a lifted, soft-sprung car (the user's Hopper Off-Road) at
  67 m/s made its physics unstable and the engine removed it. drive.py now drives a stock test car
  (Vivace), freezes the user's vehicle meanwhile and restores it, and stops on a non-finite or runaway
  velocity ('unstable').

## v1.4 M4: the 512² physics block (docs/V1_4_SPEC.md §3.5; built offline 2026-10-10, behind a flag)
- **Server**: `earthd.py --block 512` (mesh mode only; **default 1024** = v1.3.1 until the in-game
  probes below pass). `farfield.Layout(cells, block)` carries the block size (`Layout.grid` 128 m,
  `Layout.block_at`, `Block.size`); `heightmap` takes the size everywhere (`grid(size)`,
  `valid(size)`); `earthd.FOLLOW_RULES[size]` is the mod's follow table (512: prepare 128 m, lead
  2 s, reach 260 m, prefetch from 100 m; fast mode above 150 m/s: lead 3 s, reach 420 m, the prepare
  test 0.75 s ahead), used by `predict_request` and the prefetch start; requests snap to the layout's
  grid; `hello.blockSize` only with 512 (a 1024 session's hello is unchanged), `terrain.size` 512.
- **Mod**: `core.setBlockSize(hello.blockSize or 1024)` first in `hello` calls `lattice.configure`,
  `follow.configure`, `terrain.configure` (the 512 layer / flat maps, also for `loadFlat`) and
  `rebase.configure`; `anchorTerrain` / `terrain_ready` accept the session's size. `follow.lua` has
  flat `*_1024` / `*_512` constants (modsim reads them) and `configure` resets every derived local.
  1024² behaviour is unchanged (no fast mode, no re-check, stale anchors discarded as before).
  - Commit-time re-check (512 only, normal mode): a ready block with
    cheb(p + 2 s·v, pending) > 128 m is discarded and re-prepared **only while the player stays out
    of the 150 m edge band for longer than the heightmap latency (the measured one, at least 1 s)
    + 0.5 s** (never past the hard commit, never to re-request the same centre). The spec's unguarded
    rule discarded nearly every block in the offline model (77 of 78 commits at 134 m/s) and took a
    250 m/s car up to 98 m off the block at 1.2 s latency.
  - A ready block that would still be no better than the current one where the player reaches the
    hard commit distance (straight on) is dropped, so the next frame prepares the target; before
    (review, 2026-10-10) it blocked every request until it left the 260 / 420 m reach.
  - Offline (luajit `test_follow`, the real follow.lua): latency drawn per request from 0.3-1.2 s,
    commits 0-0.15 s late, all headings: ≥ 171 m inside the edges at 134 m/s, ≥ 178 m accelerating
    100 -> 200 m/s (before the two review fixes: 75 m, and up to 116 m off the block); fixed latency:
    134 m/s ≥ 199 m, commits ≥ 0.95 s apart at 0.3 s (spec ≥ 0.9; 0.32 s without the re-check);
    200 m/s at 1.2 s (fast mode) ≥ 216 m; 250 m/s at 0.3 s ≥ 254 m.
  - Stale-anchor rule (512 only): a stale rebase anchor is still committed when the player is
    ≥ 150 m inside its terrain's collidable area; otherwise discarded and re-requested with the
    terrain at p + v·(measured anchor latency + 1 s) (`core.rebaseHint`), so a slow server cannot
    loop. `rebase.STALE_DIST` 450 / 192 m (= the table's commit distance).
  - Assets: `beamngEarth_layer_512.png`, `beamngEarth_flat_512.png` (`tools/make_level_assets.py`).
- **Tests**: `test_heightmap` over (32, 1024²) and (64, 512²) (128 m moves bit-identical, even SW
  lattice index); `test_server` `MeshServer64Block512Test` (+ the prefetch against modsim's 512 rule,
  both modes); `test_protocol_agreement` (both tables, `blockSize`, STALE_DIST per size, the
  512 session's messages); modsim ports + `modsim_trace.lua` 'M' lines (the 512 table, fast mode,
  re-check, stale rule, configure back to 1024); luajit `test_follow`, `test_lattice`, `test_rebase`,
  `test_core_stubbed` (hello.blockSize 512: a 512² anchor accepted, the 512 flat map at level start,
  a 1024² terrain refused, back to 1024 without blockSize).
- **Integration**: `MeshDrive64Block512Test` (30 m/s, 16.5 km, one rebase): 100 commits, every
  overlap bit-identical (196 608-229 376 shared samples), no hole, the tile under the player L-1 at
  every pose, the player ≥ 296 m inside the block, commit 18.4 ms / rebase 44 ms in the cost model.
  `FollowDrive512Test` (in-process server, real time, no far field): 134 m/s at 20°: no late
  terrain, ≥ 243 m inside; 200 m/s with +1.2 s heightmaps (fast mode): ≥ 167 m inside; 250 m/s
  with a 3 s / 5 s anchor latency: one rebase each (5 s: the stale answer discarded once, the
  re-request committed).
- **Still to do in-game** (user not playing; `tools/m4_probes.py`, Lua in `probes/v14/m4_probes.lua`,
  no mod update needed, the 512² heightmap is a crop of the live one): `check`, then **P2**
  (gate: steady 512² import ≤ 30 ms with the layer map; extent [c−512, c+510]; heights equal;
  restore), **P11** (does physics follow a re-import without reloadCollision), **P7 (a)**
  (`p7a-install`, a drive with ≥ 3 rebases, `p7a-read`: the rebase-frame breakdown and remainder),
  **P7 (b)** last (a hidden second block, swap ≤ 5 ms). Then D134 with `--block 512` (commit frames
  ≤ 40 ms, rebase frame per §3.5) before flipping `BLOCK_DEFAULT`.

## v1.4 M1h: the line-of-sight horizon (docs/V1_4_SPEC.md §3.6 "Radius"; built offline 2026-10-10, behind a flag)
- **Server**: `earthd.py --horizon` (mesh mode only; **on by default in mesh mode since 2026-10-11**, the owner's choice; `--no-horizon` for the fixed far radius; `Server()` keeps it off).
  The eye is the pose's camera z (else player z + 1.5 m) as a height above sea level in the pose's
  anchor frame (`frame.enu_to_geodetic`; the frame's h0 is sea level), quantised to max(10 m, 5 %),
  kept across rebases. Per anchor, one `horizon` worker builds `farfield.HorizonGrid`: the highest
  DEM z6 pixel under every L6 cell (16 km; block maxima of 8 × 8 px, `dem.Dem.block_max`, cached
  across anchors) and its max pyramid up to L10, over ±(min(1500 km, d(h_eye) + d(8900 m)) + 21 km),
  rebuilt bigger for a climbing eye; z5 or coarser where z6 would need > 64 DEM tiles. `select(...,
  max_level=10, disc=True, horizon=Horizon(eye, grid))`: roots at L10 (262 km), a disc of RADIUS =
  clamp(d(h_eye) + d(zmax_region), 80 km, 1500 km), d(h) = sqrt(2R'h), R' = 6371 km / (1 − 0.13),
  zmax_region = the highest L6 cell above the eye's horizon; beyond 80 km a node whose nearest point
  is past d(h_eye) + d(zmax_node) is dropped with its subtree. Within 80 km the selection is today's.
  Textures 256 px at L8/L9, 128 px at L10 (imagery picks the low zoom). Until the first grid the far
  field is the 80 km disc. `hello.horizon: true`; every status carries `radius` (re-sent when it moves
  by > 2 %). `swap_groups` unions up to L10.
- **Mod**: `horizon.lua`; `core.setHorizon(hello.horizon == true)` and `core.setFarRadius(status.radius)`:
  `LevelInfo.visibleDistance` = max(100 km, 1.1 × radius) with 10 % hysteresis, `postApply()` after every
  change (the engine ignores the field without it); `ScatterSky.atmoPlanetRadius` = 6371 (level: 6360)
  while horizon mode is on in an active world; both back to the level's values at level end (sky), on
  unload and with a server without horizon. Distance haze: TODO in `horizon.lua` with the
  `core_environment` knobs (setFogDensity, setFogAtmosphereHeight) and the ScatterSky Mie fields.
- **Offline numbers** (real DEM, 64 cells): Whitney summit 503 km radius, 83 tiles beyond 80 km (379
  in all, 300 today), San Gorgonio (306 km) kept, farthest tile 414 km; 10 km above Whitney 631 km, 97
  beyond; Central Valley floor 289 km, 49 beyond; open ocean 80 km, none beyond (296: the disc has no
  corners). Selection 1-2.5 ms per pose.
- **Tests**: `tests/test_horizon.py` (distances, extent, the pyramid and its lookups, the grid never
  below a DEM pixel, the sampled-DEM fallback and the block cache, the zoom fallback, the disc tiled
  exactly once with and without the cull, San Gorgonio kept from Whitney and dropped without the
  mountain, sea level past the horizon dropped at 2 m-10 km eyes, extra-tile counts, the near field
  and demand unchanged along a 134 m/s drive, L10 swap groups, the server flag / zooms / textures /
  eye); `test_server.MeshServer64HorizonTest` (a session: 80 km until the grid, ~170 km from a 3 m eye
  on the synthetic 1.5 km plateau, the climb to 10 km: a bigger grid, 545 km, L10 tiles at 128 px; a
  rebase's own grid); `test_protocol_agreement` (hello.horizon, status.radius, the horizon session,
  planet radius and margin on both sides); luajit `test_horizon`, `test_core_stubbed`.
- **Still to do in-game** (user not playing): P12 extended with `--horizon` at Whitney summit and at
  10 km: fps / p50 / p95 and perfmon with the distant tiles shown, objects, the L8-L10 import costs;
  screenshots of the horizon (the silhouette meets the sky's horizon band, no sky gap); read back
  atmoPlanetRadius and visibleDistance; no depth / shadow artefacts at 500-650 km; tune the haze.
- **Known engine issue, not patched: clouds over far terrain** (in-game 2026-10-11). From altitude
  (a plane at ~3 km) cloud fragments show on the terrain between its silhouette and the sky's
  horizon line, smearing as the camera tilts (like a Source-engine "hall of mirrors"). The volumetric
  cloud shaders (shaders/common/clouds) take reversed-Z depth <= 1e-6 for sky (beyond 100 km with the
  0.1 m near plane) and carry last frame's clouds forward where the scene depth does not reject them.
  Patching the threshold and the depth read (in the mod folder, then the user folder, with game
  restarts) did not remove it, so it was reverted: a BeamNG bug to report, not ours to work around.

## De-shadowing of the USGS imagery (`--deshadow`; built offline 2026-10-10, behind a flag, default off)
- Integration of docs/research/2026-10-10-deshadow-prototype.md, method A (method B dropped):
  `server/deshadow.py`, hooked into `imagery.Imagery(..., deshadow=obj)` (`_mosaic` swaps each USGS
  z12–z16 source tile for its relit copy; mosaic alpha 254 marks relit texels) and into
  `earthd.Server.new_anchor` (`prepare`: queue the z12 regions within 16 km, nearest first).
  `--deshadow off` builds no object: textures are byte-identical to the pre-hook code (sha256 of a
  multi-source render set pinned in `test_deshadow.FlagOffTest`).
- Per z12 region (~8 km), one background thread: z14 imagery + DEM z13 on the same pixel grid, sun
  fit (r² + 0.5 MCC, el ≥ 25°), gates r² ≥ 0.25 and class separation ≥ 0.9, lit / lam / prior grids
  (uint8 `.npz`), then classifier, halo-guard statistics, haze and sky/sun ratio from a few z16 tiles
  at the shadow edges and interiors (z14 alone gave the wrong haze and a too-tight guard: blotches and
  a dark rim in the relit shadow). Cached as `<cache>/deshadow/usgs/12/x/y.json`, shared by both modes.
- Per tile: grids sampled at the tile's pixels, shadow mask snapped onto the photo (Bayes + guided
  filter + linear mixing on the terminator + halo guard), gain capped at LAM_CAP 1.6 (slope part) and
  GMAX 10 (total, hue kept), chroma smoothing where boosted; gain-1 pixels keep their exact bytes.
  Relit tiles: memory LRU + `<cache>/imagery/usgs-ds1-<mode>/` (PNG, `.same` for unchanged tiles).
  `full` gives relit texels half the tone curve's shadow lift (`tone_lut(lift=0.5)`).
- Cost (16 cores shared with other builds, load ~6–8): region 2.5 s on the ravine with its tiles on
  disk (fit 1.3 s); per 256 px tile 20 ms at a shadow edge, 4 ms elsewhere with a cast shadow
  nearby, 7 ms lambert-only, ~1 ms unchanged (decided from the grids, before the neighbours are
  fetched), plus the PNG write.
- Real data (offline, tiles from the local cache): ravine 36.506,-118.402 fitted az 95 el 29
  (r² 0.28, just over the gate; the prototype's 3 km window gave 93 / 33.5 at r² 0.63), cast on,
  shadow lifted from sRGB ~(22,26,43) to ~(64,69,74) against ~(91,96,91) sunlit: cooler and flatter
  than the sunlit side (the prototype's known limits: no sky-view / bounce light, LAM_CAP on the
  steep shadowed wall). Central Valley flat 35.40,-119.20: gated off (r² 0.00). Kern Plateau
  36.20,-118.30: gated off (r² 0.08 over the whole region).
- Region not ready: as with the colour seams below (same mechanism): `render(pending=)` names the
  region as `(imagery.DS_PENDING, rx, ry)` (`Deshadow.pending_region`, asked before `relight` so a
  region finishing in between costs one extra render rather than a raw texture for good), the worker
  calls the server's listener (`add_listener`; changed = the region relights), `Deshadow.status` answers
  from memory / one stat call, and a tile on screen is re-rendered live (`far_retex`).
- Limits: regions are fitted separately, so two regions with different suns can show a step in slope
  shading at their border; z ≤ 11 source tiles stay raw; a region whose fit failed (network) is retried
  only when a render asks for it again, so its tiles stay raw until then.
- In-game check still to run: the ravine at 08:00 and 16:00 game time, `--deshadow full` against off.
## Colour seams of the USGS imagery (`--colormatch`; built offline 2026-10-10, behind a flag, default off)
- Integration of docs/research/2026-10-10-imagery-colormatch.md §3 ("seam feather") per its §7 plan:
  `server/colormatch.py` (pure numpy), hooked into `imagery.Imagery(..., colormatch=obj)` (`_mosaic`
  corrects each USGS z12–z16 source tile into a second LRU keyed (source, z, x, y, "cm1", relit); the
  raw `Tile` stays in `_lru`, so uniform / coverage-probe logic is untouched), into
  `Imagery.prefetch` (`request`: queue the tile's region) and into `earthd.Server.new_anchor`
  (`prepare`: the z12 regions within 8 km, nearest first). `--colormatch off` builds no object:
  textures are byte-identical to the pre-hook code (`test_colormatch.FlagOffTest`, the same pinned
  digests as `test_deshadow`). `hello.colormatch: "s2"` (settings page: "Imagery colour seams").
- Two cached stages per z12 region, each a pure function of source tiles, so a place comes out the
  same whatever order its regions were computed in and from whichever anchor (tested: fields and
  rendered bytes equal for two region orders; fields are stored float16 and used as stored, so a
  field read from disk gives the bytes of a freshly computed one):
  1. seams (`<cache>/colormatch/v1/usgs/12/x/y.seams.npz`, `.seams.none`): z14 USGS and EOX S2 2023
     (2016 per tile) over the region plus a 1-tile halo, reliability weights, IRLS, the oriented
     step filter (15 m grid, 36 angles, 5 x 240 m segments, the S2 gate), NMS, hysteresis and the
     component filter (>= 1.5 km, consistent step vector, and USGS carries the step: the median
     USGS step on the 300 m cut blurs over the component's median ratio step >= 0.5); per seam cell
     of the core: normal, local step (cut blur at 300 m), sub-pixel line from the z16 profile (z16
     tiles only along the seam; the centroid re-centred twice, ≤ 0.2 px on synthetic slants with
     texture). The USGS-share filter came out of the review: the S2 gate (|D_s2| − 0.03 < 1.5 |D|)
     lets a step in S2 only through (|D_s2| ≈ |D|, score ≈ |D|/3), so the edge of the per-tile 2016
     fallback and EOX compositing steps were "corrected" into USGS (Monument Valley window, 2016 on
     its west half: two false seams of 0.45/0.83/1.08 log; a 20 % step put into S2: one); research
     §5 assumed the gate rejected them. Now those are dropped (share ≤ 0.4; the AZ/UT seam 0.9–1.0,
     its output byte-identical); below about twice the texture's 45 m band-step noise a step in S2
     only cannot be told from a USGS step plus texture.
  2. field (`.npz`, `.none`): from the seams of the 3 x 3 regions, on a 30 m grid: half-steps
     scattered on each side, extended with the cut blur (300 m; 1500 m without support), faded with
     a Gaussian of the distance (σ_f 1500 m, smoothstep taper to 0 at 6 km = one region at most),
     clamped ±log 2; the band within 60 m of a seam keeps per cell its nearest seam cell, line and
     both sides' gains. Adjacent regions' fields agree to 6e-5 (log) on their overlap.
- Per tile (`Field.apply`): bilinear gain from the 30 m field (phase-sliced upsample); in the band
  each pixel takes its side's gain from its signed distance to the line (2 z16 px ramp); at z15/z16
  the |d| ≤ 1.5 px strip (the mosaic's blend row) is inpainted along the normal from d = ±2.5 px,
  with MARGIN px of the neighbouring tiles where it nears the edge (as the mosaic shows them: relit
  with `--deshadow`; raw neighbours put the strip ~20 % off there); z12/z13 average the band
  gains over each pixel. No seam within reach: the input itself (~0 ms). Gain-1 pixels keep their
  exact bytes.
- Order with `--deshadow` (§6, tested): raw → de-shadow → colour gain → tone curve. Seams are
  detected on the raw tiles (`Imagery.tiles`), the gain is applied to the relit tile, relit texels
  keep alpha 254 and their own tone curve.
- Region not ready: its tiles render raw; `render(pending=set())` collects the regions, `TileInfo`
  keeps them (`cm_pending`). The ColorMatch worker calls the server's listener when a field is done;
  on the event loop the far tiles waiting for it become `stale` if the field is not empty. A stale
  tile's texture (only the texture: `retexture`) is rendered again under `far_<aid>_<key>_r<rev>.dds`
  (the engine caches textures by path): live if a mod shows it, else by the next `tile()` when it is
  sent again. An evicted stale / re-rendered tile's next build continues the revision (`tex_rev`),
  and so does one evicted while a region was still pending (the listener cannot mark it any more, and
  its next build may render the corrected texture); eviction deletes every revision's texture with
  the tile (in-use / held / `RETIRED_KEEP` rules as for every tile file), `prune` with the anchor's files.
- Live retexture (`far_retex`, 2026-10-11; before it the corrected look appeared only after a
  teleport): a tile turning stale is queued (`retex_todo`); the pump on the event loop starts the
  re-render on the tile pool (`make_tile(prev)` → `retexture`, never on the loop) for the queued
  tile a connection shows, or holds sent but not swapped in, under its active anchor, nearest that
  connection's player first; at most `RETEX_INFLIGHT` = 1 at once (the other two workers keep
  building) and `--retex-rate` (`RETEX_RATE` = 5) starts per second; tiles nobody shows are dropped
  from the queue (re-rendered when sent again). A tile is re-rendered once per region of it that
  turns ready, however many (an L5 texture spans up to 3 x 3 z12 regions, an L6 one 4 x 4, as many
  de-shadow ones; review 2026-10-11: the first cut capped live re-renders at 8 revisions, which left
  coarse tiles partly raw); the loop guard is `RETEX_REPEAT` = 3 re-renders in a row that come back
  still waiting for a region whose status is ready (`TileInfo.retex_repeat`), then the tile waits to
  be sent again. A region that failed (network; retried later) reports not ready (`status`), whatever
  its cache folder holds (a file there is one renders cannot use: another version / mode / reference
  chain): calling it ready re-rendered a shown tile in that loop, or dropped it from the region's
  waiting list. A live re-render that raises (not the network: a bug, a full disk) is logged
  (`live retexture failed`, the first 3) and leaves the tile stale; `prune` also drops the anchor's
  tiles from the regions' waiting lists (after a teleport the far regions never finish). When a re-rendered texture lands (`_tile_done`, also for the send path), every
  connection showing / holding the tile gets `far_retex {anchorId, key, texture}`; a far_tile sent from
  a build that finished before a live re-render carries the newest record's texture, and a stale one
  being sent is queued at once. All regions of one ready event are queued before the first start, so
  the nearest goes first. Mod: `farfield.onRetex` sets the new file on the shown and the loading
  instance of the key (a material not created yet takes it from there) and `satmat.requestRetex`
  queues the switch; `update` runs at most `RETEX_PER_FRAME` = 2 switches a frame as governor unit
  `retex` (prior 1.0 ms, not measured yet; never in a heavy or quiet frame): `colorMap` + `flush` +
  `reload` (as dev.lua's tint), the fallback tint cleared if the material was created without its
  file, pcall'd and counted (`retexFailed`) so a bad switch never throws every frame. Stale messages
  (another anchor, a key neither shown nor loading, an older revision, a missing file) change nothing;
  a queued switch older than the texture the material took meanwhile (its tile went before the switch
  ran, the key was re-sent with a later revision and `ref` pointed the unused material at it) is
  dropped, never going back (review 2026-10-11).
  `satmat.ref` follows the same rule for a re-sent key: a later revision of the same tile's texture
  (same folder, same stem) is switched in for the in-use material (time-sliced) instead of kept;
  another session's file under the same name keeps the old rule. Offline (`test_retex.py`, synthetic
  imagery, 64 cells): 110 shown tiles of one region re-rendered and sent in 1.96 s at rate 200 (render
  median 10.6 ms, max 40 ms; real colour-matched tiles cost more), i.e. ~22 s at the default 5/s.
- Threads: the region worker computes; tile workers read fields from disk (`field`, without holding
  the ColorMatch lock); the calls made on earthd's event loop (`prepare` at an anchor, `request`
  from prefetch, `status` when a tile is done) look at memory, the queue and the files' existence
  only, never decoding a field (~4 ms each).
- Real data (offline, the research windows served from the prototype's cached tiles, no network):
  Monument Valley's AZ/UT line ΔE 17.6 → 0.77 (200 m bands) and 17.9 → 0.56 (20 m), survey step
  +62/+96/+50 % → +0.3/+1.8/+0.2 %, mean change 9.7 levels, log-HF ratio 1.005 (the prototype's
  numbers); Central Valley and Baltimore: no seam, no byte changed.
- Cost (16 cores, single worker thread): detection 20–25 s per region window (1536² cells), so the
  first field of a cold place needs its 9 windows (~3.5 min; ~85 s for the research window, whose
  edges have no data); a field from cached seams ~1 s. Per 256 px tile: 2.2 ms median / 4.2 ms max
  on the real seam window (1.2–2.9 ms smooth, 4–6.7 ms band tiles on synthetic ones), ~0.02 ms
  unchanged.
- Limits / follow-ups: moderate seams in farmland and cities are not detected (research §5: a
  geometric prior along candidate lines; a state-boundary prior only from a generic boundary dataset,
  never hard-coded lines); z10–z11 source tiles (far rings) stay raw; detection speed (30 m grid, candidate
  prefilter); de-shadow could fit its sun per flight using these seams (§6); in-game check of a
  handful of known state-line seams before default-on. Found in review, not fixed: a field reaches
  6 km into its neighbours, so where a corrected region meets a raw one (a region beyond 24 km from
  the anchor, which is never queued; L5 tiles at that distance still use z12 sources; likewise the
  z12/z11 switch between L5 and L6) a seam's half-step stops at the z12 tile edge: a straight step
  of up to half the seam's step, ±6 km along that edge. While a region is pending the same step is
  temporary. A fix needs the fields out to the reach of the z12 sources, or the z12 fields applied
  to the z10/z11 tiles.
## Real weather, level parity, gravity (quick wins 1, 2, 4; built offline 2026-10-10, feature/weather)
docs/research/2026-10-10-real-weather.md, 2026-10-10-game-feature-quick-wins.md §5.1-5.4, probe facts
in 2026-10-10-weather-probe-results.md.
- **Level objects** (`tools/make_level_assets.py` → `main/MissionGroup/Weather/items.level.json`, a new
  `Weather` group): the CloudLayer `bngEarthClouds` (the fields sky.lua used to create at runtime; sky.lua
  keeps `createObject` as a fallback and then resets core_environment's per-class object cache), two
  camera-following Precipitation objects `bngEarthRain` (`rain_drop`, BWM_Weather's verified 0.39 setup)
  and `bngEarthSnow` (`Snow_menu`), both `numDrops 0` and without a `numOfDrops` field (so BWM_Weather
  keeps its own rain object), a ForestWindEmitter and a 2D reverb SFXEmitter (`snapshot:>Reverb_WC_Open`).
  LevelInfo gets `temperatureCurveC [0,15,1,15]` (ISA until real weather: core_environment now drives
  the engine's temperature and the menu stops showing −273 °C).
- **Server** `server/weather.py` + three hooks in earthd (`Server(weather=...)`, `Connection.weather`
  in `on_pose`, re-send on `hello`; `--weather on|off`, default on; the credit "Weather data by
  Open-Meteo.com" in `hello.attribution`). One keyless GET per fetch (the project User-Agent, through `netfetch`), at the 0.1°
  cell of the player, when there is none yet, every 600 s or after 25 km; cached per (cell, 15-min
  slot). `derive()` keeps the physics (rates per hour from the 15-min sums, fog-code visibility,
  cloud base, sea-level temperature, winds aloft, the real UTC offset); the `weather` message is
  additive (no protocol bump) and in the agreement table. Fixtures: Baltimore and Whitney, recorded
  2026-10-10.
- **Mod** `beamngEarth/weather.lua`, its own extension `beamngEarth_weather`
  (`scripts/beamng_earth_weather/modScript.lua`; core.lua only forwards the message; the BeamNG.Earth
  window shows a one-line summary). In the BeamNG.Earth level only. Every value approaches its target
  exponentially (95 % in 60 s), written at 2 Hz (ground wind 1 Hz) and only when it changed, through
  core_environment (the pause menu shows the real values): coverage from max(low, mid) through
  `COVERAGE_CURVE` (probe rows to 19 %, guesses above, retune there), cirrus from high cloud (density
  0.3-0.6, the CloudLayer default 0 hides cirrus), drift from the 700 hPa wind, fog density
  3.912 / V × exp(groundZ / H) (the engine's fog thins with absolute z, `sceneFog.h.hlsl`; capped at the
  menu's 0.05), ground wind from the 10 m wind with an OU gust term up to the gust speed, rain/snow
  as `setPercentage(sqrt(rate / full))` of a 4000/3000-drop pool (none above the cloud base), and a
  flat `temperatureCurveC` at the sea-level temperature (LevelInfo setter found by read-back, editor
  dirty for 3 frames so core_environment re-reads it). The sun dims under the deck: ScatterSky
  `brightness` = the level's value × (1 − 0.7·deck^1.5) (the clouds cast no shadows, so under
  overcast the car kept a sharp sun shadow; in-game at noon 0.35 made it faint while the auto
  exposure kept the scene's brightness); restored in manual mode and for missions, unless someone
  else changed it since. The cloud base above the ground goes to
  sky.lua (it keeps the ground-following altitude and the rebase weather-offset shift); the real UTC
  offset too (`sky.todFields(..., real)` within 200 km of the reported point; Singapore +8, Delhi
  +5.5 instead of the meridian guess).
- **Modes** (changed 2026-10-11, "keep it on real and switch to manual if edited"): `'real'` (default)
  / `'manual'`; `'auto'` (the old BWM_Weather-driven default) is accepted by `setMode` and from a
  saved setting and means real. Writes wait 20 s after a level start (BWM's start transition, env's
  `setState(init_env)`). Afterwards an onEnvironmentChanged that is not from our own calls (the
  `applying` guard) and really changes a weather value (`WEATHER_KEYS`: cloud cover, cirrus, cloud
  wind direction / speed, fog density / height, ground wind, cloud altitude, precipitation; compared
  with the engine's value through the core_environment getters, since the hook fires before the value
  is applied; fog in either scale, setState carries the menu's x1000; keys without a value to compare
  count as changes; every tolerance is at least half the menu's slider step, since its number field
  sends the value rounded to the step when it loses focus, even unedited) stops all writes at once (3 s quiet, env's 1.15 s menu lerp) and, unless a
  mission starts within 2 s (its setup comes just before `started`), switches to manual: rain and
  snow hidden, the level's sun brightness back, one toast ("Weather set to Manual because it was
  changed ...", naming BWM_Weather when it is changing the weather at that moment), and
  `beamngEarth_settings.set('weatherMode', 'manual')` so the settings page shows it (its apply skips
  a repeated manual; Real is always applied, which clears a gravity change as before). A setState
  repeating the current values is no change. `setGroundWind`
  applies before its hook, so the ground wind is compared with the value we wrote last. BWM_Weather
  no longer switches anything by itself; its changes count like the player's. Choosing Real again
  drives everything again. A cloud altitude someone else sets is kept in manual mode too (the layer
  stops following the ground until Real). Missions and the big map (`freeroam_bigMapMode`: fog 0, clouds 0, its own
  time, put back as it closes; `onBeforeBigMapActivated` / `onDeactivateBigMapCallback` + 2 s) pause
  it and their changes are not edits. Gravity is its own setting: a gravity change by the game (the
  menu's presets) is kept until `setMode('real')`, as before, and does not switch the weather.
  sky.lua does the same for the time: in the BeamNG.Earth level, after its own 20 s level-start
  grace, outside missions and the big map, a `time` more than 0.002 day (~3 min) off both the
  TimeOfDay and the real clock, another date, `play = false`, a day length other than 86400 s or
  another UTC offset (`sky.timeChanges`) switches the sky to manual (saved as `skyMode`, one toast).
  Its own re-syncs write the TimeOfDay fields directly and never reach the hook. Missions and the big
  map also pause the re-sync now; it runs at once when they end. After a Lua reload mid-level both
  read the saved mode back (`beamngEarth_settings.get`): settings.lua loads and applies first, and no
  level start follows to apply it again.
- **Gravity** by latitude and height (WGS84 Somigliana minus 3.086e-6·h; 9.7803 equator, 9.8322 pole)
  through `core_environment.setGravity` when it changes by 1e-4 (~30 m of climb); `GRAVITY_ENABLED`
  (on); a menu gravity change is respected; off restores 9.81.
  - Real gravity (after the merge): earthd's `--gravity on` (default) computes |g| with
    `server/gravity.py` (normal + EGM2008 anomaly at the pose's lat/lon/height above sea level) on
    its own thread, after the first pose and whenever the player moved > 2 km or > 200 m in height,
    and sends the additive `gravity` message (`v`, `g`, `lat`, `lon`, `h`, `source`). Without the
    EGM2008 cache it sends normal gravity (`source: "normal"`) and downloads the 74 MB in a daemon
    thread; the next pose after it is ready recomputes. The mod uses `g − 3.086e-6 · (z − h)` instead
    of the latitude formula while it has a value (cleared on `hello`), and moves the engine's value
    by at most `GRAVITY_STEP` (1e-3 m/s²) per 1 s check once its own is set. Settings page: "Real
    gravity" (on) = `GRAVITY_ENABLED`.
- Tests: `server/tests/test_weather.py` (fixtures, codes, cache, refetch rule, server, the Lua mapping
  under luajit, the level objects = generator output), `mod/tests/test_weather.lua` (pure conversions
  + a stubbed core_environment run: grace, convergence, writes only on change, the switch to manual
  and what is no change (repeated values, our own writes, grace, missions, the big map, gravity),
  BWM, gravity, rain objects, temperature curve, other levels untouched), `test_sky.lua` (real
  offset, the same switch matrix for the time), `test_settings.lua` (the 'auto' migration),
  `test_core_stubbed.lua` (forwarding). Still to check in-game: the temperature-curve setter form,
  `setPercentage` drops visible, coverage calibration above 0.95 (overcast), fog k = 1/H, wind push.
## Dev tools and the pause-menu settings page (docs/DEVTOOLS.md; built offline 2026-10-10)
- `dev.lua` (extension `beamngEarth_dev`): in-world overlays (per-tile LOD tint, tile outlines by
  state, loading heat with the next imports numbered, physics block squares, rings, under-car
  probe) and imgui panels (stats, 60 s graphs, mini-map with click-to-inspect, controls, event
  log). Off by default; off = no per-frame work. Tiles come from `tileindex.lua`, kept current by
  `farfield.setWatcher` only while the tools are on.
- `settings.lua` (extension `beamngEarth_settings`) + `ui/ui-vue/mods/beamngEarth/` (index.js,
  EarthSettings.vue): a "BeamNG.Earth" button in the pause menu's Mods tab, values in
  `/settings/beamngEarth.json`, defaults = today's behaviour. With the page, the imgui window is a
  compact HUD hidden at level start (setting "Status HUD").
- Tests: `mod/tests/test_dev.lua`, `mod/tests/test_settings.lua`. The Vue files are checked
  in-game only.

## Network: rate limits, failover, offline (`server/netfetch.py`; built offline 2026-10-10, merged to main)
docs/research/2026-10-10-rate-limits.md §5. Before, the thread pools were the only limiter (cold start
~30 req/s to USGS, up to ~80 req/s to Mapterhorn, the same after every teleport), 429/503 and
Retry-After were treated like any error, the DEM had no breaker and a Mapterhorn error never fell
over to AWS, a 400 wrote a permanent `.404` marker, and the User-Agent lived in two copies.
- **One `Fetcher` per server** (`main_async`), handed to `Dem`, `Imagery`, `weather.fetch` and
  `gravity.Provider` (and through `Imagery`, the de-shadow and colour-seam region fetches, both as
  kind `background`). Per host (`POLICIES`, §5.2 for today's hosts; unknown hosts 2/s, 2 at once, no
  prefetch): a token bucket and a concurrency semaphore (apart from the pools), priorities (demand
  waits up to 30 s; prefetch / background take a token only above 1/2 / 3/4 of the burst and are
  dropped, not queued), 3 attempts with full-jitter backoff, Retry-After pausing the whole host (bucket
  emptied, breaker open for that long), a circuit breaker (5 failures in 30 s; 30 s open, doubled per
  failed probe up to 15 min), daily soft/hard budgets in `<cache>/netstats/<UTC date>.json`, one
  summary log line a minute. Injectable clock, sleep, rng, transport and wall clock (tests).
- **Failover**: `HostUnavailable` / `RateLimited` cost no network and the chains move on: imagery
  usgs → eox2023 → eox2016 (one host, one breaker: both skipped together) → palette; DEM Mapterhorn →
  AWS for the same key (also on Mapterhorn errors now) → `DemUnavailable` (where a coarse offline DEM
  will go). A prefetch future the limiter dropped is re-fetched as demand by the render that needs it
  (one fetch for every render waiting on it); prefetch skips a source whose circuit is open, so it
  warms the source render fails over to. An undecodable DEM tile leaves the cache and fails over.
- **Review fixes (2026-10-10)**: only the half-open probe closes a breaker (a late success of a
  request sent before it opened no longer does); demand requests waiting for a token when a
  Retry-After pauses the host give up at once and fail over (they used to sleep out the pause and
  then all fire); `HttpsTransport` re-sends only on a reused keep-alive connection (a fresh one that
  timed out was tried twice, outside the bucket, doubling every timeout).
- **Markers**: 404/410 only (a 400 is an error); `.404` files keep their format (empty, mtime = when)
  and expire after the host's `miss_ttl` (90 days; AWS never), but count as valid while the host
  cannot be asked, so an expired marker never turns a "no data" into an error. Existing caches work
  as they are.
- **Offline / degraded**: `--offline` makes every request raise `Offline` (disk hits only, no weather
  fetch, no EGM2008 download); `Fetcher.offline` is also true when every imagery and DEM host is open.
  `Server.net_note()` → every `status` text gets " | Offline - using cached data" or " | Degraded:
  <hosts> unreachable; <n> tiles without imagery" (re-sent when it changes; unchanged text otherwise,
  so the golden tests are byte-identical). Palette-fallback tiles are recorded in `Server.degraded`.
- **Measured (offline, fake clock; `test_netfetch.ColdStartTest`, `test_demand.NetDemandTest`)**:
  the cold-start / teleport burst of the default layout (64 cells) is 1,431 USGS tiles at Baltimore,
  1,185 EOX tiles at Munich, 177–237 Mapterhorn tiles and up to 300 AWS tiles (no lidar). Through the
  limiter: USGS 60 at once, then 15/s (91 s in all, ≤ 74 in any second, 959 in the first minute;
  before: ~30 req/s for 45–130 s); EOX 30 at once, then 6/s (193 s; before: as fast as 6 workers
  allowed, so EU cold starts now fill their imagery about 2–3× more slowly); Mapterhorn ≤ 99 in any
  second, 7.8 s (before: up to ~80 req/s sustained); AWS 4 s. Steady driving, busiest minute (cold
  cache, rebase every 10 km): 134 m/s USGS 797/min (13.3/s of 15), EOX 310/min (5.2/s of 6),
  Mapterhorn 140/min (2.3/s of 20), AWS 304/min (5.1/s of 50); 30 m/s at most 3.8/s. The 250 m/s
  flight (~21 req/s to USGS, §2.2) is capped by the bucket.
- **Not yet**: re-texturing degraded tiles once a breaker closes (the tile's files keep their name and
  the engine caches by path, so it needs a versioned name and a replace swap), the near-terrain
  heightmap prefetch as kind `prefetch` (it is demand today), Mapterhorn revalidation after 180 days,
  the per-host state in `status` for the dev tools; see docs/ROADMAP.md "Robustness".
- Tests: `server/tests/test_netfetch.py` (no network: fake transport / clock / sleep, seeded rng;
  the cold-start bursts above), `test_demand.NetDemandTest` (§5.4 case 12: requests per minute and
  host from the real `farfield.select` drive, below each policy rate), plus the netfetch cases in
  `test_imagery.py` (EOX breaker shared, marker TTL, 400, dropped prefetch fetched once, prefetch past
  an open host, UA) and `test_weather.py`
  (UA, shared in-flight fetch, Retry-After retry).

## Data credits overlay (docs/DEVTOOLS.md; built offline 2026-10-10, merged to main)
- Server `credits.py`: each built tile records its imagery sources (from `Imagery.render`'s texel
  counts) and DEM providers (`dem.Dem.served`, set on every path of the netfetch failover:
  Mapterhorn, AWS after a Mapterhorn miss, error or open circuit, none); with `--colormatch s2` a
  texture whose USGS tiles the seam feather changed also names the S2 references (`render`'s
  "colormatch" count), and a stale tile's re-rendered texture (`retexture`) takes the new render's
  sources; per connection a `credits` message (sources within
  2 km of the player, kept up to 2.2 km against flip-flopping, + the far field's other sources +
  Open-Meteo; none before a tile's sources are known) on change, at most once a second, re-sent on
  hello (`--credits on`, the command line's default; `Server()` sends none).
- Mod `credits.lua`: one background-draw-list text per frame in a screen corner (settings: on,
  corner, size), hidden in the pause menu and big map; the session's attribution line before the
  first message. No allocation per frame; the text is measured only when it changes.
- Tests: `server/tests/test_credits.py`, `test_protocol_agreement.py`, `mod/tests/test_credits.lua`,
  `test_core_stubbed.lua`. Still to check in-game: readability, the pause-menu / big-map hiding.

## Saved places (quick win 1; docs/DEVTOOLS.md; built offline 2026-10-11, branch feature/places)
- Mod `places.lua` (a module, nothing per frame): saved places `{ id, name, lat, lon }` and recent
  places `{ lat, lon, name?, kind }` in `/settings/beamngEarth_places.json` (apart from the settings
  file; jsonReadFile / jsonWriteFile, lat/lon rounded to 1e-6). No file yet: the list starts with
  `places.SEED` (docs/PLACES.md's first table, then El Capitan Meadow, Pikes Peak, the Fuji Subaru
  Line 5th station, Everest North Base Camp, Stelvio Pass, the Blue Mountains) and is written at
  once, so an emptied list stays empty. A file that cannot be read (or is not a places file, i.e.
  has no `places` list): one warning (log and page), its text copied to
  `beamngEarth_places.json.bak` (readFile / writeFile), an empty list; broken entries are skipped
  with one warning after the same copy, duplicate ids (and ids / nextId at or above 2^31)
  renumbered. The file is never overwritten with less than it held without that copy: when the copy
  cannot be made, nothing is saved for the session (the warning says so; the lists still work).
- Recent places: `beamngEarth_core.teleport(lat, lon[, name])` records every teleport it sends
  (`kind = 'teleport'`, the place's name when it came from the list) and the level start's own
  (`kind = 'start'`, from `onLevelStart` or the first hello); newest first, an older one within 200 m
  goes (its name stays when the new one has none), at most 10. A refused teleport (no server,
  invalid lat/lon) is not recorded. The recording is a pcall: a file problem never stops a teleport.
- Go (`places.goTo`): only with `beamngEarth_core` loaded, the server connected and the BeamNG.Earth level
  loaded (`_state().inOurLevel`) and the ground where the player is in (`groundReady`: no teleport under way, set
  down, no drop hold; never for good: a failed teleport waiting to be asked again, or one unanswered for
  `GO_WAIT_MAX_S`, lets another place be asked), else the reason (the page shows it and disables Go); then
  `beamngEarth_core.teleport(lat, lon, name)`. No car is needed: with none the free camera goes to the target
  (core `moveCamera` in the commit frame, then over the ground there in `placePlayer`, held as the player until it
  reads back there; a later move down onto a tunnel's floor leaves a camera flown away alone); with a car and the free
  camera on, the camera goes along behind the car, over the ground there. luajit `test_teleport_nocar.lua`. Still to
  check in-game: that `core_camera.setPosRot` moves the free camera in single player as it does in BeamMP.
- `places.parseLatLon(text)`: "36.5785, -118.2923" (Google Maps' copy), space-separated, N/S/E/W as
  prefix or suffix (either order: a N/S coordinate is the latitude), degrees-minutes(-seconds) with
  or without °'" (and ′″, no-break spaces); refuses one number, three coordinates, two latitudes,
  "-36 N", minutes/seconds >= 60, out-of-range values.
- Settings page bridge (settings.lua): `uiPlaces`, `uiPlaceAddHere(name)`, `uiPlaceAdd(name, text)`,
  `uiPlaceRename(id, name)`, `uiPlaceDelete(id)`, `uiPlaceGo(id)`, `uiPlaceGoText(text)`,
  `uiPlaceGoRecent(i, lat, lon)` (the row's lat/lon decide: a Go moves its place to the front, so
  a double click's index is stale), each answering `{ ok, error, places, recent, cannotGo, here, warning }` (in a
  pcall: an error inside answers `ok = false` with it, never nothing).
- Tests: `mod/tests/test_places.lua` (seed on the first run only, round trip, corrupt files, recent
  dedup / order / cap, parsing, go and its checks, the bridge; the review's stale recent index,
  unreadable file without a copy, non-places JSON, skipped entries, huge ids),
  `test_core_stubbed.lua` (the level start and console teleports recorded). The Vue section is
  checked in-game only.

## Download cache: size cap and LRU eviction (`server/cache.py`; docs/ROADMAP.md "Caching" 1, parts of 3 and 5; built offline 2026-10-11, feature/cache-cap)
Before, every raw download stayed on disk forever (~/.cache/beamng-earth: 2.4 GB, ~43k files after
three days of testing: dem 741 MB / 7.5k files, imagery 1.4 GB / 35k, deshadow 74 MB, gravity 145 MB).
- **Bookkeeping**: one `cache.Cache` per server (`main_async`; `Server(cache=...)`, None by default, so
  `Server()` and the golden streams are unchanged). Each producer has a `disk` attribute (`cache.NONE`:
  plain reads, no bookkeeping) that earthd sets: `Dem` / `Imagery._download` read through `disk.read()`
  (marks the file used before opening it, a miss is None: an evicted file is simply downloaded again)
  and report writes, 404 markers and removed files; the de-shadow region / relit-tile caches and the
  colour-seam fields report their loads and `_atomic_write`s. Last use = that record (one dict update
  under a lock per hit; no stat per request, no atime, which relatime makes unreliable), persisted in
  `<cache>/index.tsv` every 10 min when it changed, after a clear and at shutdown. At start a
  background walk takes sizes, and the mtime (download time) as the last use of files the index does
  not know (the existing cache is adopted as it is); nothing is evicted before it is done.
- **Eviction**: over the cap (default 20 GB, `--cache-cap`, the page's choice persisted in the index),
  the least recently used files of `dem/`, `imagery/`, `colormatch/`, `deshadow/`, `mapdata/` (by
  mtime: earthd does not use MapData yet, so nothing there is protected) go, down to 90 % of
  the cap, at most every 3 min. Never: files this server process used within the last hour (checked
  again under the lock per file, so a tile worker's read during a pass wins), `gravity/` (counted
  only), `netstats/` and unknown directories (not tracked), empty markers (cleared only). Directories
  stay (a producer may be between `makedirs` and its write). A de-shadow region whose grids were
  evicted while its parameters were in memory is computed again (it used to stay "pending").
- **Clear** (the page's button, `cache` with `clear: true`): the same files regardless of the cap,
  markers included, but protecting everything this server process used (the current session's files,
  not only the last hour's), on an executor; the answer carries `clearedBytes`. `usage()` (the
  message, built on the event loop) reads the totals without the lock, which a pass over a full cache
  holds for a few tenths of a second. A missing or empty `VERSION` is written atomically; a
  non-finite time or an overflowing cap in the index is damage (skipped), never a value.
- **Layout version** (ROADMAP 5): `<cache>/VERSION` = 1, written when missing; any other value (a newer
  server's layout, or garbage) leaves the cache alone: counted for the page (`managed: false`), never
  evicted, cleared or indexed. Producer versions are in the paths (`colormatch/v1/`,
  `imagery/usgs-ds1-<mode>/`): after a generator update the old directory is never read again and
  ages out through the LRU, so only what changed is recomputed.
- **Lifetimes** (ROADMAP 3, the cheap part): `cache.LIFETIMES` (path prefix -> max age after the
  download), enforced by the start-up walk. Empty: USGS (public domain), EOX (CC BY / CC BY-NC-SA),
  Mapterhorn and AWS Terrain Tiles set no caching-time limit; 404 markers expire through netfetch's
  `miss_ttl` (90 days). Background refresh of stale tiles is not built.
- **Protocol** (additive, no bump; server/README.md): mod -> server `cache` {`capGb`, `clear`} (both
  optional; neither = fresh numbers), server -> mod `cache` {`v`, `usedBytes`, `capBytes`, `files`,
  `ready`, `managed`, `clearedBytes` after a clear}, after `hello` and in answer to every mod `cache`.
  The mod sends the page's cap after every `hello` and on change, keeps the last `cache` for the page
  (`core.cacheState()`), and the page asks for fresh numbers every 5th 2 s poll (docs/DEVTOOLS.md).
- Memory ~200 B per tracked file (a full 20 GB cache of ~30 KB imagery tiles: ~130 MB); the walk of
  today's 43k files took 0.13 s (warm, read-only timing) on its own thread.
- Tests: `server/tests/test_cache.py`, `test_protocol_agreement.py` (`cache` both ways in the session,
  the page's cap values = the settings schema's), `mod/tests/test_settings.lua` (the cap setting, the
  usage line, the clear bridge, the poll), `test_core_stubbed.lua` (cap on hello, the kept message, the
  requests only with a session). Still to check in-game: the page section (select, the confirmation
  row, "Clearing..." / "Freed"), a clear while driving (nothing around the player re-downloads), the
  start-up walk's time on the real cache, and the log line of the first eviction with a small cap.

## Robustness: no server, lost server (docs/ROADMAP.md "Robustness" 1-3; built offline 2026-10-11, feature/robustness)
- **Banner** (`banner.lua`, docs/DEVTOOLS.md "Connection banner"): a pure state machine fed once per
  frame by core.lua from net.lua (state, `attempts()`, `everConnected()`), the server's hello and the
  network note of its last status (`banner.noteOf`: "Offline - using cached data" / "Degraded: ...",
  server `net_note`), and the edge guard's mode. Drawn on the background draw list at the top centre,
  hidden in the pause menu / big map (`credits.hiddenByUi`), in our world only. `core.banner()` feeds
  the settings page.
- **Edge guard** (`edgeguard.lua`): core.lua's `serverHealthy()` = connected, the hello arrived, an
  anchor block committed (terrain-follow never moves the level-start flat block), no resync pending
  (`st.resync`, from a hello while an anchor is committed until that server's resync anchor commits;
  a failed resync is asked again every `rebase.RETRY_S`), no terrain request unanswered for `STUCK_S`
  (5 s) and no terrain_error within `ERROR_HOLD_S` (10 s). Only when it is false (and physics runs, the player is placed) does `updateGuard` run, after the
  frame's world update (a block commit has already moved the block), against the collidable area of
  the committed block (`lattice.groundBox`; before any anchor, the level-start flat block). Far inside
  it returns without a call; near an edge one `castRayStatic` down from the vehicle decides "on the
  ground" (within 6 m); on the ground the velocity towards each edge is capped at
  sqrt(2 DECEL (d - STOP_M)) by setting the velocity (`applyClusterVelocityScaleAdd(-1, 0, vx, vy,
  vz)`, idempotent if the reading lags a frame; an add would brake twice); off the block and 3 m below
  the ground it last stood on (within 10 s), the car is set down `STOP_M` inside
  (`spawn.safeTeleport(..., false)`: no reset, as the game's own upright recovery) once per second.
  Off the ground (jumps, aircraft) nothing is touched. Any error turns it off once (logged). An
  anchor commit (a new frame) forgets the remembered ground height (`edgeguard.newFrame`).
  Offline: 30 m/s rests 15 m inside, braking <= 6.6 m/s^2; 134 m/s on a 512 block stays on it (cut to
  the profile when it engages); an aircraft at 500 m flies off untouched.
- **Session adoption** (server `Server._scan_sessions / claim_sessions / release_session /
  sweep_sessions`, mod `farfield.heldSessions`): see server/README.md "Run" and the protocol table
  (`hello.sessions`, `pose.sessions`; the server's own messages are unchanged, so a default session is
  byte-for-byte as before). farfield.lua keys previous anchors' tiles by (session, anchor, key),
  `heldAnchors` names only the current session's anchors. Until the resync with a restarted server
  commits, the committed anchor id is the old session's while the new server numbers its own from 1
  again (its resync anchor is usually 1, as is a mod that never rebased): core.lua `anchorKnown()`
  holds back every pose and terrain request meanwhile (review 2026-10-11: a pose naming anchor 1 made
  the new server switch to its anchor 1 before the mod did and stream tiles the far field filed under
  the old session; the server then never re-sent them, holes once the old tiles went at far_done;
  `test_restart.RestartDriveTest.test_kill_and_restart_anchor_1`). hello still names the folders.
  The server's sweep removes unclaimed orphans in a worker thread (`sweep_sessions_async`; a session
  is thousands of files), and a folder carrying the new session's own id is never an orphan.
  Material names (2026-10-16) are pooled per server session (`bngEarthFar_<session>_<n>`, server/matslots.py),
  so a new session never repeats an old session's name (the old limit: a repeated `bngEarthSat_<anchor>_<key>`
  kept the old tile's texture).
  Pooled slots (2026-10-16): the game's frame cost grows ~0.3 ms per 1000 distinct shape files it ever loaded
  (shadows and reflections; a game restart clears it), and every far tile used to be a new file. Slot n is a
  material name and a shape file `slot_<n>.cdae` (a hard link to the tile's own `far_<a>_<key>.cdae`, replaced
  atomically for the slot's next tile). Each far_tile with one carries `slotSerial`; the mod reports
  `[slot, serial]` in `pose.slotsGone` once that message's object (or never-spawned instance) is gone, lists what
  it still holds in `hello.slotsHeld` after a reconnect, and the server hands a slot to another tile only when no
  connection holds it. The mod keeps pooled Materials (satmat.lua) and deletes a spare or doomed object still
  holding a pooled path before loading it again (farfield.lua freeSlot).
  Load order and teleports (2026-10-16): the server's build queue and the mod's imports take the far tiles
  nearest the camera first, a tile outside the camera's view cone (half the horizontal field of view + 15 deg)
  counting 3x as far (farfield.view_distance; earthfast.view_distances; farfield.lua peekImport), so what is
  on screen fills first. The anchor's first terrain block loads its elevation tiles through an urgent lane
  (dem.Dem.urgent: its own fetch threads, ahead of the shared queue the far field, road plans and ground-ahead
  fill): on a first visit the car waited 38-54 s, then 18 s. Screen-space LOD (farfield.View, --lod-px, pose
  cam.h): tiles also split while their split would pop more than N px on screen; off by default (Yosemite: 4 px
  cost 17 % fps and +36 % tiles for ~25 % smaller pops; cliffs' "errors" are mostly sideways shifts).
  A teleport (prepare_anchor > 20 km from the player) pauses the old place's work on that connection (its far
  tiles, road / tree / building tiles, prefetch, ground-ahead) until the new anchor commits, fails or 120 s pass:
  right after a level load the first block waited ~30 s for the CPU, then 7 s. Road, building and far-building tile
  meshes are pooled shape files too (`roads_tile` / `bldfar_tile` `slots` {what: [path, serial]}; roads.lua reports
  a slot once its tile is dropped and its last object deleted).
- **Tests**: `server/tests/test_restart.py` (the fault-injection drive: a 64-cell / 512² session
  killed mid-drive and restarted in the same folder; 4127 coverage checks within 512 m without a hole,
  no vanished file, the guard warned and held the car 15 m inside the edge during the 25 s gap and
  until the resync committed, never while the server was healthy, the old folder held while named
  and deleted after the mod let go; run with the mod's anchor 2 and with its anchor 1, the resync
  commit waiting the governor's `CALM_TIMEOUT` so poses go out meanwhile),
  `test_protocol_agreement.SessionsAgreementTest`, `test_modsim` (the guard's step and the
  farfield sessions, Lua against port), `test_server.SessionCleanupTest`; luajit `test_banner`,
  `test_edgeguard`, `test_core_robustness`.

## Ground surfaces under the physics block (`server/surface.py`, `earthd --surface on`; 2026-10-11)
- The TerrainBlock is physics-only in mesh mode, so its layer map only decides the tyres' ground model
  (and GroundCover, which binds to a layer). `make_heightmap` keeps the block's lat/lon/height grids
  (`heightmap.generate(keep=)`), `Surface.classes` reads the WorldCover class under every 2 m sample from
  the map-data cache (z14 cells, `mapdata.MapData` "landcover", no DuckDB needed) and `classify` maps
  it to MATERIALS (§5.4 of docs/research/2026-10-10-world-data-sources.md: built-up asphalt, tree /
  grass / moss grass, shrub dusty dirt, bare loose dirt, cropland dirt, snow, water / wetland mud; built-up
  dirt since 2026-10-14: as asphalt it washed whole towns grey, the roads are painted asphalt anyway) with
  ROCK where the slope is above 35° (snow stays snow). One 0/255 mask per material
  (`hm_<a>_<cx>_<cy>_m<i>.png`, rows as the heightmap's) goes next to the heightmap; the terrain message
  names them (`layers`, `materials`) and terrain.lua imports 8 greyscale masks (+4-5 ms per 512²
  import, docs/research/2026-10-11-engine-probes.md), making the 7 extra TerrainMaterials as runtime
  clones of `beamngEarthDirt` with other `groundmodelName`s (they join the material set by themselves).
  Any unusable layer list falls back to the single dirt layer.
- Organic borders: WorldCover's 10 m square pixels gave stair-stepped, ruler-straight class
  borders that the grass made visible (Monument Valley, 2026-10-11). `surface.warp` moves every
  sample's land-cover lookup by smooth value noise on a fixed world grid (40 m / +-8 m and
  12 m / +-3 m octaves, at most ~10.5 m), so borders curve while patches keep their size and place.
  It is a pure function of the world position, so neighbouring blocks agree. Cost: ~64 ms per 512²
  block on the heightmap thread.
- Never waiting: a cell not cached yet reads as no data (dirt, as before) and is fetched in the
  background (1 thread, netfetch kind "prefetch", 3 km around the player at most every 500 m); the
  next blocks have it. `surfaceComplete` in the heightmap info says whether every cell was there.
- Masks are evicted with their heightmap. On by default since 2026-10-11 (`--surface off` disables it); the tyre contact
  material per class was checked at Fells Point, Serengeti, Everest Base Camp and Monument Valley.
- Ground cover (`groundcover.lua`): stock GroundCover definitions read at runtime from official levels
  (Hirochi Raceway green grass, West Coast USA dry grass and scrub, sparse green grass between the scrub;
  their material files loaded first)
  and recreated with every plant type's `layer` set to ours (`bngEarthGroundGrass`,
  `bngEarthGroundDusty`) and `maxElements` scaled. Made once per level, on the first layered import.
  An empty `layer` places nothing (not "everywhere").
- No grass flicker at block moves (2026-10-11; in-game per-frame counts plus a static analysis of the
  0.39.4 binary). Every TerrainBlock import, setTransform / setPosition (even to the same place) and
  onAdd fires the engine's terrain-update signal over the whole block (min (0,0), max INT_MAX), and
  GroundCover's handler frees every cell in that box. It has no other condition: not the block, its
  render / collision / hidden state, the GroundCover's hidden or lockFrustum, any pref. Neither a
  second block nor a create-new / delete-old swap avoids it, and only patching the executable would.
  Freed cells come back in the same frame's prepRender under a global budget (~24 cells per frame
  for GPU placement, 3 for CPU placement), and `flushGroundCoverGrids()` lifts that budget for the
  current frame only. So `core.loadBlock` calls `groundcover.afterBlockChange()` (the flush) in the
  same tick as every import, and the GroundCovers use CPU placement (`useNewPlacementGpu 0`): those
  cells are drawn in the frame they are generated, while GPU-placed ones draw a frame later (one
  blank frame). GPU placement also reads a layer copy the hidden block never refreshes; CPU
  placement reads the layer map directly, so no refresh is needed. Price: one slower frame per block
  move (every ~128-192 m), growing with the element count. That is why the shares are low and the
  long grass reaches 70 m instead of 120. Two drives at 30 m/s over 3 km on Serengeti grassland
  (15 and 17 block moves) showed no blank grass frame at any move; the rebuild frame was ~43 ms with
  the first shares and ~25 ms with the tuned ones, next to the import's ~28 ms frame.
  The earlier cuts (GPU placement with a one-frame sunk-block refresh) blanked the grass for 2-4
  frames and regrew it over ~10 frames per move. CPU placement's normal-driving cost is tracked by
  the benchmark.
- Optional preload library `tools/gcpatch` (Linux, 0.39.4 only, README there): at game start it
  changes the handler's `and esi, 0x0E` to `and esi, 0` in memory (after checking its first 54
  bytes), so block imports and moves free no grass at all. BeamNG's Lua FFI refuses C function
  declarations, so the mod's Lua cannot do this itself. Teleports and rebases still need fresh
  grass, so core calls `groundcover.rebuild()` (recreate the objects, then the same-tick flush),
  which works with or without the library. Measured: a 3 km drive at 30 m/s, 10 block moves, zero
  blank-grass frames, no rebuild frame (each move only the import's ~25 ms frame), median 7.6 ms
  per frame versus 10.0 ms without it.
- A second TerrainBlock (even 5 km away with disableCollision) drops the player's vehicle through the
  ground at once (in-game, twice): never create one while a vehicle is in the world.

## Roads (`server/roads.py`, `roads.lua`; `earthd --roads on`, 2026-10-11)
- Tiles: 256 m squares of the anchor's ENU frame. The connection's `roads.Tracker` keeps the tiles
  within 640 m of the player sent and drops those beyond 960 m. A worker builds each tile from the
  "roads" layer of every map cell touching it (taken once per feature id), and an anchor change
  re-sends everything. The tile is written as `roads_<a>_<tx>_<ty>.json` in the session folder and
  announced as `roads_tile` {anchorId, key, path, n}, then `roads_drop` {anchorId, key}. The mod
  sends `roads_reset` after the Roads setting is switched back on.
- Highway signs (`server/signstream.py`, from cody196's PR #363): for each road tile the mod has, the same road
  processes build a sign tile (exit and overhead panels, merge warnings, Interstate mile markers; render-only
  shapes `exits` / `mileposts` / `merges`), announced as `roads_tile` with key `s<tx>_<ty>` (one in flight a
  connection; an empty one is not sent unless it replaces signs). The sources come from the weekly sign shards
  (`tools/build_sign_tags.py` -> BeamNG.Earth-data Pages `signs/`, read by `railtags.SignTags`; `BNE_SIGNS_DIR`: a
  local shard folder), placed on the roads' own pavement rows (`signroads.Context`, per map cell); only where a US
  state is (every face is MUTCD; `BNE_SIGNS_WORLD=1` everywhere). The mod's Highway signs switch sends
  `highway_signs` {on}: no sign tiles are built while it is off.
- Graphics settings (2026-10-28; settings.lua PRESETS low / medium / high / ultra / extreme): the mod sends
  `graphics` {lodScale, treeScale, buildingScale, photoPx} after every hello and on a change (core.sendGraphics):
  the far field's K times lodScale, the tree tiles' and the far buildings' / skyline's / far bridges' reach times
  their scales, the block photo's size (terrain.lua points the texture set and every layer's flat base maps at that
  size before a level's first block). Mod-only knobs: tree density (trees.lua QUALITY_LEVELS), grass distance
  (groundcover.lua DISTANCE), object detail ($pref::TS::detailAdjust in our level, enginepref.lua).
- Every reach that decides what is loaded or shown is a disc (2026-10-28): roads.tiles_near / tile_distance and the
  vegetation trackers' squares (tree tiles, far buildings' groups, skyline squares, far bridges) by the Euclidean
  distance to the square's nearest point, dropped beyond their drop distance by the same rule (the hysteresis), and
  the mod's ghost releases after a rebase likewise. Purely internal squares stay: the terrain block and its
  follow / prefetch rules (Chebyshev from the block centre), the map cells warmed around a place, the AI graph's
  graded square (blockworker.NAV_GRADED_M), the sea's physics water block, the far field's quadtree (already a disc).
- Speed-limit signs (`server/speedsigns.py`, kernels `speed_sign_place` / `speed_sign_mesh` in
  `native/earthfast/src/speedsigns.rs`): in the same sign tiles (shape `speeds`), everywhere. The value is
  `navgraph.road_mph`'s (the AI's: Overture's / OpenStreetMap's limit, else the class default) in the posted unit.
  Per direction on the road pieces between junctions (Overture connectors) round the tile (900 m): a sign where the
  limit changes (at a junction's straight-on continuation or inside a road), past major junctions (a secondary-or-better
  road meets a tertiary-or-better one), 300 m past motorway entrances, and reminders where a road crosses a fixed
  lat/lon grid (about 1 mile in town, 2 out of it); thinned per value and direction, placed beside the carriageway
  clear of every pavement, off bridges and tunnels. Faces: MUTCD R2-1 in the US, Rb-1 in Canada, Vienna C14
  elsewhere. (They replaced the furniture props of 2026-10-14, 30 m past both ends of every long segment.)
- Build: class and rules give the width (STYLE, or the width rule) and the surface kind (paved /
  gravel / dirt). Footways, paths and steps are left out, and so are segments flagged as bridge or
  tunnel or off ground level. Each road is clipped to the tile plus 2 m, simplified (RDP 0.25 m),
  given nodes at least every 12 m, and split into chunks of at most 100 nodes sharing their ends. The
  nodes are in the anchor frame, with z on the DEM (the DecalRoad snaps them to the physics ground
  anyway). Deterministic: sorted by feature id.
- Mod: each chunk is one DecalRoad (insertNodeNoRegen + regenerate, `overObjects` false,
  drivability -1, distance fade 450 / 150 m), made and deleted under the governor (kinds roadChunk
  0.5 ms, roadDelete 0.05 ms). The look uses stock materials loaded from the player's install:
  `road_asphalt_2lane` (West Coast USA) for paved, `m_dirt_road` (Johnson Valley) for gravel and dirt.
  A DecalRoad draws only where the physics block is (~500 m around the player) and re-clips by
  itself when the block moves.
- Under the tyres: `surface.paint_roads` draws the same roads into the material grid at their width
  (PIL lines through a least-squares lon/lat -> pixel affine of the block's samples). Dirt, then
  gravel, then paved on top, as DIRT / LOOSE / ASPHALT. The cells' roads are read from the cache
  without waiting (fetched in the background like the land cover). ~130 ms per 512² block in a city.
- Measured at Fells Point: 809 DecalRoads, +0.08 ms per frame (A/B, 5 rounds); every sampled road
  node within 250 m is asphalt under the tyres.

### Roads as meshes, the road lift, bridges (`server/roadmesh.py`; 2026-10-11)
- **Why not DecalRoads.** A DecalRoad re-clips itself to the terrain whenever the TerrainBlock
  changes, and ours moves every few seconds at speed. With 3,740 DecalRoads one block move cost a
  140-148 ms frame (9 ms without them; ~37 us per decal). That was the user's "massive lag spikes at
  speed". The official maps never move their terrain.
- **Road mesh.** Every chunk of a road tile (asphalt, cobble strips, lines, gutters, drains) becomes
  a ribbon in the DecalRoad's own UV layout (u across, v = distance / texture length). Ribbons are
  cut every 2 m and both edges sampled from the physics elevation in one call per tile. The result
  is one render-only TSStatic per tile: about 6k triangles / 360 KB, built in ~0.03 s. The same
  terrain-signal test now costs 8-12 ms, and roads show beyond the physics block too.
  - The stock decal materials render on a mesh (translucent with zwrite). The draw order inside one
    mesh is not guaranteed: with many translucent objects in view (2026-10-14, buildings and signals)
    the markings vanished under the asphalt. `roads.lua` now makes the asphalt and cobble materials
    opaque when it loads them (`M.OPAQUE`), so the translucent lines always draw over them.
  - On a MeshRoad, `sidewalk_concrete` drew nothing.
- **Road lift.** The physics ground (block + road heights) is raised `grading.ROAD_LIFT` (4 cm)
  under roads, tapering with the grading weight. The visible ground tiles are not raised. So the road
  mesh lies exactly on what the tyres touch (the user saw the tyres sink into a mesh lifted over the
  ground) and above what is drawn (no z-fighting). Markings sit 1 cm higher, gutters 0.5 cm.
- **Bridges.** Overture puts most bridges on a range of a longer segment (`between`).
  - Before: the rule covering the largest share decided the whole segment, so a street with a few
    metres of bridge was dropped whole (the user's "sections of road missing").
  - Now: `level_ranges` / `ground_parts` / `bridge_parts` split each segment. Ground parts are
    drawn, graded and painted; tunnel parts are left out.
  - Bridge parts are chained by shared end points. Deck heights are the abutments' ground heights
    weighted by 1 / distance along the chain (between two abutments, a straight line), never below
    the ground under the deck + 5 cm, and smoothed like a road profile.
  - `BridgeProfiles` (one per anchor) resolves each chain once: it loads the map cell at every
    loose end until all ends are abutments (at most 12 cells) and caches every part's heights, so
    tiles agree at seams. Reading 600 m of cells around every tile instead slowed tiles to 1-2 s and
    starved the block builds (edge warning).
  - Over a ground road crossing under it, a deck's top stays 6.5 m above the road, the requirement
    falling at 5 % along the chain. Measured on I-95: 2.2-4.5 m before, ~6 m after.
  - Decks crossing each other are solved together, the higher Overture level clearing the lower
    deck by 6.5 m (iterated). On the I-95 interchange, 40 of 41 crossings are 5 m or more apart.
  - Not real heights: the DEM is bare earth and Overture has only level order. Real deck heights
    need lidar (USGS 3DEP point clouds class 17 "bridge deck") - next.
  - `roadmesh.deck` builds one colliding TSStatic per tile: an opaque concrete slab (`sidewalk1`,
    1.2 m deep) whose top is the road surface. The asphalt and lines are the road mesh's, 1 cm above
    it. A deck topped with the translucent decal asphalt was see-through.
- **Grading fixes found driving at speed** (the car flipped):
  - roads close together (divided carriageways, junctions) jumped from one road's height to the
    other's at a core edge. They now blend by 1 / (1 + (d / 4 m)^2) from each centreline, and the
    minimum transition is 5 m.
  - Centreline second difference p99 per metre went 0.17 -> 0.027 m, worst 0.5 -> 0.087 m (the DEM
    itself: 0.13).
- **Build speed.** The tile's elevation calls, the grading plans' DEM samples and the land-cover
  urban check (`road_specs`) are each one batched call. Before, they were one call per road: tiles
  took 6-8 s and physics blocks ~6 s (15-35 s under load, so the car outran its ground). Now tiles
  take ~0.5 s and blocks 1.6-2.4 s.

### Road look updates (2026-10-14, from the street photo comparisons)
- Divided urban trunks (one-way carriageways) get a sidewalk on the curb side only (`walk_sides`: right of
  the travel direction, left where traffic keeps left) when they are town arterials: a limit of 45 mph or
  less, or with no limit a junction every 150 m or less (`Spec.kmh`, `Spec.gap`); expressways none.
- Two-way urban trunks (town main streets on state highways) get sidewalks, lamps; urban trunks not
  lined by buildings default to 4 lanes (13.2 m); white dashed lane lines where the driving width holds
  2+ lanes a direction; white edge lines on urban roads without curbs too.
- Curb ramps: the tactile pad is the first 0.6 m of the slope (tinted yellow); brick sidewalks on 55 % of
  the streets of dense brick-region cores; parking lots (Overture infrastructure `parking`) are the
  asphalt ground layer (no grass); built-up land is the dirt layer.

### Raised sidewalks, curbs and storm drains (`server/sidewalks.py`; 2026-10-11)
- Asked by the user ("will that include making the curbs and sidewalks 3d?"). A curb is a sharp
  15 cm step, which neither the height field (1 m+ samples) nor a decal can draw.
- First cut, replaced: a MeshRoad slab per sidewalk piece. The user found the curbs "there but
  invisible": `sidewalk_concrete` is a translucent decal material that draws nothing on a MeshRoad,
  so the car stood on an invisible top. Each slab was also three draw calls with no curb profile.
- How West Coast USA does it (its level files): no MeshRoads at all. Each district's sidewalks are
  one modelled mesh (64 `sidewalk_<district>.dae`, 231k triangles), with:
  - a curb profile textured from the `curbs` trim sheet (`/assets/materials/trim/concrete/curbs`:
    8 bands of plain / red / yellow / white / green curb, each a light top over a darker face);
  - slab tops from `sidewalk1` (a 4096 x 1024 sheet of 5 sidewalk strips with panel joints);
  - the gutter, tactile paving at ramps, a collision mesh, and LODs.
  That is one draw call per material per district. Storm drains are decals in the official maps
  (Driver Training's `m_metal_grid_drain` / `m_concrete_drain` decal-road materials).
- Ours, the same way per road tile:
  - `roads.build_tile(..., walks=[])` returns the sidewalk runs, cut at crossings, clipped exactly
    to the tile, with end caps only at real ends. `sidewalks.Builder` sweeps a fixed profile along
    each run: a 15 cm face with a 2 cm batter, a bevelled nose and a 15 cm curb top band (`curbs`,
    plain band), then the slab (`sidewalk1`, rising 1.5 % away from the road) and a back face.
  - Heights follow the road's graded centreline z, and grading keeps the sidewalk band flat.
  - It is written as one two-material `.cdae` (`cdae.encode` now takes several materials, one
    primitive each) and announced in the tile JSON as `"walk": {shape, pos}`.
  - The mod shows it as one TSStatic with `collisionType "Visible Mesh"` (governor kind `roadWalk`).
    Collision needs `be:reloadCollision()` (asynchronous, ~1.8 ms), which runs at most every 0.5 s
    while meshes come and go.
  - No sidewalk decals any more.
- Storm drain inlets: a metal grate DecalRoad (1 x 0.45 m) in the gutter, 2.5 m in from each real
  end of a sidewalk run (the corners) and every 60 m along it.
- **Intersections** (`server/junctions.py`). The user found square corners that did not join, and
  no crosswalks or ramps. Overture splits roads at junctions, so a junction is a shared node; its
  branches are sorted by angle.
  - **Curb returns.** Between neighbouring branches with sidewalks meeting at 35-150 degrees, a
    5 m radius arc tangent to both curb lines. Sidewalks and gutters are cut exactly at the tangent
    points (a cross-section square to the road, interpolated, uncapped). A corner piece
    (`Builder.add_corner`) has the curb along the arc, the slab out to the outer corner, and back
    faces. The asphalt between the arc and the old square corner is filled (a polygon in the road
    mesh).
  - **Crosswalks.** On a branch with corners on both sides, a zebra (`crossing_white`, 3 m) crosses
    the road 0.5 m beyond the corners. The branch's centre and edge lines stop at its outer edge.
  - **Curb ramps.** Where a crosswalk meets a sidewalk, the curb drops to 1 cm over the crosswalk
    (0.6 m flares). The slab climbs back over 1.8 m (1:12), with the `tactile_indicator` pad on the
    slope. Runs are refined to 0.5 m around ramps.
  - At other crossings (no shared node, or angles outside 35-150 degrees), the wider road's
    sidewalk runs on to the other's curb and the other's stops against its outer edge, as before.
  - A Fells Point tile: 20 corners, 23 crosswalks, 38 ramps; 8.8k sidewalk triangles.
  - Checked before building, otherwise the sidewalks end with a plain cut. A curb return needs
    branches longer than its tangent points, corner asphalt within 20 m of the node, and nothing of
    its slab or curb on any road. A crosswalk needs both ends clear of other roads, no other road
    crossed, and 2 m to the next one. A 36-tile survey of unusual junctions (skewed multi-way, short
    connectors, roundabouts, one-way pairs) went from 50 corners on carriageways to 4.
  - Tuned against Mapillary photos and NAIP orthophotos (`tools/ortho_overlay.py`):
    - corner radius by class (3 m residential, 4.5 m tertiary, 6 m secondary / primary);
    - stop lines 1 m before crosswalks on the approach half of two-way streets;
    - one-way streets (Overture `access_restrictions` by heading -> the `oneway` rule, roads cache
      layer version 2) at their own driving width with parking on one side and no centre line.
      Before, divided Broadway's one-way carriageways were drawn 14-15 m wide for 8-9 m.
    - Curb extensions (bulb-outs) where a two-way street has parking, as at most Fells Point
      corners. The corner's curb follows the driving lanes' edge for 6 m past the curb return, then
      tapers back to the parking curb over 3 m; the sidewalks resume beyond the tapers. Crosswalks
      span the driving lanes only, and their curb ramps are on the corner piece
      (`Builder.add_corner(ramps=...)`). The junction checks treat the corner's own roads at their
      driving width. **Off since GitHub #208** (2026-10-23): the rule put bulb-outs on every town
      street with parking, and most streets have none (Tustin's suburban corners stuck out into the
      road). `roads.build_tile` passes no `has_parking`; the code stays in `junctions.find` for a
      later opt-in from map data (OSM `traffic_calming=choker`, `kerb=extended`).
- Street widths from the buildings (`server/roadwidth.py`, `earthd --road-widths on`): Overture
  has a width on ~2 % of segments, so every street of a class used to get the same default (Thames
  St's 25-30 m square was drawn 10.6 m wide). In a built-up street the buildings stand at the back of
  the sidewalks, so the distance between the building faces across it is the right-of-way:
  - every 6 m along the road (10 m clear of each end), a ray each way, up to 25 m, to the first
    Overture building wall (the road's cell and its neighbours, bucket-indexed);
  - "walled" when both rays hit in >= 60 % of the cross-sections and the face-to-face distances
    agree (IQR <= 30 % of the median); the median is the right-of-way;
  - split into two sidewalks of 20 % of it (1.8-4.5 m) and the carriageway between (>= 5 m);
  - used on the built classes that get sidewalks (not service roads, not with a width rule) when
    the carriageway is 0.6-3x the class default; the sidewalk width follows. Elsewhere (front
    yards, a quay, open land) the defaults stay.
  - Memoised per road id for the server's life, so the physics grading, road meshes, sidewalks and
    surface masks agree. Fells Point: Aliceanna and Fleet ~13 m curb to curb, sidewalks to the
    building faces, matching the NAIP orthophoto (`tools/ortho_overlay.py`).
  - Tried first from NAIP colour: asphalt, concrete and many roofs are the same light grey there,
    so cross-sections of one street read 4-35 m.
- Grading keeps the sidewalk band flat: on the classes that get sidewalks in towns, the flat
  shoulder is the sidewalk + 0.5 m instead of 1 m (everywhere, so it never depends on the land
  cover).
- All road objects live in one SimGroup (`bngEarthRoads`), emptied when roads.lua loads. A Lua
  reload rebuilds the VM without unloading extensions, and after a few reloads 3,558 DecalRoads were
  in the scene for 1,011 tracked (the user saw fps drop).

## Ground graded under roads (`server/grading.py`, `earthd --grading on`, 2026-10-11)
- From the official-map study (docs/research/2026-10-11-official-road-study.md §3):
  - Along each road, the centreline height is the DEM every 2 m, Gaussian-smoothed (σ 12 m;
    5 m on dirt).
  - Across the road it is flat over the core: the carriageway taken as a city street would have
    it, parking included, plus a 1 m shoulder. Built roads always get this width, so it never
    depends on the land cover.
  - Then it blends back to the natural ground over 2x the height difference (a 1:2 slope),
    within 2-12 m.
  - Junctions blend the targets by weight. Bridges, tunnels and off-ground segments are not
    graded (roads.road_specs leaves them out).
- One `Grader` per anchor makes each map cell's road plans once and caches them. The physics
  block (make_heightmap's elevation function), the level -1 / 0 mesh tiles (`GradedDem`, a
  drop-in for dem.Dem) and the road decals' z all sample the same graded heights, so they agree
  exactly. Measured at Kern River: visible vs physics ground within 0.01 mm over 2,601 points.
  Coarser tiles stay ungraded (they sink under the block).
- Offline at Fells Point:
  - Cross-road |dz| at 2 m: median 0.026 -> 0.002 m, p90 0.073 -> 0.018 m (the official maps:
    0.00-0.08 m).
  - 25 % of a city block's samples move by more than 1 cm (median 6 cm, p95 21 cm).
  - Cost: 1.8 s for a 512² block with the plans cold (mostly the profiles' DEM samples), 0.16 s
    with them cached.

## Map data (`server/mapdata.py`; built offline 2026-10-11, branch feature/mapdata)
The shared input of every content feature (roads, buildings, water, land use, trees, ground
types), per docs/research/2026-10-10-world-data-sources.md. Not wired into earthd or the mod yet:
`mapdata.MapData` is the integration surface. Modules (all new): `mapcells.py` (cell keys),
`rangeread.py` (range reads + block cache + the fsspec filesystem DuckDB reads through), `cog.py`
(minimal COG reader), `maprasters.py` (WorldCover, CHM), `mapgeom.py` (columnar features, WKB,
clipping), `overture.py` (STAC index, fetch planning, DuckDB), `mapdata.py` (cache, provenance,
CLI). The only change to an existing module: host policies in `netfetch.POLICIES`.
- **Cells**: Web-Mercator z14 tiles `14/x/y` (2.4 km at the equator, 1.9 km at 39 N, 1.2 km at
  60 N), keyed only by position. Chosen over a geodetic grid or S2 because the sources are cut in
  it (a CHMv2 file is exactly 16 x 16 cells; the imagery and DEM tiles nest in it; vector tile
  services stop at z14), its area varies with cos(lat) like a geodetic grid's, and S2's evenness
  buys nothing for storage cells. v1.4 M6 tiles (S2) and the generators use
  `mapcells.cells_for_bbox()` to find the cells under any footprint and merge by feature id (only
  valid keys, each once; `MapData` refuses a key outside the grid). A whole feature spanning cells
  is listed in each and `own`ed by the one containing its bbox centre; a water / land-use polygon
  clipped to the cell is that cell's piece and owned there; so "build the owned features of every
  cell" builds each feature exactly once and every polygon whole. Edges are exact: a point on an
  edge belongs to the cell east / south of it, as `CellKey.bounds()` puts the edge (the tile
  formula alone misplaced ~1 in 7 points on a north edge); a centre at exactly 180 E is owned by the
  last column (the only one listing it). No cells beyond ±85.05° (as the imagery and the DEM).
- **Content = f(cell key, release ids, VERSION)**: features sorted by GERS id (deduplicated by
  highest version), rules in list order, polygons clipped in IEEE arithmetic with the "no area
  left" test summed exactly (`math.fsum`: np.dot's rounded sum kept or dropped slivers depending on
  the first vertex and on the CPU's BLAS kernel), rasters on the
  sources' own pixel grids (WorldCover: the global 1/12000° grid window over the cell; CHM: the
  cell's 2048² block of its quadkey-10 file), local metres from the cell's ENU tangent frame.
  `CellData.digest()` hashes it; tests show the same digest cold and warm, layer by layer, after
  the blocks were fetched in reverse order, with the rows shuffled inside the files, and from
  concurrent calls.
- **Overture fetch**: the release's STAC collection lists every file's bbox, so only the 1-4
  intersecting files are opened (the whole-release glob cost 35 s per query in the research). Then
  `overture.Reader._plan` fetches in parallel (4 at once): the footers; the bbox columns of the row
  groups whose statistics touch the cell; the used columns of the row groups with a matching row.
  DuckDB (no extensions installed or loaded; GEOMETRY and `ST_AsWKB` are core in 1.5; its own
  sequential prefetching off) then reads from the block cache through `rangeread.NetfetchFS`, the
  request kind being the protocol (`nfdemand://`, `nfprefetch://`, `nfbackground://`,
  `nfpredownload://`). Measured on
  the Monument Valley cell: DuckDB on its own 21 s / 49 MB; with prefetching off 13 s / 16 MB;
  planned and parallel 2.9 s / 34 MB (the 8 layers of a cell run 4 at a time). Found on the way:
  DuckDB 1.5 stopped skipping row groups by statistics when the scan and a `QUALIFY` window were
  in one statement with many columns (read every row group between the matches: +12 MB, +9 s for a
  city cell), so the scan and the dedup are two statements. The Azure mirror is asked when AWS
  cannot deliver (same bytes, same block cache).
- **Rasters**: COG range reads with `cog.py` (classic/BigTIFF, Deflate/PackBits/none, predictor
  2, sparse tiles; no GDAL, no tifffile). A missing file (open ocean for WorldCover, no CHM
  coverage) is a 404 remembered by a marker; the cell gets class 0 / no canopy layer. (Checked
  2026-10-11: the WorldCover, CHM, Overture S3 and Azure hosts answer 404, not 403, for a missing
  object.) No index gives a COG's size, so the size learned from the first Content-Range is kept
  in `<block dir>/size` and every cached block's length is checked.
- **Cache**: `<cache>/mapdata/v<VERSION>/<source>/<release>/<layer>/14/<x>/<y>.npz` per layer (no
  pickles; written atomically; an empty layer is cached too), `<cache>/mapdata/raw/<source>/<release>/`
  for 256 KiB range blocks of the source files and the STAC JSON (immutable, shared across
  VERSIONs). The cache cap's LRU can evict any file: it is fetched again. A damaged file is a miss,
  not an error: a layer file that does not load (truncated, a bad deflate stream, no provenance)
  is rebuilt from the raw blocks, a block of the wrong length (a crash after `os.replace` without
  fsync can leave it short or empty) is fetched again. Hot blocks are also kept in memory, in one
  LRU for the process (`rangeread.MEM_BYTES`, 64 MiB; it was 32 MiB per COG and 8 MiB per parquet
  file, up to ~0.75 GiB after a long drive). One dense city cell is
  3.3 MB of layer files and ~66 MB of raw blocks (they serve the neighbours too: the next cells
  cost 0-9 MB); a desert cell 60-90 KB and 35 MB.
- **Release pinning**: `overture.RELEASE = "2026-09-23.1"`; `mapdata.py --latest` prints the newest;
  bump the constant (cells are cached per release; a data update may change a place, a restart
  never does). A pinned release Overture has deleted raises `ReleaseGone` for uncached cells (also
  when its STAC index is cached and the files answer 404).
- **Provenance**: per cell a list of records (Overture per theme with the source datasets and
  licences of its features and the release, WorldCover with its files, CHM with its file), each
  with the exact attribution and licence (CREDITS.md; Overture's per-theme texts whole, read
  2026-10-11); `CellData.credits()` / `credit_line()` for the overlay (`mapdata.CREDIT_LINES`).
  The overlay line is Overture's own; where a cell's features come from a CC BY dataset (Esri
  Community Maps, Google Open Buildings, ...) its provenance names it, and the overlay wiring must
  credit it too.
- **What the data holds** (2026-10-11): Fells Point: 3,013 road segments (footway 1,609, service
  523, residential 351, primary 147, secondary 133...; surface on 56 %, width on 2 %, no lane
  counts), 12,458 buildings but heights on only 1.9 % (Microsoft ML 221, OSM 21) and floors on
  3.3 %, so building height needs a model; WorldCover 84 % built-up; canopy up to 25 m. Monument
  Valley: 10 roads (tertiary / track / unclassified), 1 building, 92.5 % bare. Kayenta: tracks,
  11 buildings, the solar plant's polygon, substations, towers and lines in `infrastructure`; the
  CHM shows the panel rows as 1-8 m "canopy", so tree placement must mask power plants and
  built-up land.
- **A dense city cell** (review, 2026-10-11, Midtown Manhattan 40.7549, -73.9840 = 14/4824/6157):
  4.8 s cold (90 MB, 64 requests from S3; 2.5 MB of layer files), 2,975 road segments (incl. rail
  and subway), 2,707 buildings with heights on 96.7 %, 4,420 infrastructure features; peak RSS
  381 MB (DuckDB takes ~150-200 MB per layer query, 4 run at once). In memory one city cell is
  ~37-40 MB, 16 MB of it the infrastructure `tags` column (fixed-width UTF-32 sized by the longest
  JSON, 895 characters): a follow-up should store long text columns ragged (UTF-8 + offsets).
- **Not yet** (ROADMAP "Shared map data"): wiring into earthd (one MapData on the server's Fetcher,
  prefetch of the cells ahead along the velocity as kind `prefetch`), the credits overlay ids
  (with the CC BY datasets of the provenance), building parts, places, OSM tags (lanes, width)
  joined by `src_record`, NLCD, recording MapData's disk hits / writes in `cache.Cache` (`mapdata/`
  is under the cap since the merge, LRU by mtime; `.tmp.npz` files being written are not tracked). Offline, a cell with one layer never fetched raises
  `Offline` as a whole; the earthd wiring may want the cached layers without it.
- Tests: `server/tests/test_mapdata.py` with `tests/mapfixtures.py` (synthetic GeoParquet written by
  DuckDB, synthetic COGs, a Range-serving fake transport; no network).

## Visual benchmark (docs/DEVTOOLS.md "Visual benchmark"; built offline 2026-10-11, in-game run pending)
- `tools/benchmark.py` + `tools/benchmark.lua` (a GE chunk sent through MCP `run_lua`, global
  `BNGE_BENCH`; no mod file, no mod change) and the set `docs/benchmarks.json` (docs/PLACES.md's
  places, its photo benchmark, 22 global places of which 5 are held out). Per place: teleport, wait
  until the core status says the far field is complete and the near ground exact, a fixed sun
  (TimeOfDay at local mean solar time, clock stopped) and clear weather, free-camera views, perfmon
  frame times, MCP screenshots; numbers and images in `docs/research/benchmarks/<date>-<label>/`
  (ignored by git). `compare` writes an HTML side by side with per-shot MAD / SSIM / changed-pixel
  share and number deltas against tolerances.
- Integration surface (read only, besides the documented console API): `beamngEarth_core.teleport`,
  `beamngEarth_core._state()` (anchor, pending, player, serverBacklog), `farfield.stats()` (done,
  queued, groups, transitions, live, underLevel / exactLevel / underOld, the import and exactMiss
  counters), `beamngEarth_sky` / `beamngEarth_weather` `setMode` / `getMode`, `weather.fogSigma` /
  `fogDensityFor`, `beamngEarth_perfmon.snapshot()`; stock: `core_camera.getGlobalCameras().free.manualzoom`
  (the free camera's zoom, saved and put back). Renaming any of these breaks the tool;
  `test_benchmark_tool.py` and `mod/tests/test_benchmark.lua` run against the real sky, weather and
  geodesy modules and a fake core / far field with these names.
- Safety: refuses to start above 1 m/s and stops (leaving the car to the user) when it moves
  between places; zero inputs, parking brake (again after each teleport); state.json before any
  change; weather, sky, perfmon, the player's position and the camera (and the free camera's zoom)
  restored in a finally (each step independent, also of a second Ctrl-C, the camera last); a failed
  or uncommitted teleport takes no shots; a timed-out MCP call is retried once; `restore <run>`
  after a hard kill.

## Airports and runways (`server/airports.py`, `runwaygeom.py`, `runwaymarks.py`; built offline 2026-10-11, branch feature/airports)
Groundwork for docs/ROADMAP.md quick win 4; design, data quality and the test places in
docs/research/2026-10-11-airports.md. Not wired into earthd or the mod yet. New modules only, plus the
host policy `netfetch.POLICIES["davidmegginson.github.io"]` and `airports` in `cache.KEPT`.
- **Data**: OurAirports' nightly CSV dump (public domain), `airports.csv` + `runways.csv` from GitHub Pages
  through netfetch (0.2 req/s, one at a time, hard cap 40 a day; gzip: 5.2 MB on the wire for 16.7 MB).
  `<cache>/airports/ourairports/<gen>/` + `current.json`: a new generation is written to a temp dir,
  checked (columns, a full parse, >= half the previous rows), renamed, then the pointer replaced; refreshed
  weekly (kind `background` when a copy exists), retried at most every 6 h after a failure, the last good
  copy used whatever fails (offline too). `cache.py` counts it and never evicts or clears it.
- **Model** (`AirportDB`, a pure function of the two files, sorted by OurAirports' persistent ids,
  `Runway.key = "oa:<id>"`, `snapshot` = their content hash): both ends (lat/lon, elevation m, true
  heading, displaced threshold), length, width (data or the ICAO minimum per code), surface (668 free-text
  values classified; BeamNG ground models: ASPHALT, ASPHALT_OLD, COBBLESTONE, GRASS, GRAVEL, DIRT, SAND,
  SNOW, ICE, METAL, WOOD; water none), lighted, closed, flags and a quality: `surveyed` (both ends),
  `derived` (one end + heading + length), `arp` (the only runway, centred on the airport point: excluded
  by default). 2026-10-11: 16,563 runways placed of 48,336 rows (7,584 helipads, kept apart; 24,064 rows
  have no end position); large airports 94 % placed, small 29 %. Load 1.6-1.9 s.
- **Queries**: `near(lat, lon, r)` (distance to the centreline, ~1 ms), `in_cell(key)` (mapcells z14:
  the influence footprint, exact rectangle test), `group(rw)` / `groups_in_cell(key)` (runways graded
  together: overlapping footprints, transitively, built from the whole file), `helipads_near`.
- **Grading** (`runwaygeom`): per runway an ENU frame at its midpoint, s/t along/across; zones runway
  (crown 1.25 % paved / 2 % unpaved), shoulders (paved, 2.5 %), graded strip (ICAO half-width and end
  extension by code; the terrain clamped to 2.5-3 % from the pavement, the end grade continued), blend
  (20-250 m, smoothstep, at most 1:4 added). Longitudinal profile: the DEM along the centreline (crown-aware
  median of 5 across), the ends at the DEM's level with the data's end-to-end difference when they agree,
  a Whittaker smoother at the smallest lambda meeting the code's vertical-curve radius and grade; steep
  runways (altiports) and relaxed unpaved strips keep the terrain's shape (radius >= 400 m, >= 50 x the
  pixel on coarse DEMs). Several runways: longest first, each fitted on the field graded before it, a
  crossing keeps the main runway's surface. `apply(plans, lat, lon, elev)` returns the graded elevation,
  weight and zone; outside every footprint the DEM bit for bit. `graded_elev_fn(elev_fn, plans)` is the
  drop-in sampler for `heightmap.surface` and the tiles (no change to heightmap.py; the physics block and
  the exact-level mesh tiles then sample the same function on the same lattice).
- **Markings** (`runwaymarks`): strokes (segment + width in s/t, each a 2-node DecalRoad, probes P1-P3)
  for threshold stripes, designations (seven-segment glyphs, the data's ident or the heading), centreline,
  aiming point, touchdown zone, side stripes, displaced-threshold bar and arrows, closed-runway crosses;
  FAA style in the US, ICAO elsewhere; class (precision / non-precision / visual / unpaved / closed) from
  length, lighting, surface and airport type.
- **CLI**: `python3 server/airports.py --near lat,lon --radius km`, `--airport IDENT`, `--preview IDENT out.png`
  (`airportpreview.py`: hillshade + zones + markings, the height change, the profiles; JSON report),
  `--stats`, `--refresh`, `--offline`, `--cache-dir`.
- **Test places** (Mapterhorn / AWS DEM): KBWI, KLAX, LXGB, EGLM grade within ICAO limits (max 0.2-1.4 %,
  0.02-0.22 m RMSE from the DEM, data end elevations within 1.2 m of lidar DEMs); Courchevel keeps its
  19 % knee, Lukla 14.7 % on an SRTM-class DEM, Lele (PNG) 9.9 %, Johnson Creek relaxed to 4.8 %. Plan
  13-56 ms per runway, `apply` up to 0.6 s for 2.2 M points (a whole airport preview).
- **Integration plan** (docs/research/2026-10-11-airports.md §6): one Store per server; plans per group
  cached by (ids, snapshot, versions, DEM sampler at a fixed zoom per place); the earthd hook wraps the
  block's and the tiles' `elev_fn` (after v1.4 M6, which rewrites that code); runway ground models in the
  v1.5 layer masks (+4-5 ms per 512² commit for 8 layers); groups per map-data cell; M6 tile content keys
  gain the OurAirports snapshot and the runwaygeom version; DecalRoad markings within ~2 km; runway ends
  as spawn places; taxiways / aprons / buildings from Overture / OSM later.
- **Wired (2026-10-13, `--airports on`, default off):** `airportgrade.AirportGrader` (one per process: the main
  server loads / refreshes the OurAirports copy before the worker processes start, they read it offline) grades the
  groups touching a box's map-data cells, planned once per process on a fixed z15 DEM sampler; every elevation
  function takes it before the road grading: the block (`blockworker.build`, the thread path), the far tiles
  (`GradedRunwayDem`), the road tiles. KBWI 10/28 in-game: centreline 2nd-difference RMS 0.40 cm (DEM 1.28 cm),
  within 16 cm of the DEM. Not yet: markings (DecalRoads), runway ground models in the layers, spawn places.
- Tests: `server/tests/test_airports.py`, `test_runwaygeom.py`, `test_runwaymarks.py`, `test_airportgrade.py` (synthetic files from
  `airportfixtures.py`, synthetic DEMs, fake netfetch transport and wall clock; no network).

## Sea water (`server/seacarve.py`, `seatiles.py`, mod `sea.lua`; 2026-10-13, `--sea on`, default off)

User's choices (2026-10-13): sea and bays first, roughly real depths and sinking cars, glossy far tiles
later. Engine facts: docs/research/2026-10-13-water-probe.md. A WaterBlock's surface is at its
position; vehicles see water they drive into, but not water that appears around them while parked.
- **Where:** Overture `base/water` polygons with subtype `ocean` (OSM's coastline: the open sea, bays,
  tidal rivers such as Baltimore's harbour). Per map-data cell they are rasterised on the z16 Web-Mercator
  grid (~1.9 m) with a 32 px margin from the neighbouring cells, then the floor depth grows from the
  shoreline at 0.15 m/m to 6 m (an octagonal erosion distance; no bathymetry yet). Cached in
  `<cache>/seacarve/v1/<overture release>/<x>/<y>.npz` (depth in 5 cm steps; a cell without sea is a
  small marker file). A point's depth is the bilinear sample, so the floor is continuous across the shore
  and the cells and the same in every process.
- **Ground:** `blockworker.ground` / `Server.ground` shape the DEM before the road grading: runways
  (`--airports on`), then `SeaCarver.carve` (z = min(z, -depth) where the depth is > 0). The block, the
  road tiles and the far tiles up to L1 are carved. Coarser far tiles have no map data: their DEM sea
  (-0.5..0.3 m) is sunk 1.5 m (`sink`), so it never shows through the water tiles.
- **Water tiles (`seatiles.plan`):** a quadtree on an 8192 m lattice in the anchor frame, planned from the
  centre of the player's 2048 m square: a square closer than 0.75 x its size is split, down to 2048 m,
  out to 8192 m. That gives <= 34 tiles over open sea (it was <= 76 with 512 m tiles; a 2048 m tile is
  0.17 m off the curve at its corners). A tile is kept when one of its 64 x 64 samples is sea: the
  polygon depth for tiles up to 2048 m, the DEM band for larger ones. Its surface is the sea level at its
  centre in the anchor frame and its normal the local vertical there (tilt cx / R, cy / R), so it
  follows the Earth's curve. No land is moved for it. The server plans with the cells at hand
  (`wait=False`: a cell not built yet uses the DEM band and is built by a background thread), so water
  appears within ~2 s at a new place (Annapolis) and the polygons take over at the next plan.
- **Message:** `sea` {anchorId, tiles: [[key, x, y, z, size, nx, ny], ...]}: planned in the ground pool
  when the player enters another 512 m square or every 15 s, sent when the set changed.
- **Mod (`sea.lua`):** one WaterBlock per tile (`bngEarthSea_<key>`): position = the surface, scale
  (size, size, 40) (water to 20 m down), `rotation` the tilt, East Coast USA's ocean look (murkier),
  `gridElementSize` size / 64. Four made per frame; changed keys only. A car slower than 1 m/s inside a
  new tile is re-placed where it is (`setPositionRotation`), since a parked car does not notice water
  that appears around it. Cleared on disconnect, a new anchor and unload.
- **Physics block:** a vehicle takes one water object at a time, the newest one it is in (2026-10-13:
  on the edge between two tiles only the nodes over one were in water, 495 / 950; re-creating either
  block flipped which). So `sea.lua` keeps one more block, `bngEarthSeaPhys`, not drawn
  (`setRenderEnabled(false)` keeps its physics; `setHidden` would not), 256 m square around the player
  at the level of the tile under it, and makes it again (so it is the newest) after a batch of new
  tiles, when the player is within 64 m of its edge, or when the level under it changes by > 5 cm.
  A car in the water (below the level + 1 m) and slower than 1 m/s is then re-placed where it is.
  Tile edges at Fells Point: 950 / 950 nodes in water.
- **Cost (Sandy Point, 2026-10-13):** 83 tiles (512 m minimum): 99 vs 102 fps at eye level, 108 vs 119
  from 200 m. 39 tiles (2048 m minimum, with lakes): 108 vs 109 at eye level, 110 vs 119 from 200 m
  (water over most of the screen).
- **Lakes (2026-10-13):** Overture `lake`, `reservoir`, `pond` and `water` polygons (not fountains,
  pools, basins, drains or intermittent water) go into the same per-cell raster as the sea, which now
  holds a depth and a level per pixel (cache `seacarve/v2/<release>/lakes/`). A lake's level is the
  median DEM (z15) inside it, away from the banks, quantised to 0.1 m (US DEMs flatten lakes, so every
  cell gets the same value). A lake smaller than ~350 m^2 in the 3 x 3 cells, one whose inside spans
  more than 1.5 m (p10..p90: a river, bad data) and one at sea level are left out. Floors are carved
  from the shore at 0.15 m/m to 4 m. The far tiles up to L2 are carved (L2 reaches ~3 km).
  `seatiles.plan_lakes` gives the water tiles within 2.5 km: 512 m squares holding lake water, split down
  to 16 m while one holds land more than 1 m lower than the lake (a valley below a dam) or two levels;
  at most 60, nearest first. Loch Raven: 61 tiles (before the cap), Patterson Park's boat lake: 21.
- **Rivers (2026-10-13):** Overture `river` / `stream` polygons (not drains or ditches; canals with
  locks broke into dozens of 16 m tiles, so they wait for River objects). Their level follows the DEM
  pixel by pixel: the DEM every 4th pixel, the lowest of the 3 x 3 samples around (the flattened
  surface, not a bank), quantised to 0.1 m. Floors carved to 3 m. Lake and river pixels at sea level
  (<= 0.3 m: lagoons, tidal rivers such as the Potomac at Georgetown) join the sea. Their tiles come
  from `plan_lakes`, split until one spans at most 0.3 m of the river's fall. Susquehanna at
  Harrisburg: 89.8 m down to 89.2 m across the area. Plans: 0.4-1 s warm, so the server plans again
  only when the player enters another 512 m square, or every 30 s (late map data).
- **Seams fixed (2026-10-13, the user saw lines on a creek near Harrisburg):** tiles 1.6 m apart within
  300 m made steps at their edges. River levels are now the lowest of the river's own DEM samples within
  ~70 m (the channel's surface), and any lake / river pixel whose DEM stands > 1 m above its level is dry
  (outlines that include banks, islands or a floodplain: no water, no trench). Inland tiles are planes
  fitted to their water (least squares; split while a sample is > 0.15 m off), tilted to match, and the
  physics block takes the tilted surface's height at the player. Ground carved > 5 cm under water gets
  the MUD layer (no grass through the surface).
- **Every river is River objects, beds carved (2026-10-13, the user: "if the tiles can only be flat we
  shouldn't use them, even the large river has holes"; "you can carve the streams"):** `lake_at` leaves out
  river outlines, so tiles are only the sea and lakes. `SeaCarver._carve_lines` draws the stream / river
  lines of the 5 x 5 cells around into the water raster: a level every 4 px (the lowest DEM within half
  the width, <= 15 m), the width from the outline (twice the distance to its edge) or by class, the bed
  cut to level - D (1 - (d / hw)^2) (D = width / 6 in 0.6-3 m) with a 1:1 bank for 4 m beyond (a negative
  depth: the ground kept below level + distance), the deeper cut winning at confluences; flattened river
  outlines keep their own carving. Those pixels join the outline mask. The Rivers' nodes take that level
  + 0.05 m (`level_at`), each node moved midway between the outline's banks with the outline's width
  (up to 2 km). The Harrisburg creek: one 28-48 m River dropping 91.7 -> 88.5 m in its cut channel.
- **Sections by shape (2026-10-13, the user: "treat each river branch / lake as an isolated section"):**
  a River ribbon 1 km wide folded over itself at bends and islands (backfacing triangles: holes in the
  Susquehanna). So a flattened river with a half width >= 60 m somewhere (`WIDE_RIVER_M`) is area water
  like a lake: its pixels leave the outline mask and `plan_lakes` tiles it, each tile a least-squares
  plane (the river's fall; tolerance 0.4 m; the levels smoothed over ~22 m first, a flattened DEM still
  stepped 0.6 m within 128 m below the Dock Street Dam), 64-512 m, <= 120 within 2 km. Narrow rivers and
  streams stay Rivers; a narrower one entering a wider one or tiled water ends one node inside it. The
  water engine can't cross-fade objects, so sections meet by a small overlap: tiles are drawn 1 m larger
  than their squares (no hairline gaps between tilted neighbours).
- **River objects along centrelines (`server/riverlines.py`, 2026-10-13):** after the user's look at the
  "Downhill Mayhem" map (each of its two rivers is one River with 24-130 nodes, 5 m deep, the surface
  following the valley, `flowMagnitudePhysics` 35 / 100 m/s pushing cars). Overture water lines (river /
  stream, canal of class canal; no drains, ditches, intermittent) are chained where one way continues
  another (OSM draws them in flow order), resampled every 10-20 m and cut into pieces on a 1024 m lattice
  of the anchor frame (stable keys). Surface: the lowest DEM of the node and four points 3 m around, a
  5-node running median, never rising downstream (running minimum), + 0.25 m; not carved yet (shallow
  water in the channel). Width across the river's outline (seacarve's outline mask) or 12 / 4 / 8 m by
  class; depth width / 8 (1-4 m); flow 1.0 / 0.6 / 0.3 m/s downstream. Left out where tiles are water
  (the sea, lakes, rivers the DEM flattens: those keep their carved tiles). Only rivers the DEM flattens
  (p90 of their inner samples <= 0.25 m above the surface) are tiles now; rough ones are left to these.
  Sent as `sea.rivers` [key, kind, flow, [[x, y, z, width, depth], ...]] with the tiles; the mod makes
  them from a definition (`Sim.deserializeObjectFromText`, which leaves them out of every group: added to
  MissionGroup) with little foam (shallow water drew as white ribbons). Creek near Harrisburg: 12 pieces,
  0.7 s to plan warm.
- **Bathymetry (2026-10-13):** per cell, one export of NOAA NCEI's DEM global mosaic (CUDEM ~3 m along
  the US coasts; host policy `gis.ngdc.noaa.gov`), 4x coarser than the raster and aligned with it. The
  sea floor reaches NOAA's depth (1-80 m) from the shore at 0.3 m/m; without it (offline, refused) the
  generic 0.15 m/m to 6 m floor is used, not written to disk and built again after 2 min. Depths are
  stored in cm (uint16; cache `seacarve/v4`). Baltimore harbour off Fells Point: 12.9 m.
- **Drive cost (Fells Point east along the waterfront, 30 m/s, 6 km, 2026-10-13):** 75.7 fps with water,
  79.3 without; p99 22.4 vs 21.9 ms, worst frame 55 vs 53 ms.
- Tests: `server/tests/test_seacarve.py`, `mod/tests/test_sea.lua`.

## Trees, bushes and rocks (`server/vegetation.py`, mod `trees.lua`; 2026-10-13, `--trees on`, default off)

The user asked for "trees, rocks and other decoration" (ROADMAP v1.9 "Cities & nature").
- **Per map-data cell (deterministic, cached in `<cache>/vegetation/v1/<release>/<x>/<y>.npz`):**
  - Trees are the tops of Meta/WRI's CHMv2 (1.2 m canopy heights, z17 grid): local maxima of the 3 x 3
    smoothed heights in a disc that grows with the height (r = 1-4 px for bushes to > 15 m trees), then
    one per crown (the tallest first, removing tops within 0.5 (0.12 h + 1) m; 0.8 looked sparse).
  - None on buildings (the CHM counts roofs and solar panels), road corridors (half width by class +
    1.5 m: overhanging canopy would put a trunk in the lane) or water; the neighbours' features too.
  - Bushes: tops of 1-3 m, and a hashed scatter on a 6 m grid over shrubland (WorldCover 20).
  - Rocks: the same scatter over bare land (60), thinly over shrubs and grass.
  - Every choice hashes the item's global pixel: species group (deciduous / conifer by a regional share
    and height, shrub, rock), yaw, size variation, and a fixed rank for thinning.
  - Cost: ~3.5 s per cell the first time (Patterson Park 5,074 trees; Loch Raven woods 10,387; an
    Arizona desert cell 13,000 rocks).
- **Tiles:** 256 m tiles like the road tiles, sent within 1280 m and dropped beyond 1536 m (`veg_tile` /
  `veg_drop`), each a JSON of [group, x, y, z, yaw, scale, height, rank] in the anchor frame. z is the
  block's ground (`Server.ground`: runways, carved water); items in water are left out.
- **Mod (`trees.lua`):**
  - One `Forest` (`bngEarthForest`) of East Coast USA's stock shapes: oaks, beeches, aspens, Douglas firs,
    bushes and rocks, with their measured heights (they have LODs, billboards and collision meshes).
  - Each item takes the variant nearest its height (within 0.8-1.25, else 0.6-1.6, picked by its rank),
    scaled to it.
  - Item types are TSForestItemData made with `worldEditorCppApi.createObject` (plain `createObject`
    returns nil for datablocks), after loading the shapes' `main.materials.json`.
  - A tile shows the items whose rank is under its distance's threshold: all within 300 m, half to
    550 m, a quarter to 900 m, an eighth beyond (2026-10-14: out to 1280 m like the far buildings).
  - Items are made and removed every third frame, at most 750 and `FRAME_MS` (4 ms) of Lua per such frame
    (`LOADING_FRAME_MS`, 12 ms, while the world loads; none in the governor's heavy frames); a pass stopped by
    either goes on from its row in the next one. A tile's Forest comes from a pool: a new one costs ~9 ms to
    register, so one a frame at most, and a few spares are made in tree frames with nothing else to do.
    (2026-10-26, Fells Point autodrive, A/B twice: the slowest 1 % of tree frames 16.5-18.8 ->
    9.5-9.6 ms, the worst 53-87 -> 15-17 ms; the same 90,228 forest items in the same places.)
- **Regions (`vegetation.regional`, 2026-10-14):** a coarse climate from the position and the land cover
  turns the generic groups into the stock maps' species: West Coast pines / redwoods and oaks /
  eucalyptus (west of -114), junipers, desert bushes and rocks in the dry West's open land, palms on warm
  coasts and in the tropics (60 % of town trees, 20 % elsewhere), tropical trees. Shapes from West Coast
  USA, Utah, Johnson Valley, Jungle Rock Island and Italy; heights measured in game. A tile lists its
  groups; the mod loads every shape folder's `main.materials.json`.
- **Prefetch:** each pose of the active anchor queues the cells within 1200 m (`warm_around`, nearest
  first), so a teleport's trees are built before its tiles ask for them.
- Tests: `server/tests/test_vegetation.py`, `mod/tests/test_trees.lua`.

## Buildings (`server/buildings.py`; 2026-10-14, `--buildings on`, default off)

The user: "make the world look alive ... look like a professionally made map".
- **With the road tiles:** each 256 m road tile also gets `bld_<a>_<tx>_<ty>.cdae`, every building whose
  footprint centroid is in the tile, one mesh with one primitive per material, shown as a colliding
  TSStatic by `roads.lua` ("bld", held like the sidewalks while the car is inside a building's box).
- **Data and prediction:** Overture footprints (outer ring, simplified, counter-clockwise). Overture
  rarely has heights (Fells Point 2 %, towns 50-80 %) and almost never roofs or facades, so:
  - storeys from the class and the footprint: sheds 1, row houses (narrow, deep, sharing a wall) 2-4,
    houses 1-2, apartments 3-5, others 2-4; a given height or floor count wins;
  - roofs: gabled / hipped for detached houses and small sheds on near-rectangular footprints (the
    oriented box fills >= 80 %), flat otherwise and always when attached;
  - facades: the stock window sheets `m_highrise_plaster` / `m_highrise_metal` (asset survey
    2026-10-13): each 256 px band is one floor style; every floor of a wall is one band with a whole
    number of bays (12 m per 1024 px); the sash band's two black "open window" bays are skipped (one quad
    per bay); shops get the shopfront band on the ground floor; 7+ storeys the office curtain wall;
  - brick by region (`brick_share`: 75 % east of -95 and north of 36, 35 % in the South, 15 % in the
    West), tinted variants (`buildings.materials.json`, own names pointing at the shared textures);
  - floors stand on the footprint's highest ground; below it a plain plinth down to 0.6 m under the lowest.
  - every choice hashes the footprint's position: the same building from any anchor.
  - colours per building by vertex colour (`cdae.encode(colors=)`, materials with `vertColor`): one
    material per look (window sheet, shop, office, 3 roofs, siding, stucco), each building its tint and a
    +-6 % brightness; roofs in 4-5 colours per material.
  - detached houses (2026-10-14): siding (East / South) or stucco (West) walls, world-scaled, with sash
    windows cut from the sheet laid 3 cm over them, one per 3.4 m bay and floor; brick far less often
    than for row houses.
  - Overture `building_part` (new map layer `building_parts`): a building with parts is drawn as them,
    each from its bottom to its top (podiums, setbacks, towers), one look; a part without a height
    takes the building's; raised parts get an underside.
  - cornices on flat roofs up to 6 storeys, boxes on flat roofs over 250 m2, front doors (Building
    Architect kit, Forest items) on the wall facing the nearest road.
  - big boxes (low commercial, 1200 m2+) and warehouses: blank precast-panel / corrugated walls with one
    storefront band on the wall facing the road; barns gabled with metal roofs, barn red or white;
    shopfronts on the ground floors along urban main streets; towns (< 16 % footprint cover) mostly
    1-2 storeys; airport terminals / hangars / parking garages by name (Overture names them, no class).
  - small towns of the arid West (`district_town`: the footprint share of the 1 km disc around a tile,
    from per-cell centroid / area arrays, below 0.18; Bishop 0.14, Moab 0.11, Frederick 0.21, Fells Point
    0.28): 75 % one storey, attached buildings are shops (not row houses), a 1.2 m false front on one-storey
    shops. A tile's own share cannot tell (a main-street block is as dense as a city's). Near and far
    building tiles both ask, so they agree.
  - driveways (asphalt strips in the road mesh) from siding / stucco houses to the road's edge.
  - churches (class or name): a tall gabled nave and a square tower with a slate spire; brick row houses
    get white marble stoops (two steps, the door raised to them); in dense cores (> 35 % cover) small
    buildings are row houses even where Overture's footprints leave gaps.
- **Far tiles** (`bldfar_tile` / `bldfar_drop`, 256 m squares to 1600 m): the same buildings, lite mesh (no
  cornices, boxes, per-bay quads), render-only; `roads.lua` hides one while the road tile of its square
  shows its own colliding buildings, and makes them only once the near meshes are all made (they share
  the roadWalk budget: Frederick once had 50 near meshes waiting).
- **Cost:** ~0.5 s per tile to plan, 5-13k triangles (Fells Point).
- Tests: `server/tests/test_buildings.py`.

Front porches (2026-10-14): pitched-roof houses under 300 m² get one on the front door's wall (55 % in the East,
25 % west of 102° W, hashed): a white deck 0.35 m high, three posts and a 2.2 m deep roof in the roof's colour;
the door stands on the deck. Shop signs: a coloured band (9 tints, a few shops without) over each shop's street
wall, shop by shop (6-11 m); storefronts take the wall facing a street, not a service road.

## Street furniture (`server/furniture.py`; 2026-10-14, with `--buildings on`)

- Towns (road tiles under 30 % footprint cover): wired wooden utility poles every 40 m on one side with
  a cobra-head lamp on every other pole, instead of metal lamp posts; dense cores keep the posts.
- Hydrants every 120 m on town streets (alternating sides), benches and bins on major ones.
- Street trees: half the town streets (hashed per street) get a row of small broadleaves in pits every
  11 m, 0.9 m from the curb, both sides (the canopy model has few in dense streets).

- Street lights on the sidewalk runs, 0.5 m from the curb, arm over the road (East Coast USA's
  `eca_lamppost`): residential streets one side every 40 m, tertiary / secondary / primary both sides
  staggered every 30-36 m (West Coast USA's lamps stand ~17 m from their nearest neighbour, often across
  the street). Utility poles every 45 m along country roads without sidewalks, 2.4 m off the edge.
- Stations at fixed distances along each road from its first vertex, phase hashed from the road id:
  deterministic across tiles and anchors.
- Sent in the road tile (`"props": {groups, items}`, trees.lua rows) and drawn by `trees.lua` as Forest
  items (`onRows`, key `p<tx>_<ty>`, dropped with the road tile).
- Utility pole wires: three sagging wires per span (two crossed ribbons per 5 m segment), the spans that
  start in the tile, render-only `wire_<a>_<tx>_<ty>.cdae` ("wire" in the road tile).
- Junctions (road ends meeting, 3+ arms, all urban): boom signals on every arm where 3+ major roads (or
  2 at a four-way) meet; stop signs on minor arms meeting a major road, the stem of a minor T and half
  the minor four-ways (hashed); on each approach's right, facing its drivers.


## Parked cars and traffic (`server/parking.py`, `navgraph.py`; mod `parking.lua`, `navgraph.lua`; 2026-10-14)
The user's choice (2026-10-14): BeamNG's own traffic system, which spawns and recycles traffic and parked cars and
has the player's settings (Options > Gameplay > Traffic). We only feed it what a stock level's files would.

**Parked cars** (with `--buildings on`). `parking.curb_spots` puts parallel-parking spots along the sidewalk runs of
urban residential / tertiary / secondary / unclassified streets of a road tile: both sides of streets 9 m or wider,
one side (hashed) of narrower ones, none under 7 m; 6.5 m apart at stations of the road (furniture.py's, so every
tile and anchor agrees), 9 m clear of a run's ends, facing the traffic on their side (right-hand traffic; one-way
streets: their direction). `clear_of` drops spots in front of hydrants (4.6 m) and driveway mouths (4.2 m).
`lot_spots` fills the mapped lots (Overture infrastructure `parking` polygons) with 90-degree stalls (2.6 x 5.5 m,
7 m aisles) along each lot's long side, off building footprints (garages). The road tile carries them as `"park"`
rows `[x, y, z, heading(, 1 = lot)]`. The mod's `parking.lua` keeps one sites object (`gameplay/sites/sites.lua`,
as `city.sites.json` builds), its spot objects kept by position across rebuilds, rebuilt at most every 2 s from the
spots within 900 m of the player and 80 m inside the terrain block (the only collidable ground), and hands it to
`gameplay_parking.setSites`. Once 24 spots are near it starts `trafficParkedAmount` cars (`setupVehicles`, which
needs `core_multiSpawn`: our level loads no traffic extensions). A guard every 0.5 s moves a parked car that is
within 30 m of the block's edge or falling to a spot near the player. Teleports: the cars go inactive
(`setActiveAmount(0)`), and once the new place has spots the stock system is told to look again (its cached spot
list is in the old frame) and the cars are scattered onto them. Cost (Fells Point, 16 cars): ~0.7 ms render,
0.25 ms physics.

**Traffic** (`--traffic on`, default off). `navgraph.build` turns the drivable roads within 1100 m of the player into
the stock map's graph: Overture connectors are the nodes (`c<id>`, the same name in every window and anchor and
shared at junctions), plus simplified shape points and splits of edges over 80 m; edges carry half width, lanes
per direction (3.3 m lanes), one-way direction and speed limit (mapped, else US class defaults, town arterials
capped). Driveways, parking aisles, sidewalks and crossings are left out. A new graph every 400 m (`navgraph`
message, a JSON file). The mod's `navgraph.lua` clips it to the block (60 m in) and applies it with
`map.reset(customNodes)` (docs/research/2026-10-10-navgraph-traffic-signals.md), handling the custom path's
pitfalls: the node table object stays the same (gameplay_traffic caches it), the US road rules come from our own
file through `path.getPathLevelInfo`, every vehicle is told to fetch the graph (`mapmgr.requestMap`), a foreign
`map.reset()` is undone next frame. ~600 nodes per 512² block at Fells Point, applied in ~9 ms. The first graph
starts `trafficAmount` cars (`gameplay_traffic.setupTraffic`); a guard respawns cars near the block's edge;
teleports hold them inactive until the new graph is in, then `forceTeleportAll`. Cost (7 cars): ~1.6 ms a frame.
The level's `supportsTraffic` stays false, so freeroam's own start-up does not try traffic before our graph exists.
**Signals and stop signs.** The graph carries the junctions that `furniture.junction_controls` controls (the same
rule that stands the props, so the AI's controls are where the booms and stop signs are): per incoming approach the
stop point (2 m before the junction's widest road edge, on the right half of the lane, where `map.findClosestRoad`
finds that approach), the travel direction, the phase (by axis) and the head's prop transform. The frame after a
`map.reset`, `navgraph.lua` builds them with `core_trafficSignals`: a two-phase `lightsBasic` sequence per signalled
junction (green / yellow from the approach speed, `autoSetTimings`), one shared `signStop` controller, and a
switchable stock boom (`createSignalObject`, the instanced_trafficlight material) per approach in place of the road
tile's static one (trees.lua's signal group hides while they exist; heads are kept across applies by instance
name). Vehicles that hold signals read the state mailbox every frame and throw on one never written, so an empty
update is written first. Fells Point: 51 junctions, 132 approaches, cars stop 2-5 m before the line on red.
Cost per new graph: ~7 ms (map.reset; the UI's NavigationMapChanged dump is dropped) + ~5 ms (signals) in two
frames; the first graph of an anchor ~10 + ~22 ms (all heads made).
**Bridges, widths, countries.** Roads that are bridge all along (left to the bridge builder by `road_specs`) are
in the graph with their `bridge_specs`, and nodes on a bridge part take its deck height (the anchor's
`BridgeProfiles`). Edge widths are the road tiles' measured ones (`roadwidth.py`): with class defaults the AI's
lanes ran over London's kerbs. The road rules come from the nearest airport's country (OurAirports;
`navgraph.road_rules`): left-hand traffic (the game's `rightHandDrive = true`) in ~70 countries, turn on red in the
US and Canada, `lightsBasicEU` where red+yellow precedes green; the same per-anchor flag turns the parked cars,
signal booms, stop signs and stop points to the left side (road tiles in both the main process and the pool).

**Keeping it running** (found in game 2026-10-14):
- Each vehicle's new signals table keeps the old junctions it lacks (`navgraph.setSignalsLua`): ai.lua remembers the
  junction a car approaches across map changes and reads its signal unchecked; a graph without it disabled the car.
- No stop or signal on an approach from another connector under 15 m away (`SHORT_APPROACH_M`): offset streets split
  one intersection into connectors metres apart, and a car met the next stop line behind it and waited forever.
- A traffic car still for 45 s, 40 m or more from the player, is respawned (`STUCK_*`; paused time excluded): stock
  traffic respawns only far, unseen cars. A parked car off its spot (another frame's position after a teleport the
  scatter missed) is moved to a spot once the player is 60 m away (`parking.offSpot`).

**The player's choice** (the user, 2026-10-14: spawn them if the player has them on, not otherwise). The level
`supportsTraffic`, so the level-start configurator offers Disabled / Parked only / Normal / Police. The mod takes
that choice in `onClientPreStartMission` (freeroam's camera-ready spawn can come before our mission start) with
`navgraph.takeChoice`: freeroam's options are set to disabled (it would spawn before our first road graph is in the
map), our cars start once it is, and the player's options are given back then (and when the level ends). Level
start runs twice; a repeat keeps the first choice. The radial menu's traffic entries work at any time. The server
builds the graph and the parking spots by default (`--traffic on`).

## Seasons (`server/season.py`, mod `seasonlook.lua`; 2026-10-14, `--season auto|fall|off`, `--leaf-on`)
The user: colour-correct Maryland's leaf-off photos, and use it for seasons (it was fall).
- `leaf_on` (in `Imagery.render`, for sources with `leaf_off`: md6in): where the summer reference (the rest of
  the chain: USGS / NAIP, already rendered for `match_colours`) is vegetation (green chromaticity 0.345-0.37),
  the texture takes its chroma at the reference's ~8 m scale and 80 % of its brightness; roofs, roads, dirt and
  water keep theirs, the photo's detail stays.
- `grade` (every texture: the block's photo and every far tile, so they agree): fall turns the canopy (green,
  darker than value 0.44-0.52 and textured at 2-3 m; brightness only on texels over 2.5 m) yellow / orange / rust
  in world-anchored noise patches (18 m stands, 6 m trees, 25 m colour regions), the share growing with the
  amount, and every green moves 35 % of the amount toward yellow. Textures of 512 px or more are graded at half
  size and applied as a colour ratio (21 ms per 512 px tile). The amount: `season_amounts` (peak ~Oct 27 at 40 N,
  2.5 days earlier per degree north, +-28 days; 0 under 23 degrees; the southern hemisphere half a year off).
- The 3D trees (`seasonlook.lua`, hello.season): no stock autumn models, so the stock leaf materials
  (`baseColorFactor`; the merged billboards' `diffuseColor`) are tinted in our level: oak rust-orange (~12 days
  late), beech bronze, birch / aspen gold (~10 days early), conifers green; amount^1.6 so trees hold their green.
  The stock factors come back when the level ends. Engine-made far billboards keep their green (open).

## Post effects in our level (mod `postfx.lua`; 2026-10-14)
The stock depth of field blurs everything past a few hundred metres; stock maps hide it in haze (3-15 mile view
distances), ours (60+ miles) showed it as soft distance. In our level the far blur is 0 (re-applied when the
player's preset resets it); the near blur and the player's settings stay theirs; the stock parameters return when
the level ends. LOD research: docs/research/2026-10-14-lod-research.md (also: linear-light sharpened mip chains,
`imagery.mip_arrays`; gentler far-tree rings, the engine billboards far trees itself).
