# Swedish roads

Branch `sweden-next`, off `sweden-roads`, off `main` at 1.7.3-beta.

## What this covers

This page covers the Swedish road work on `sweden-next`: sidewalks and medians on 2+1 roads, the C31 speed limit and B1 give-way signs, road markings, road types, and city crossings, stop lines and priority signs. For each fix it gives the problem seen, the cause, the change, where it is in the code and what was measured.

None of the fixes has been tested in game. The one exception is the user's confirmation that the 2+1 centre barrier works, which is not one of the fixes below.

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

## City data: crossings, stop lines and signs

Commit `25610804`. Before it, `junctions.find` drew a crosswalk on every arm with corners and a stop line behind it, as on a West Coast US street. A Swedish street is painted by rule (TSFS 2010:171): M15 only where a B3 stands or signals are (4 kap. 8 §), M13 only at a B2 or at signals (3 kap. 1, 3 §§). So a junction under the right-hand rule has nothing painted unless one of those stands there.

`server/crossings.py` reads OpenStreetMap's nodes through Overture's infrastructure layer. M15 and M16 come from `crossing:markings`. Tallied 2026-11-02 within 1.5 km of the middle of Borås, Göteborg, Stockholm, Malmö and Umeå: 7427 crossings, 95% within 2 m of a road's centreline. Mid-block crossings are included, kerbs are lowered at unpainted ones, and a node on a junction is left out.

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
| Crossing, stop-line and give-way nodes | OpenStreetMap nodes via Overture's infrastructure layer | `server/crossings.py`: paint, lowered kerbs, stop lines, priority signs |
| Crossing and stop-line rules | TSFS 2010:171 (3 kap. 1, 3 §§; 4 kap. 4, 8, 9, 10 §§); VGU 2024 (9.3.8.1, 12.2.1.4.1); VGU K237740 | Marking sizes and positions in the City data section |

## Known limits

- **Not tested in game.** None of the fixes has been tested in game. The user confirmed that the 2+1 centre barrier works, which is not one of the fixes above.
- **Test suite.** The full test suite has not been run on these commits, because the game was open. 29 related test modules pass.
- **Sign artwork.** Transportstyrelsen's pages state no reuse terms for the artwork. This must be flagged in the PR.
- **Flat tiles.** Tiles are built on flat ground, so the measurements do not include terrain slope.
- **Height steps.** 66 of 620 loaded tiles have at least 15 m² of road mesh steeper than 20%. One spot was examined: (57.6696, 13.0284), a track beside a bridge embankment. The rest are unexplained.
- **Samples.** Fix 3 was measured on 60 tiles and fix 5 on 300, both around Boras. The surface statistics cover seven regions and only rural roads with a surface tag.
- **Road types.** Class "unknown" roads have no OpenStreetMap tags to work from.
- **Widths of gravel and private roads.** No official width was found for gravel or private roads, so the repo's existing STYLE values stay.
