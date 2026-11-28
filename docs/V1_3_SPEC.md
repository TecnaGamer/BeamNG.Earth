# BeamNG.Earth v1.3: satellite imagery, unified near/far visuals, no visible terrain updates

Builds on v1.2 (docs/V1_2_DESIGN.md + §12 probe results, server protocol v3). Everything not mentioned
here stays as in v1.2. Written 2026-10-10 by the lead from user feedback and in-game probes.

## User feedback on v1.2 (2026-10-10)
1. "Still seeing terrain get updated very close to me, it's disorienting."
2. "There's a hard border in the distant terrain colours" — confirmed in-game: brown terrain-block dirt
   next to near-white far tiles (the far palette whitens with elevation), see
   docs/research/v13_investigation/border_high.png.
3. "They pop in as terrain loads."
4. Free hand to improve visuals.

## In-game findings (lead, 2026-10-10) — binding
| Finding | Evidence |
|---|---|
| **Cause of #1 (main part): terrain material texture repeats not dividing the 256 m follow step.** Macro 80 m and base 512 m made 24% of the view change at every commit. **Fixed already** (macro 64 m, base 256 m, detail 2 m) in tools/make_level_assets.py + level JSON: the fixed-camera diff across a commit went from mean 20.8 to 2.9 (only the moving car and a distant far-tile swap changed). | docs/research/v13_investigation/A*/B*/diff_*.png |
| **Per-tile satellite textures on far tiles work.** A Lua-created Material with mapTo = the .cdae's material name and colorMap = a texture file renders the imagery on the TSStatic. Material creation 0.13–0.46 ms; tile spawn incl. texture 2.5–3.5 ms (jpg/png/dds all work; first-ever jpg 15 ms one-time). | docs/research/v13_investigation/sat_probe.png, probes/v12/p_sat.lua |
| **Swapping the TerrainBlock material's base colour texture at runtime triggers terrain texture packing: a 121 ms frame** ("Texture added for packing, because cache not found: /temp/art/terrainMaterialCache/…"). Doing that at every terrain-follow commit is unacceptable. → Do NOT put imagery on the TerrainBlock. | lead probe |
| Mesh Material supports detailMap / detailNormalMap (+ detailMapUseUV, detailScale-like fields) for close-up grain. | editor/materialEditor.lua:268,1295,1346; exe strings |
| A render-disabled TerrainBlock (setRenderEnabled(false)) stays fully collidable. | ARCHITECTURE.md TerrainBlock probes |
| Imagery sources reachable, no key: **USGS ImageryOnly** `https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` (public domain, US only, z≤16 ≈1.9 m/px at 36°N, 256 px JPEG, z17 = 404); **EOX s2cloudless-2023** `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2023_3857/default/g/{z}/{y}/{x}.jpg` (global, ~10 m, CC BY-NC-SA 4.0 — personal non-commercial use with attribution "Sentinel-2 cloudless 2023 by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023)"); EOX 2016 `s2cloudless_3857` (CC BY 4.0) as last fallback. All Web-Mercator XYZ, 256 px. | lead fetch test, scratchpad/imagery/montage.png |
| Pillow 12 can save DDS with pixel_format='DXT1'. Whether it writes mipmaps must be verified (missing mips ⇒ shimmering). | lead |

## Design

### A. Satellite imagery on every visual tile (server)
- New `server/imagery.py`: source chain USGS (only where it returns real data — detect 404 / blank /
  uniform tiles) → EOX 2023 → EOX 2016; disk cache `~/.cache/beamng-earth/imagery/<source>/<z>/<x>/<y>.jpg`
  with remembered 404s (like dem.py), polite User-Agent, a small fetch pool (≤6 concurrent), in-memory
  LRU of decoded tiles.
- Per far tile: render a texture of the tile's ENU footprint (same footprint as its geometry) by sampling
  imagery at each texel centre: ENU (current anchor frame) → lat/lon → Web-Mercator pixel at a zoom whose
  ground resolution ≈ the texel size (never finer than the source max). Bilinear. Texture size by level:
  finest near levels 512², others 256². Write **DDS DXT1 with a full mip chain** (verify Pillow output; if
  Pillow writes no mips, write them yourself: DXT1 blocks per mip with a numpy BC1 encoder, or fall back to
  PNG and let the engine build mips — measure load cost in tests and document the choice).
- Per-tile material name `bngEarthSat_<anchorId>_<key>` written into the .cdae (2026-10-16: a pooled name
  `bngEarthFar_<session>_<n>` instead, server/matslots.py: the engine's frame cost grows with every Material ever
  made, so the names and their Materials are re-used by later tiles); texture file next to the
  .cdae with a unique name. UV0 = 0..1 across the footprint (skirts/aprons reuse edge UVs). UV1 =
  world-locked detail UVs = (x, y)/DETAIL_M in the anchor frame (continuous across tiles) for the detail map.
- Brightness/colour: imagery has baked shadows; apply a mild global tone curve (configurable) so it sits well
  under BeamNG lighting; keep sources' relative colours (blend USGS/EOX at coverage edges is out of scope).

### B. Near ground drawn by mesh tiles; TerrainBlock physics-only (default `--near-visual mesh`)
- The terrain block keeps doing physics exactly as in v1.2 (lattice, quantum, follow, commits), but
  `setRenderEnabled(false)` (mod) when near-visual = mesh.
- Add finer quadtree levels below L0: L-1 = 128 m tiles (4 m spacing), L-2 = 64 m tiles (2 m spacing,
  32×32 cells). L-2 vertices must coincide with the physics lattice (world positions multiples of 2 m) and use
  the **identical quantised heights** the terrain PNG uses (same anchor quantum q / zBase rule) and the
  **same triangle diagonal pattern as BeamNG/Torque TerrainBlock** (determine it from Torque3D source —
  a copy is in scratchpad/t3d from earlier research, or WebFetch github.com/TorqueGameEngines/Torque3D
  terrCollision/terrCell — and verify with a unit test against our own collision assumptions), so the visible
  ground equals the collision surface within float precision at L-2.
- Selection: refine with K per level as v1.2, plus min-level rule near the player: L-2 within ~150 m,
  L-1 within ~400 m (configurable). Because the terrain is invisible there is **no block-exclusion region,
  no covered/sunk L0 handling, no apron, no 2 m gap** in mesh mode — remove those paths from the mesh-mode
  selection (keep them for `--near-visual terrain`).
- Hysteresis + crossovers from v1.2 apply to all levels; near-level swaps happen when out of view where
  possible (behind the car / outside the cone).

### C. Materials (mod)
- On `far_tile` with a `material` + `texture` field, create (once) a Material: mapTo = material name,
  colorMap = texture, detailMap = a stock detail texture (pick a neutral grey ground detail from
  /assets/materials/terrain/… and verify the path exists), detailMapUseUV = 1, plus sensible roughness;
  canSave = false; register. Delete the Material when its last tile is deleted. Material creation and
  texture load count against the governor budget (cost estimates from measurements: ~0.3 ms + ~3 ms).
- Mesh mode: `tb:setRenderEnabled(false)` after each import (verify it persists across importMaps).
- Attribution line in the UI: "Imagery: USGS The National Map (public domain); Sentinel-2 cloudless by EOX
  (CC BY-NC-SA 4.0 / CC BY 4.0)" + elevation credits.

### D. Pop-in (#3)
- In mesh mode the whole visible world is one tile system: every in-view swap uses the v1.2 C2 crossover
  (rise/sink) — remove the 30 km "always instant" rule for in-view tiles (keep a transition cap).
- Initial load after teleport stays immediate (loading mode).
- Server: hysteresis band ≥ 25% of the switch distance to stop flip-flopping near thresholds.

### E. Protocol v4
- `hello.protocol = 4`; `hello.nearVisual = "mesh"|"terrain"`.
- `far_tile` gains `material` (string) and `texture` (VFS path) and `detail` (bool: has UV1).
- Everything else unchanged. Keep the mod tolerant of v3-style tiles (no material field ⇒ static palette
  material) so `--imagery off` still works.

### F. Flags (server)
`--imagery usgs,eox2023,eox2016|off` (default the full chain; 2026-11-23: eox2023 removed, CC BY-NC-SA 4.0, see
server/README.md "Imagery"), `--near-visual mesh|terrain` (default mesh),
`--tex-near 512 --tex-far 256`, `--tone 1.0`.

## Acceptance (offline tests + lead in-game)
- Offline: imagery reprojection test (a known lat/lon landmark lands on the right texel), DDS has mips (or
  documented PNG choice), L-2 mesh heights == terrain PNG heights at every vertex, diagonal pattern test,
  selection with no gaps/overlaps in mesh mode, protocol agreement v4, simulated drive (modsim) still passes
  governor limits with the added texture cost, no burst deletes.
- In-game (lead): no visible change near the car across commits (fixed-camera pixel diff ≤ 3 mean);
  no colour border; imagery visible near and far; frame budget like v1.2 (commit ≤ ~65 ms frame,
  streaming p99 ≤ base + 4 ms); far.pops == 0; car never visibly floats/sinks (wheels on the visible ground).
