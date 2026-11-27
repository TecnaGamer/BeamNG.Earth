# Tunnel cases: plain input -> expected output

Five cases from the `tunnels-as-mapped` prototype (`BNE_TUNNEL_AS_MAPPED=on`, commit 7168ccff), written as
engine-neutral JSON so a test needs no map data. They answer the request on GitHub #1544 (2026-11-25): T1 takes the
portal and floor cases, T4 takes the twin-box cases.

| File | Stage | Place | What it pins |
|---|---|---|---|
| `kappler_east.json` | T1 | B31 Kappler Tunnel east portals, Freiburg (47.97952, 7.90755) | the portal height from its own approach's 20 m line, not the DEM pixel over the mouth (rule 2) |
| `porte_ditalie.json` | T1 | Paris, Porte d'Italie A6b (48.81611, 2.36028; #1172) | the portal height from its own approach, not a neighbour's cutting (rule 2); an open `layer=-1` stretch between two tunnel ranges (rule 1 / 4) |
| `b31_trough.json` | T1 | B31 Freiburg (47.984, 7.886): Schuetzenallee west entrance -> twin box -> gallery joints -> open trough -> Kappler west portals | town ramp walls (rule 5), box <-> gallery joints with one floor, the trough as a cutting (rule 1 / 4), portals |
| `b31_twin_box.json` | T4 | the Schuetzenallee-Tunnel's two tubes | the middle wall only where both carriageways and 0.6 m fit (rule 7) |
| `porte_ditalie_twin_box.json` | T4 | the A6's two bores under the Porte d'Italie | the same rule on a second twin, with stacked rows (floors > 4.7 m apart: no twin) |

Munich (#362) and Brisbane (#722, `i722_4`) were looked at for the second twin: their twins are low arches (3.6 m,
5.0 m), not boxes, so the A6 twin box from the Porte d'Italie capture is used instead.

## Frame and units

- `frame.origin_lat` / `origin_lon`: the origin of a local ENU tangent plane (`geodesy.EnuFrame`). `x` is metres
  east, `y` metres north of it.
- Every `z` is a geodetic height in metres, as the DEM gives it (floors, walls and ground alike, no earth curvature
  term). DEM zoom 16 (`grading.PROFILE_ZOOM`), the prototype's own readers, scratch cache only.
- Stations `s` run along each carriageway's window line `line`, from 0 at its first point. `feature_station_of_s0`
  gives the window's start on the whole Overture segment (`overture_segment`).

## Format (`schema: bnge-tunnelcase/1`)

`input`:
- `carriageways[]`: `line` (polyline, xy), `highway`, `width_m` / `half_width_m`, `maxspeed_kmh`, `oneway`;
  - `tagged[]`: the OSM ways along the window (`osm_way`, `s0`, `s1`, Overture's `overture_flags` such as
    `is_tunnel` / `is_covered`, `layer`, `osm_tunnel` = `yes` / `avalanche_protector` / null);
  - `tunnel_ranges`: the mapped tunnel ranges (the flags merged), what T1 reads as `SUP_TUNNEL`;
    `tunnel_ranges_joined` = [start, end]: the window cuts a range there, so that end is a joint, not a portal;
  - `stations` every 5 m (`h`, T1's `H_M`): `s`, `xy`, `z_profile` (the prototype's built profile: the floor inside
    the tubes, the graded road outside; `z_profile_source` says which), `g_axis` (the ground over the axis: the
    tunnels' cover ground, the DEM plus lids), `dem_axis` (the bare DEM), `graded_axis` (the graded ground), `side_l`
    / `side_r` (the ground at `half_width + 1.5 / 8 / 16 m`, left / right of the direction of travel: T1's
    `SIDE_OFFS`), `cross_ground` (the ground across the axis at `cross_offsets_m`, -30..+30 m, + left);
  - `held`: the station intervals under a road crossing over a tunnel range on the ground (T1 636), worked out as
    `square.rs`'s `tunnel_record` does: each crossing's half width over its sine (at least `HELD_SIN` 0.3) +
    `HELD_PAD_M` 2 m either side, only where the axis cover there is at least `HELD_MAJOR_LOW_M` 5 m (a major
    carriageway) or `TUNNEL_LID_MIN_M` 3.6 m. The crossings come from `neighbours` without a layer or flags;
    `held_from` lists each one (`osm_way`, `highway`, `s`, `cover_m`, `held`);
  - so `StrokeIn { s, z: z_profile, g: g_axis, side_l, side_r, ranges: tunnel_ranges, joined0/1, kmh, held }` maps
    1:1.
- `neighbours[]`: other roads within reach of the case's lines (their line, graded height and ground every 5 m,
  `layer` / `flags` as fractions of their own segment), e.g. the crossing boulevard peripherique, the branching
  street at the B31 west entrance.
- `dem_grids[]`: 61 x 61 m, 1 m grids round every mouth (`ground[j][i]` at `(x0 + i*dx, y0 + j*dx)`).

`expected`:
- `per_carriageway.<id>`:
  - `sections[]` (`s0`, `s1`, `prototype_kind`, `label` = `box_town` / `bore` / `low_arch` / `gallery` /
    `cutting`, `t1_code` 0 / 1 / 2 (null for a cutting outside every tunnel range), `section_h_m`, `open_side`);
  - `portals[]` (`s`, `xy`, `floor`, `roof_top` = floor + the section's height);
  - `floor_at[]` (every 25 m and at the ends);
  - `walls[]` (tunnelscan's place-wide wall call: base line, `top`, `foot`, `road_z`, `thick`);
- `t1`: per carriageway `expected_from_prototype` (the prototype's sections in T1's codes, its label beside) and
  `t1_actual`: upstream/dev's `tunnel::stroke_record` run on exactly these stations and `held` (`t1.run_on`:
  upstream/dev 241aae43, tunnel.rs last changed in b82b6abf; the module copied unchanged into a small runner, its own
  four tests pass). Against tunnel.rs before `held` (36bd55d3) the runs are the same but for one run end in the B31
  westbound (s 2165.0 -> 2165.225 in b31_trough, 885.0 -> 885.225 in b31_twin_box): `held`'s end is a station of
  its own;
- `portal_end_profile[]`: the 20 m portal profile rule as a pure function: the DEM out of each mouth every 2 m in,
  the road's profile out (exact);
- `town_ramp_walls` (b31_trough): the west entrance walls as the road tiles build them, with the headwall tops;
- `twin_rows` (T4 cases): the built box rows every 2 m (wall offsets, open sides) next to the rule's answer from the
  two axes alone;
- `checks_zero`: tunnelscan's fault counts within 40 m of the case's lines; `check_rows` lists every non-zero row;
- `key_values[]`: the headline assertions, each with `value`, `tol`, `basis` and a one-line `reason`.

`tolerances` (in every file): per kind of value its tolerance, its `basis` and the rule it comes from.
`rules`, `rule_vs_prototype` and `notes` say per case what the rules say and where the prototype differs.

## "rule" vs "prototype"

Every value carries a `basis`:
- `rule`: what the rules in #1544 require; a port should assert it (portal at the mapped end, one floor at a joint,
  no tube over an untagged stretch, a cutting never at grade, no wall in a lane, the wall top at the mouth).
- `prototype`: what the prototype built; a reference with its tolerance, not a target where the maintainer's rules
  decide otherwise.

Where the maintainer's T1 rules expect something else than the prototype:
- **Portal snap** (rule P: outward by at most 30 m where the approach cut would exceed 15 / 8 m): the prototype never
  moves a portal. No case here snaps (`t1_actual.snaps` is empty).
- **Floor** (the profile's within its grade limits, not straight pin to pin): `floor_at` is `prototype`.
- **Section type from cover** (T1: full at >= 7.3 m axis cover, else lid): the prototype builds a town box end to
  end; T1 gives lids near the mouths. Both answers are in `t1`.
- **Clear height** (from `maxheight`): the prototype's box is 4.70 m clear, so `roof_top` is `prototype`.
- **Galleries**: T1 has no gallery kind; they are coded 2 with the label `gallery`. The B31 westbound gallery
  w1481396355 has no Overture flag, so T1 does not see it as a tunnel range at all (it needs the tags in the shards).
- **Porte d'Italie**: at 7168ccff the prototype's d6ccbd4d mouth floor (37.66 m) is 6.2 m under its own approach line
  (43.83 m) and blocked 3.9 m: a regression of the prototype since task 7g fix 1 (43.39 m). The case gives both
  values; the rule's is the one to port.

## Where the values come from

- Map data: Overture roads release 2026-11-18.0 in the scratch cache, OSM tags from the Layercake pin
  `adeb3f6b276284a74b0a2d75f2160714-5555` (the decode-time tag join).
- The extraction tools are not in this folder: they need the prototype (`BNE_TUNNEL_AS_MAPPED`), which is not on
  dev. They are on the fork's branch `tunnels-as-mapped` (Tachy/BeamNG.Earth, `tools/tunnelcases/extract/`):
- `extract/capture.py PLACE LAT LON R OUT.pkl`: runs `tools/tunnelscan.py`'s scan at the place with hooks on
  `roads.TunnelProfiles` (sections, boxes, mouths, pins), `roads.retaining_walls` and the tube rows; samples the
  ground with the scan's own readers. One place at a time, `BNE_TUNNEL_AS_MAPPED=on`, an explicit scratch cache
  (`BNE_TUNNELCASES_CACHE`, default `~/bnge-tests/tunnels/fcache`), a fresh `BNGE_SIM_BUMP`.
- `extract/wallprobe.py`: the road tiles' real build (`tools/tilesmoke.py`) round the B31 west entrance, every wall
  call and portal (task 7p's probe, with a dump).
- `extract/cases.json` lists the cases (windows, checks); `extract/texts.py` writes the rules / notes / key values
  into the scratch copy of it (`$BNE_TUNNELCASES_DIR/cases.json`); `extract/build_cases.py
  $BNE_TUNNELCASES_DIR/cases.json tools/tunnelcases` writes the JSON files and runs `extract/t1run` (build
  it with `cargo build --release` after copying `native/roadsys/src/tunnel.rs` from upstream/dev into its `src/`).
- Pickles and stroke files stay in the scratch folder (`BNE_TUNNELCASES_DIR`, default `~/bnge-tests/tunnels/tcases`).
