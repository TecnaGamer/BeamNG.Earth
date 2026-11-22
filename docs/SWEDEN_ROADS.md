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
- If the tags cannot be read, the tile falls back to the old guess.

## Trams

Commit `1c0cde55`. A tram within 3.5 m of a town carriageway's edge is now street track on paving: a `road_tramline` decal on a 3.4 m paved strip, not a gravel bed with sleepers. Raised sidewalks and medians stop at a tram (`rails.tram_lines`).

- **Source:** VGU 2024 K235543 asks a reserved track to lie at least 3.5 m from the road edge at 40 km/h, measured to the track's middle. This is an inference from that rule. It is not a rule about trams in streets. A track nearer than 3.5 m is therefore treated as gatuspår (street track). OpenStreetMap's embedded tag is not in the data.
- **3.4 m:** no official figure. Half of Göteborg's double tracks lie 3.24-4.19 m apart (median 3.58 m). 3.4 m was chosen so that a pair's paving mostly meets.

Results, central Göteborg, about 150 km of track:

- Ballast inside a sidewalk or median band, with 1.3 m tolerance: 8.1 km -> 1.4 km.
- Strict overlap after the change: 0.32 km on sidewalks, about 0.01 km on medians.

Limits: paved track still lies in a sidewalk's outer band for 3.3 km, because only the curb line is tested. Not tested in game.

## Bus stop signs (E22)

`server/sebusstops.py`, country SE only, built in the sign tile next to the bus stations' name signs. At every mapped bus stop a pole stands at the kerb with a flag read from both directions: E22 Busshållplats (TSFS 2019:74, the small 0.45 m size) and under it a white plate with the stop's name, and its letter ("Läge") where one is mapped.

- **Data:** 58,504 stops are mapped in Sweden; 97.7% have a name and 3.0% a `local_ref` (Overpass, 2026-11-16). A `ref` of one or two characters counts as the letter too; a longer one is the stop's number.
- **Which stops:** the stops the shelters stand at (`shelters.py`), including `shelter=no`, plus `public_transport=stop_position` nodes on the carriageway. Most town stops are mapped that way: 686 of 714 in Göteborg (Overture, 2026-10-21). A stop mapped twice gets one pole.
- **Side of the road:** a node on the carriageway does not say which kerb it is on. The pole goes on the side of the nearest mapped platform within 20 m (39 of 42 in central Göteborg), else on the right of a one-way road, else on a side picked by the stop's id.
- **Placement:** 0.9 m past the carriageway's edge, 2.3 m past the stop's point in the direction the bus travels (the head of the stop, clear of the shelter), the plate's bottom 2.1 m up (TSFS 1 kap. 13 §: at least 2.0 m over a pavement).
- **Left out:** stops on a bridge's deck or on a bus station's ground. A tile makes at most 30 name plates; stops past that keep only E22.
- **Shelters:** unchanged. `shelter=no` already gets no shelter; untagged stops still get one everywhere.
- **Checked in game** (2026-11-17). Spots in central Göteborg: Brunnsparken E1 and E2 (57.7068, 11.9670), Domkyrkan A and B (57.7043, 11.9637).

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
| F2 | 350 m before a spansk sväng on a 2+1 road, outside towns | 3 (counted before the 2+1 requirement) |
| C1 | Both sides of the end of an exit ramp from a fast road, and of a spansk sväng's link | 10 |
| E1, E2 | Where a motorway begins and ends, and on its ramps | 16, 9 |
| B3 | Both sides of a painted crossing without signals, at 60 km/h or less | 181 in about 3 km² of Borås |

**Spansk sväng.** F2 goes only on a confirmed 2+1 road. That is a two-way road in `motesfri.sections` (with the 1+1 connectors between overtaking sections); or a one-way carriageway with a mapped lane count of 2 or 1, next to an opposing one with the other count; or a one-lane one-way piece within 400 m of a section, because OpenStreetMap splits the road into short one-way pieces with no lane tags around the junction's island (Rv 27 south of Borås, 57.6600, 12.9184: a section 236 m north, another 1.1 km south). Ordinary 1+1 and 2+2 roads, unknown lane splits away from a section and short turn pockets do not get one. The data has no turn bans, so the turning link itself is told by shape: a one-way link leaving a main road of 70 km/h or more on the right, 20-300 m long, that crosses back or ends beside it. Service roads are left out. Before the 2+1 requirement the shape rule found 148 links across Sweden, 92% of them on a road with OpenStreetMap's `no_left_turn`, against 42% of plain right slip roads. In a 6 km square around the test spot below the requirement keeps 2 of the 3 links; the one it drops is on a plain 1+1 road.

**How to test in game.** Only F17 has been checked in game. Stop the running server, then run `./run_server.sh` from this branch's checkout.

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

## Road signs (Settings > Experimental > Road signs)

The shared road-sign system (server/roadsigns.py, `sign_systems.json` row `SE`) puts up the warning, mandatory and
information signs its rules find, in Sweden only those whose Swedish design its atlas draws. Each was compared with
Transportstyrelsen's own picture of the sign (transportstyrelsen.se, Vägmärken, fetched 2026-11-17):

| Rule | Swedish sign | Atlas |
|---|---|---|
| sharp bend, double bend | A1, A2 | yellow field (`vienna_y`, as Poland's) |
| road narrows | A5 | yellow field |
| signals | A22 | yellow field |
| roundabout ahead / roundabout | A30 / D3 | yellow field / blue disc |
| cycle path | D4 | blue disc |
| dead end, parking | E17, E19 | blue square |

Left out, as the Swedish sign differs from the atlas's or a Swedish module already places it: B1 (yellow inside) and B2
(furniture.py), C1, E1, E2 and B3 (sesigns.py), E9, E16, E22 (sesigns.py, PRs #1417, #1434), E7 (white figures on
blue), E26 (a portal, with the distance), the chevrons (not checked), and the side-road warning, which Sweden does not
have (A29 is a crossing).

Placement and size (TSFS 2019:74):
- **Beside the road / over a footway** (1 kap. 13 §): 0.3-4.0 m out and 1.0-2.0 m up / 0.5-4.0 m out and at least
  2.1 m up. Used: 1.0 m out and 1.5 m up outside towns, 0.5 m out and 2.1 m up in towns.
- **Sizes**: the normal size wherever the road is not a motorway (2 kap. 3 §, 5 kap. 1 §): triangles 0.90 m, discs
  0.60 m; on the major roads the large, 1.20 and 0.90 m. E17 and E19 0.40 m (6 kap.).
- **Warning distance**: the shared 150 m. It lies in TSFS 2019:74 2 kap. 2 §'s ranges for 60-70 km/h (50-200 m) and
  80-90 km/h (150-250 m); the rules warn only outside towns.
- **Poles**: at most three signs (1 kap., allmänna råd); no rule read on which group goes above which, so each sign has
  its own pole.

## Direction signs (Settings > World > Direction signs)

The shared advance boards (server/dirsigns.py, `dirsigns_styles.json` row `SE`, verified): blue, white text and edge,
as Transportstyrelsen's picture of F1; a European route's number on F14's green plate above the national (riksväg)
and county road (länsväg) numbers in a white frame on the board's blue (TSFS 2019:74 7 kap. 1 §, 24 §). The colours
are the agency's pictures' own: blue 0 85 150, green 0 128 101. Not told apart: the green board TSFS asks for at the
last junction before a motorway or motortrafikled.

## Moose signs (A19)

`sesigns.moose_signs` puts the moose warning sign A19-1 on rural roads, sparingly: the user wanted the typical Swedish sign but not overdone.

- **No data:** OpenStreetMap has 1 A19 within 150 km of Borås, and Trafikverket's API has no road signs, so the signs follow a rule.
- **Rule:** rural two-lane trunk, primary, secondary and tertiary roads at 70-100 km/h, not one-way. A piece of road (a map segment, junction to junction) of at least 1 km and L m long gets a pair with chance L / 15 km, decided by a hash of its id, so every tile and every run agree. Each sign stands 150 m into the piece facing the traffic entering it, 1.5 m past the edge.
- **Measured:** one sign per 12-25 km of such road per direction (Borås 312 km of road, 30 signs; Härnösand 125 km, 10; Jämtland 152 km, 26).
- **Left out:** motorways, 2+1 and divided roads, which have game fences (viltstängsel) for long stretches; where fences end has no data.

## One-way signs (C1 and E16)

`sesigns.oneway_signs` signs town one-way streets at their junctions. OpenStreetMap's `oneway` tag says which roads are one-way (`Spec.oneway`, its direction `Spec.dir`).

- **C1** (no entry), both sides of the end where the street's traffic leaves it, facing whoever would turn in against it. TSFS 2019:74 4 kap. 3 §: it stands where "en väg eller en körbana" meets roads with traffic that could turn in, so a divided road's carriageway gets one too.
- **E16** (one-way traffic), on both corners of the end where the street's traffic enters, facing the crossing road's traffic, its arrow along the street (two models: `se_e16_r` and the mirrored `se_e16_l`). Only where a crossing road is two-way (6 kap. 17 §), and only on a one-way street: a divided road's carriageway is not one (6 kap. 16 §: all traffic the other way must be banned).
- **Size and height.** E16-1 normal size, 0.75 x 0.30 m (6 kap. 1 §). Town signs stand over the sidewalk, so their lower edge is 2.1 m (1 kap. 13 §: at least 2.1 m over a footway). The town C1 is its own model, `se_c1_t`; the ramps' `se_c1` keeps 1.5 m.
- **Where.** Each sign stands 1.5-8 m past the crossing road's edge (the first spot clear of every road), 0.7 m off its own carriageway.
- **Left out:**
  - roads out of town;
  - motorways, trunk roads and ramps, which have their own signs;
  - service roads (drive-throughs, car parks) and junctions where only one joins;
  - roundabouts;
  - one-way pieces under 12 m, which are a junction's own links;
  - one-way roads merging, where no traffic could turn in.
  - a divided road's carriageways: the road is two-way, so neither sign stands on it. A carriageway is one when the map
    pairs it with its other half, or a one-way road runs alongside it within 25 m the other way (the user, 2026-11-17).
  
  Two signs of a kind closer than 4 m that face the same way are one.
- **Measured** (4 x 4 tiles, about 1 km², 2026-11-17): Göteborg 171 C1 and 105 E16; Stockholm 176 C1 and 67 E16.
- **Artwork.** The E16-1 picture is Transportstyrelsen's, the same source as the other signs, in `bne_se_signs2` cell (384, 896).

## Gångfartsområde signs (E9 and E10)

`sesigns.living_signs` signs where a gångfartsområde begins and ends. OpenStreetMap maps one as `highway=living_street`.

- **E9** (gångfartsområde) stands on the right of the traffic turning in, facing them, where a living street meets a street that is not one. **E10** (gångfartsområde upphör) stands on the right of the traffic leaving, facing out. A one-way living street gets only the sign its traffic passes.
- **Left out:** junctions where only service roads (car parks, drives) or other living streets join, since the area goes on there.
- **Size and height.** Normal size, 0.75 m wide x 0.60 m high (TSFS 2019:74 6 kap. 1 §; the figure shows S1 as the width). The agency's web picture is 399 x 286 px, a little flatter than that, so it is drawn to the official shape. Lower edge 2.1 m, over the sidewalk (1 kap. 13 §).
- **Artwork.** Transportstyrelsen's E9-1 and E10-1, in the empty bottom row of `bne_se_signs` (the C31 sheet).
- **Measured** (4 x 4 tiles, about 1 km², 2026-11-17): Göteborg 39 entries, Borås 8, Stockholm 2 (one E9 and one E10 each).
- **Not done:** E7 and E8 (gågata). Pedestrian streets are not in the road tiles, so there is no road for their entrance signs to stand on.

## Bus lanes (M6)

Where OpenStreetMap maps a bus lane beside a general lane (`bus:lanes` / `psv:lanes`, `designated` or `yes`), the line between them is M6, not a solid line.

- **Pattern:** 3.0 m dashes (TSFS 2010:171 2 kap. 11 §), 3.0 m gaps (VGU 2024 figure 12.2.1.3.6-1).
- **Width:** 0.20 m. TSFS allows 0.20, 0.30 or 0.40 m, at least the width of the stretch's M1, so it is never narrower than the lane lines.
- **Not done:**
  - VGU's "1+1" start and end (6 m at 60 km/h or less, 12 m above), because the map does not say where a bus lane begins.
  - M28, the painted word BUSS. The agency's PNG is 64 x 74 px, too small for a decal, and its vector file was not converted.
  - Bus lanes with time limits get M6 too, though TSFS allows it only on lanes without them (12 §). The map's lane tags do not carry the times.
- **Mapped:** 38 bus-lane ways and 53 psv-lane ways in the sampled centres of Göteborg, Stockholm, Borås and Malmö (16 map cells).

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
| One-way roads | OpenStreetMap `oneway` tag | Where C1 and E16 go |
| C1 and E16 rules and sizes | TSFS 2019:74 (4 kap. 3 §; 6 kap. 1, 16, 17 §§; 1 kap. 13 §) | One-way signs |
| Bus lanes | OpenStreetMap `bus:lanes`, `psv:lanes` | M6 between a bus lane and the others |
| M6 pattern and width | TSFS 2010:171 2 kap. 11, 12 §§; VGU 2024 figure 12.2.1.3.6-1 | Bus lanes |
| Gångfartsområde streets | OpenStreetMap `highway=living_street` | Where E9 and E10 go |
| E9 and E10 sizes | TSFS 2019:74 (6 kap. 1 §; 1 kap. 13 §) | Gångfartsområde signs |
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
- **Spansk sväng.** About 8% of what the shape rule found had no mapped ban, measured across Sweden before the 2+1 requirement, which should leave fewer. A real spansk sväng on a 2+1 road whose lanes are not mapped, away from any section, gets no F2.
- **Dashes at joins.** The dashes of two ways do not meet at a join. A fit was tried and reverted, because it moved TSFS's 9.0 m dash to 8.8 m, off the official figure.
