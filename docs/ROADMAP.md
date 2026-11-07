# BeamNG.Earth roadmap (rewritten 2026-10-17, v1.4.28-beta)

Goals (user, 2026-10-10): look as good as the official maps (cities, trees, paved roads, varied
environments), a world-map GUI, GPS over continental distances (Cannonball Run, ~4500 km), AI
traffic with working signs and traffic lights, and performance everywhere as content grows. Hard
requirements: players routinely drive 150-300 mph (67-134 m/s), and some fly planes (mods).
Public beta since 2026-10-16 (posted on r/BeamNG): player reports come first.

The previous, longer roadmap (per-version design notes, measurements, research pointers) is in git
history (`git log -p -- docs/ROADMAP.md`, the version before 2026-10-17); docs/ARCHITECTURE.md and docs/V1_*.md hold the designs.

## Principle: improvements must generalise (user, 2026-10-10)
"An improvement in one part of the world should improve things everywhere." Places like Fells Point,
Monument Valley, the Kern River ravine or a famous trail are test cases, never special cases:
- No location-specific code or hard-coded coordinates in the generator. Every rule is driven by data
  available everywhere (elevation, land cover, climate zone, OSM/Overture tags, imagery statistics),
  with sensible fallbacks where data is missing.
- Per-region choices are allowed only for data sources (e.g. the best imagery per country), never for
  how the world is built from them.
- Every tuning change is checked on a fixed, diverse benchmark set of places (all continents and
  biomes, data-rich and data-poor, flat and steep, cities and wilderness; the list so far is
  docs/PLACES.md; the full set with its cameras is docs/benchmarks.json, run by tools/benchmark.py,
  docs/DEVTOOLS.md "Visual benchmark"), with before/after screenshots and numbers, plus a held-out set that is not used for tuning. A change that improves
  the test place but makes the benchmark worse elsewhere is rejected or reworked.
- Fixes like de-shadowing and colour matching are global methods with automatic gates (they switch
  themselves off where they do not apply), validated on places with and without the problem.

## Principle: deterministic generation (user, 2026-10-10)
The same place must come out the same every time it is generated with the same version: a rock,
tree, building or road marking is always in the same spot, whichever way the player arrived, in
whatever order tiles loaded, after any number of rebases, on any machine.
- Every procedural choice (placement, species, rotation, size, colour variation, roof shape, filler
  buildings in data-poor areas) is a pure function of (world position in a global frame, the input
  data, a fixed per-feature salt, the generator version) - e.g. a hash of the global cell id or of
  the feature's OSM/Overture id. Never of the anchor, tile key, LOD level, load order, thread timing,
  time of day or an unseeded RNG.
- Positions are computed in a global frame (lat/lon/height or ECEF, doubles) and only then
  transformed into the current anchor frame, so a rebase or teleport cannot move anything.
- LOD only changes how detailed something is drawn, never whether or where it exists: the coarse
  version of a forest is the same trees, thinned by a stable rank (each object keeps one fixed
  random rank; a level shows the objects below its threshold).
- Inputs are versioned: generator version + data snapshot ids (imagery capture, DEM source, Overture
  release, OSM date) are part of cache keys and the credits; a data update may change a place, a
  restart or a different route never does. For identical results across players, pin data snapshots.
- Things that are real-time by nature (weather, time of day, traffic) are exempt, but their random
  details (cloud pattern seed, gust phase) are also derived from position + time slot, not from RNG.
- Tests: generate the same area from different anchors, paths and load orders and compare a hash of
  every placed object; a golden-hash test per release; float tolerance only where unavoidable.

## Where we are

| Area | Status |
|---|---|
| v1.4 Speed | Done: horizon far field, 512 m blocks following the car, look-ahead loading, rebases with nothing disappearing, 20 km at 100 mph at ~140 fps. First visits: ~60-95 s for the far field (remote servers' cold caches). |
| v1.5 Ground truth | Done: per-material surface physics, grass, de-shadowed and colour-matched imagery. |
| Water | Paused (user 2026-10-13): sea, lakes, rivers work; known issues below. |
| v1.6 Roads | Done: road meshes, sidewalks, curbs, intersections, bridge decks, tunnels (1.4.20). |
| v1.7 Navigation | Done: navgraph, GPS routes, place / address search, world map with teleport. |
| v1.8 Alive | Done: parked cars, AI traffic (setting, default off), signals, stop signs, street furniture, trees. |
| v1.9 Cities & nature | Partly: buildings with collision, canopy trees, filled sparse areas. |
| Quick wins | Done: saved places (map), launcher (Linux AppImage, Windows .exe), real weather, airports, data credits. |
| Interchanges | In progress (opt-in `BNE_INTERCHANGE=1`): freeway carriageways solved together, headroom from OSM maxheight; notes, known issues and next steps in docs/INTERCHANGE_SOLVER.md. |
| Robustness | Mostly done: lost server / offline handling, orphaned servers, level isolation (issue #1). |

## Next (in this order unless the user reorders)

### Bugs (reported)
1. **Roundabouts** (Harbor Point, 39.2801, -76.5890): the ring drawn as cobbles with a D-shaped island,
   odd sidewalk pieces at the entries, road patches that do not meet. Needs ring handling: a one-way ring
   road, a raised round island, splitter islands. *1.4.29: the island and the ring's sidewalks were never
   drawn (held render-only meshes); now shown. The D-shaped island is the map's own outline.*
2. **Sidewalks at offset multi-road junctions** (Fells Point 39.28188, -76.59114): connectors ~10 m apart
   get no curb returns; corners must be built across the merged junction from its outer branches.
   *1.4.32: nodes joined by a connector no longer than the road is wide are one junction; its corners turn
   onto the connector's edge at one node and off at the other (2 -> 4 corners there; the 36-tile Fells Point
   survey 428 -> 440 corners, no new slab on a carriageway). Left: the 73-degree corner on a curving branch.*
3. **Sidewalk pulls the car down** (39.28499, -76.60554): a wheel sank at a sidewalk end beside a verge;
   suspect the slab's end face or a curb-ramp edge in the collision mesh. *Fixed in 1.4.29: the slab's back
   slopes down at 1:2 instead of a 20 cm ledge.*
4. **Raindrops jumping / streaking** (Fells Point rain): the Precipitation box following the camera or the
   rebase, or gusts changing its speed every frame. *Fixed in 1.4.30: the drops' rotateWithCamVel drew them
   as screen-wide streaks even with the car still; off now, drops 0.2 m as west_coast_usa.*
5. **Coarse ground tiles over roads** 150-300 m from the car: sink coarse tiles under roads, or draw far
   roads into the tile imagery. *Not seen since the terrain block and its fine edge ring (2026-10-13); not
   reproduced 2026-10-17 (San Francisco, Pittsburgh street views). Re-open with a screenshot if seen.*
6. **Parking garages are solid boxes**: an open ground storey (columns, a slab) where the map has an entrance.
   *Open decks with a ramp shipped in 1.4.5; 1.4.31: the ramp tries the four longest walls (an L-shaped
   garage's longest wall can be a thin leg), split levels counted as floors make whole decks, and a building
   under a mapped multi-storey car park is a garage whatever its building tag (all 18 downtown Baltimore
   garages are decks now, 15 of 16 in downtown Denver).*
7. **Huts above the tree line are grey boxes** (Mount Whitney summit shelter): a stone wall look. *Fixed in
   1.4.30: a small isolated hut above the tree line (by latitude) gets stone walls.*

### Reddit feedback (r/BeamNG release thread, read 2026-10-17; in this order)
8. **Bumpy roads**, worst uphill and at speed (three players): smooth the road profile / the terrain under it.
   *1.4.35: the grading plan's vertices 16 -> 4 m (a slope kink every 16 m on crests and sags; a San Francisco
   street in-game: slope change per 2 m 4.4 % rms / 11.5 % max -> 0.8 % / 1.9 %).*
9. **Sidewalks lying flat like a plane** instead of following the ground, and **non-colliding geometry over
   roads**; everything feeling oversized (buildings crowding narrow streets); some roads missing.
10. **Las Vegas Strip partly flooded** (water where there is none).
11. **Spawn location**: pick the start place (now Mount Whitney); maybe a start per country.
12. **The map does not zoom out from the car** when opened, as the stock map does.
13. **Outdriving the loading over 100 mph** on Chicago highways; choppy in dense woods.
14. **Quality presets** (Steam Deck, low-end PCs).
15. **Multiplayer (BeamMP)**: asked by five or more players; big, later.
Not possible / out of scope: a repo-only version (GeForce Now; the mod needs its server), Windows 7.

### Performance and loading
- First-visit far field: fewer requests per place (e.g. Mapterhorn PMTiles range reads) rather than more
  threads (2026-10-17 A/B: threads and DEM prefetch made no difference).
- File events: the tile-named shape files beside their pooled slots (two events per shape; staging.py
  already removed the .tmp ones).
- Remaining Lua / Ticking hitches at 100 mph (5 and 10 per 20 km).
- More Rust where a profile shows real CPU (per-thread CPU from /proc, not the wall-clock sampler).

### Content
- Traffic on by default once driven by the user; denser parked cars.
- Night lights: lit windows, street lamps; light pollution in the night sky.
- Underwater tunnels (Fort McHenry): water kept out of portal trenches; merge Overture's short pieces.
- Water issues: confluences and river mouths, near-camera river mesh, stretched river textures, seams.
- Mailboxes, street-name blades, pole-mounted business signs; airport buildings.

## Later (approved, not scheduled)
- Overhangs, arches and caves (3D rock meshes on the height field; lidar where it exists).
- Railways, power lines, fences, guardrails; solar and wind farms; gas stations with refuelling.
- Seasons and snow cover; wind effects; sound design; lava.
- Several vehicles far apart (single player), then multiplayer (BeamMP; last or near last).
- Generated challenges: time trials, a timed Cannonball run, deliveries.
- Offline pre-download of a region or route.
