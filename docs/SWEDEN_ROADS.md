# Swedish roads

Branch `sweden-roads`, off `main` at 1.7.3-beta.

## What this covers

This page covers the Swedish road work on this branch: sidewalks and medians on 2+1 roads, the C31 speed limit and B1 give-way signs, road markings, road types, and city crossings, stop lines and priority signs. For each fix it gives the problem seen, the cause, the change, where it is in the code and what was measured.

The branch's author checked the final code in game on 2026-11-02. The measurements below come from tiles built offline.

## Measuring on real tiles

Real tiles are built offline with `roads.build_tile` from the cached map data, on flat ground, country SE. Top-down images of each tile's chunks are rendered for inspection. Road meshes the game loaded (`rs_*_road.cdae`) are rasterised to look for height steps.

- Fix 3 (lines) was measured on 60 real tiles around Boras. Fix 5 (dashes) was measured on 300 real tiles around Boras.
- Mesh scan of 620 loaded tiles: no height steps between overlapping surfaces in the 196-tile rasterised sample.
- 66 of 620 tiles have at least 15 m² of road mesh steeper than 20%. The one spot examined, (57.6696, 13.0284), is a track beside a bridge embankment. The rest are unexplained (see Known limits).

## Fixes

| Problem seen | Cause | Fix | Where in the code | Measured result |
|---|---|---|---|---|
| 1. Thick sidewalk medians on 2+1 roads | A nameless pair of one-way carriageways got a median value, and `has_median` drew a curbed band. Urban-flagged fast pairs had it too. | Nameless pairs are only kept when they overlap. A one-way road of 80 km/h or more has no sidewalks unless mapped. In Sweden, rural cable barrier is kept past houses. | `roads.NO_WALK_KMH` (server/roads.py); `flags_of` (server/medians.py). Commit e6b4be77 | No measurement recorded. Not tested in game. |
| 2. C31 speed limit and B1 give-way signs did not look like the real signs | The kernel's Vienna sign and flat triangles did not look like the real ones (commit b0dd9251) | Transportstyrelsen's own artwork on one 71 KB sheet, 1024 × 1024 px with 256 px cells (`bne_se_signs.png`). C31 is a mesh with a 32-sided disc, folded edge, grey back and round pole. B1 has rounded corners. Sizes are in the table below. | C31: `speedsigns.sweden_mesh`. B1: `tools/build_median_props.py`. Sheet: `tools/build_swedish_signs.py`. Commit b0dd9251 | No measurement recorded. Sizes are from TSFS 2019:74. Not tested in game. |
| 3. Unmarked stretches between marked ones | Every road cut every other road's lines: the next OSM way of the same road, and every side road and drive | `roads.line_through` returns one of three cases. **Straight**: a road going on in another, no line stops. **Stem**: a lesser road or the stem of a T; centre and lane lines run across the mouth, edge lines stop. **Whole**: a through road's piece between two close junctions keeps its lines. A real crossing still stops the lines. | `roads.line_through`. Commit 79175770 | 5.4% of centre line length missing on 60 real tiles around Boras, in bites of 5-10 m; 0.9% after. |
| 4. Two lanes' markings on a road far too narrow | Paired one-way carriageways were capped to one equal width, down to `DUAL_MIN_W` 3.5 m | `roads.dual_cap` shares the room between a pair by lane count: for example 8.4 m and 4.2 m on a 2+1 road instead of about 6.3 m each. `markings.MIN_LANE_M` 2.75 m: no lane line may be drawn if it would leave a lane narrower than this. | `roads.dual_cap`; `markings.MIN_LANE_M`. Commit 79175770 | The barrier was measured 0.4 m clear of both edges. |
| 5. Missing dashes | The cut that removes stubs under 3 m (`MIN_PIECE_M`) also removed dashes: a Swedish edge line's 1 m dashes went from every tile with a second road, and one dash at every tile border | Dashes are kept down to `roads.DASH_MIN_M` 0.3 m. | `roads.DASH_MIN_M`. Commit fdb550e3 | Before and after, 300 real tiles around Boras: Swedish edge dashes 0.3 -> 6.4 km of paint, centre dashes 2.1 -> 4.1 km, lane dashes 0.9 -> 1.9 km. |

Sign sizes for fix 2:

| Sign | Size | Source |
|---|---|---|
| C31, ordinary roads | 0.60 m | TSFS 2019:74 4 kap. 1 § |
| C31, motorways | 0.90 m | TSFS 2019:74 4 kap. 1 § |
| B1, across the rounded corners | 0.90 m | TSFS 2019:74 3 kap. 1 § |
| Lower edge of a sign | 1.0-2.0 m | TSFS 2019:74 1 kap. 13 § |

### Other commits on the branch

Not covered by the table above. Listed by subject from `git log`.

- `0fbe4bbb` Roads read as Swedish: one width in town and out, no parking lanes, no town street at 70 km/h or more (Trafikförordningen 3 kap. 17 §), and rural unclassified, service and TomTom roads with no surface are gravel.
- `c551e228` Centre barriers on 2+1 roads: a cable barrier on the line between the directions along sections and the short stretches between them, open at junctions, none along a mapped barrier.
- `9fb1e94c` Road markings from TSFS 2010:171, Vägmärkesförordningen and VGU 2024; 2+1 roads as VGU's table 12.5 (no centre line, solid lines each side of the barrier).
- `8101c8b1` Kantstolpar (roadside posts) as VGU 2024 12.2.2.1, both sides of rural main roads of 80 km/h or more.
- `7faf6df1` Give-way at junctions: on the road entering a main road, and nothing between roads of a class (Trafikförordningen 3 kap. 18, 21 §§); yellow B1 of TSFS 2019:74's size.
- `4a37734a` Give-way line M14 (TSFS 2010:171 4 kap. 5, 7 §§) where a B1 stands.
- `8e75d5e6` Lane drops on 2+1 roads: M20 arrows in the overtaking lane before it ends (VGU 2024 12.2.1.5.3, table 12.14).
- `e812161d` The AI gets the lanes a road has room for: the fit moves from the paint into `lane_layout`, so navgraph and motesfri plan the lanes the paint shows.

## City data: crossings, stop lines and signs

Commit `25610804`. Before it, `junctions.find` drew a crosswalk on every arm with corners and a stop line behind it, as on a West Coast US street. A Swedish street is painted by rule (TSFS 2010:171): M15 only where a B3 stands or signals are (4 kap. 8 §), M13 only at a B2 or at signals (3 kap. 1, 3 §§). So a junction under the right-hand rule has nothing painted unless one of those stands there.

`server/secrossings.py` reads OpenStreetMap's nodes through Overture's infrastructure layer. M15 and M16 come from `crossing:markings`. Tallied 2026-11-02 within 1.5 km of the middle of Borås, Göteborg, Stockholm, Malmö and Umeå: 7427 crossings, 95% within 2 m of a road's centreline. Mid-block crossings are included, kerbs are lowered at unpainted ones, and a node on a junction is left out. Commit `1c0cde55` also passes the nodes to earthd's tile and AI graph paths, which `25610804` had missed (it gave them to blockworker only).

**Assumption:** a crossing with no word on its paint counts as painted. This is the tags' majority, not a certainty: 614 of 722 signal crossings that say have a zebra, and 296 of 492 unsignalled crossings that say are marked.

### Sizes

| Marking | Dimension | Value | Source |
|---|---|---|---|
| M15 bars | Width, gap | 0.50 m, 0.50 m | TSFS 2010:171 4 kap. 9 § |
| M15 bars | Length | at least 2.5 m at 60 km/h or less; at least 4.0 m above | 4 kap. 9 § |
| M16 squares | Side, gap | 0.50 m, 0.50 m | 4 kap. 10 § |
| M16 rows | Spacing | 2.5 m apart | VGU 2024 råd for a two-way cycle crossing (9.3.8.1) |
| M13 | Width | 0.40 m | 4 kap. 4 § |
| M13 | Distance from the crossing road | 1.0 m | VGU 12.2.1.4.1 råd |
| M13 at signals | Distance before the crossing | at least 2.0 m | VGU K237740 |
| M13 at a stop sign, M14 | Car between line and crossing | 5.0 m råd: if a car fits, the line stays at the junction; otherwise it goes before the crossing | VGU 2024 12.2.1.4.1, 12.2.1.4.2 |

The stock crosswalk texture has 0.60 m bars; M15 is drawn with 0.50 m bars.

### Results

Before -> after, 40 tiles each:

- Central Borås: zebras 118 -> 36, stop lines 102 -> 4.
- Göteborg: zebras 156 -> 187 (more, because mid-block crossings now exist), stop lines 136 -> 81.

Mapped give-way and stop nodes add to the rank guess, for props and the AI: 47 more give-way arms at 42 of 1286 junctions in Borås, Göteborg and Stockholm.

### Limits

- Not tested in game.
- Which side of a crossing the cycle crossing lies on is not in the tags, so it follows the road's own direction.
- Stop lines at mid-block signal crossings are not drawn, because no signal prop stands there.
- B3 signs are not placed yet.
- If the tags cannot be read, the tile falls back to the old guess.

## Trams

Commit `1c0cde55`. A tram within 3.5 m of a town carriageway's edge is now street track on paving: a `road_tramline` decal on a 3.4 m paved strip, not a gravel bed with sleepers. Raised sidewalks and medians stop at a tram (`rails.tram_lines`).

- **Source:** VGU 2024 K235543 asks a reserved track to lie at least 3.5 m from the road edge at 40 km/h, measured to the track's middle. This is an inference from that rule. It is not a rule about trams in streets. A track nearer than 3.5 m is therefore treated as gatuspår (street track). OpenStreetMap's embedded tag is not in the data.
- **3.4 m:** no official figure. Half of Göteborg's double tracks lie 3.24-4.19 m apart (median 3.58 m). 3.4 m was chosen so that a pair's paving mostly meets.

Results, central Göteborg, about 150 km of track:

- Ballast inside a sidewalk or median band, with 1.3 m tolerance: 8.1 km -> 1.4 km.
- Strict overlap after the change: 0.32 km on sidewalks, about 0.01 km on medians.

Limits: paved track still lies in a sidewalk's outer band for 3.3 km, because only the curb line is tested. Not tested in game.

## Lane ends on 2+1 roads and signs from the road's shape

Sources: VGU 2024 (remiss version 0.9) and TSFS 2019:74, requirement numbers in `server/motesfri.py` and `server/sesigns.py`.

**The closure** (`motesfri.py`). The road surface keeps its width, as real 2+1 roads mostly do, and is painted into the ground, so the closure is the barrier and the paint:

- The barrier moves in one smooth curve across the joint between two sections, 150 m long at least (K236067). It used to move in two pieces over about 35 m.
- Where a 2+1 stretch ends on a plain road, the barrier comes back to the road's centre line inside its own last 150 m (`Whole`). It used to stop off-centre.
- The centre lines follow the barrier, and the lane line stops a third of the way along the move, where the lane is 2.5 m wide on a 3.25 m lane (K236077).
- On a carriageway drawn as its own one-way way, the left edge line moves in to leave one lane, then goes back to the edge over 100 m. A lane that begins on such a carriageway does the same over 100 m (K236066).
- M20 arrows count from where the lane line stops (K237774-6), not from the joint.

`bend_lines` bends the lines in a tile's finished chunks, so it does not matter how the road builder made them.

**Signs** (`sesigns.py`, country SE only). Forest rows after every barrier's, because `medians._mapped_segments` reads what comes before as barriers.

| Sign | Where | Near Borås, 18 km across |
|---|---|---|
| F17, F17 + T2 400/300/200 m | Just before the first merge arrow (20 m), the plate 400/300/200 m before the arrows, both sides (the median one halfway to the oncoming carriageway where the two are drawn close); VGU's 50 m before the 2.5 m point came too late in the game. On two-lane stretches of 300 m or more, or shorter ones the map tags `merge_to_*` (not an on-ramp's acceleration lane); two lanes are measured back through a wider carriageway beside an exit lane | 16, 18 |
| F18 | Where a plain road becomes a section, or forks into a pair of carriageways | 4 |
| F2 | 350 m before a spansk sväng, outside towns | 3 |
| C1 | Both sides of the end of an exit ramp from a fast road, and of a spansk sväng's link | 10 |
| E1, E2 | Where a motorway begins and ends, and on its ramps | 16, 9 |
| B3 | Both sides of a painted crossing without signals, at 60 km/h or less | 181 in about 3 km² of Borås |

**Spansk sväng.** The data has no turn bans, so it is told by shape: a one-way link leaving a main road of 70 km/h or more on the right, 20-300 m long, that crosses back or ends beside it. Scored against OpenStreetMap's `no_left_turn` relations across Sweden: 148 hits, 92% with a ban, against 42% for plain right slip roads. Service roads are left out.

**How to test in game.** Not yet checked in game. Stop the running server, then start this branch's: `cd ~/.cache/bngearth-dev && ./run_server.sh` (no leading `!` in fish).

| Check | Coordinates |
|---|---|
| Lane closure, F17 and the 400 m plate | 57.5331, 12.79387 |
| Lane closure after an exit lane, F17 and the 400 m plate (E4 north, Härnösand) | 62.6482, 17.8979 |
| Lane end past pieces with no lane count, both F17s (Rv 27 south-east, Borås) | 57.6371, 13.0773 |
| Spansk sväng | 57.65439, 12.91846 |
| Its F2 sign | 57.66319, 12.91821 |
| Motorway E1 signs | 57.6979, 12.8242 |
| B3 at zebra crossings, central Borås | 57.7210, 12.9401 |

Look for: a barrier curve of about 150 m with the lines following it, the lane line stopping before the arrows run out, and each sign on the correct side and facing the traffic it is for.

Not built:

- **Speed cameras (E24):** the Overture infrastructure data has no such class across 30 cells round Göteborg.
- **Town signs (E5):** town limits are not mapped well enough to place them.
- **Ferries:** they need a ship model and movement.
- **Speed bumps and raised crossings:** they need a physical bump, and a sign alone would mislead.
- **Road number signs (F14):** each number needs its own texture.
- **Bus lanes:** M28 needs a texture of its own, and how many lanes the data shows was never counted.
- **Warning line before a solid line (M3, K237689):** the dash pattern is chosen per way, so changing the last 100 m needs a change in the line builder.
- **Rest areas:** not attempted.

## Moose signs (A19)

`sesigns.moose_signs` puts the moose warning sign A19-1 on rural roads, sparingly: the user wanted the typical Swedish sign but not overdone.

- **No data:** OpenStreetMap has 1 A19 within 150 km of Borås, and Trafikverket's API has no road signs, so the signs follow a rule.
- **Rule:** rural two-lane trunk, primary, secondary and tertiary roads at 70-100 km/h, not one-way. A piece of road (a map segment, junction to junction) of at least 1 km and L m long gets a pair with chance L / 15 km, decided by a hash of its id, so every tile and every run agree. Each sign stands 150 m into the piece facing the traffic entering it, 1.5 m past the edge.
- **Measured:** one sign per 12-25 km of such road per direction (Borås 312 km of road, 30 signs; Härnösand 125 km, 10; Jämtland 152 km, 26).
- **Left out:** motorways, 2+1 and divided roads, which have game fences (viltstängsel) for long stretches; where fences end has no data.

## Road type detection

Findings, and the rules built on them, which shipped in `0fbe4bbb`. The commit message says what the rules do.

Rural roads with a surface tag (OpenStreetMap tags, as delivered through Overture), km paved and km unpaved over seven regions sampled (Boras, Skåne, Mälardalen, Jämtland, Umeå, Norrbotten, Småland), 2026-11-02:

| Class | Paved km | Unpaved km | Unpaved share |
|---|---|---|---|
| unclassified | 97 | 748 | 89% |
| service | 40 | 121 | 75% |
| tertiary | 264 | 81 | 23% |

- Unclassified and service roads in this sample are mostly unpaved. Tertiary roads are mostly paved. Town streets are paved.
- Roads without a surface tag are not counted.
- Class "unknown" roads are TomTom data via Overture, with no OpenStreetMap tags at all. About 100 km of them lie within 25 km of Boras.

## Data sources

| Data | Source | Used for |
|---|---|---|
| Road ways and their tags | OpenStreetMap, cached map data | Input to `roads.build_tile`; the road, line and sidewalk rules above |
| Roads of class "unknown" | TomTom data via Overture (no OpenStreetMap tags) | Road type findings |
| Road surface tag | OpenStreetMap tags, as delivered through Overture | Surface statistics under Road type detection |
| C31 and B1 artwork | Transportstyrelsen's own artwork (`bne_se_signs.png`) | Sign textures for fix 2. No reuse terms stated on its pages |
| Sign sizes and lower edge | TSFS 2019:74 (4 kap. 1 §, 3 kap. 1 §, 1 kap. 13 §) | Sign sizes for fix 2 |
| Minimum lane width | VGU 2024 K233046 | `markings.MIN_LANE_M` (2.75 m) |
| Road meshes loaded by the game | `rs_*_road.cdae` files | Height-step checks on loaded tiles |
| Crossing, stop-line and give-way nodes | OpenStreetMap nodes via Overture's infrastructure layer | `server/secrossings.py`: paint, lowered kerbs, stop lines, priority signs |
| Crossing and stop-line rules | TSFS 2010:171 (3 kap. 1, 3 §§; 4 kap. 4, 8, 9, 10 §§); VGU 2024 (9.3.8.1, 12.2.1.4.1); VGU K237740 | Marking sizes and positions in the City data section |

## Known limits

- **Test suite.** Full run on the final code: 147 of 153 modules pass. Five of the six that fail also fail on clean 1.7.3-beta: test_advance_sign_layout (fontTools missing), test_fargeo and test_roadgeo (real-data tests), test_e2e and test_integration_drive (far tiles). The sixth, test_server, fails now and then on clean code too, on its "first 8 sent ranked" order check.
- **Sign artwork.** Transportstyrelsen's pages state no reuse terms for the artwork. The signs themselves are set out in Vägmärkesförordningen (2007:90).
- **Flat tiles.** Tiles are built on flat ground, so the measurements do not include terrain slope.
- **Height steps.** 66 of 620 loaded tiles have at least 15 m² of road mesh steeper than 20%. One spot was examined: (57.6696, 13.0284), a track beside a bridge embankment. The rest are unexplained.
- **Samples.** Fix 3 was measured on 60 tiles and fix 5 on 300, both around Boras. The surface statistics cover seven regions and only rural roads with a surface tag.
- **Road types.** Class "unknown" roads have no OpenStreetMap tags to work from.
- **Widths of gravel and private roads.** No official width was found for gravel or private roads, so the repo's existing STYLE values stay.
- **Lane ends with no barrier move.** Where a section meets a short way with no lane split tagged, the barrier cannot tell where to go, so it keeps its offset and the lane line stops at the joint. 2 of 13 lane ends in the two sampled areas.
- **Wide single lanes.** Past a two-way lane end the road keeps its width, so a connector between sections can have a single lane near 6 m wide.
- **Spansk sväng.** About 8% of what the shape detector finds has no mapped ban, measured on a sample of Sweden.
- **Dashes at joins.** The dashes of two ways do not meet at a join. A fit was tried and reverted, because it moved TSFS's 9.0 m dash to 8.8 m, off the official figure.
