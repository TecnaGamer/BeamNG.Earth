# OpenStreetMap `placement` on one-way carriageways: design

## What is wrong

OpenStreetMap's `placement=*` says where a one-way way's line runs across its lanes. Mappers use it where a lane is
added beside the through lanes, so the line stays straight. An exit lane on the right, for example, is mapped as
`lanes=3`, `turn:lanes=none|none|slight_right`, `placement=right_of:1`: the line is the right edge of lane 1.

The game ignores it and centres every carriageway on its line, so such a piece stands half a lane, or more, too far
to the side the line is placed toward.

On the A93 southbound before Kreuz Regensburg (49.00528, 12.06115) both directions have such a piece (OSM ways
308799828, 308799832, 324848154, 324848155). The consequences there:

- each 18 m carriageway is drawn 2 m into the median, and the two overlap by 2.3 to 5.2 m;
- the asphalt merges, with no median strip;
- the median barrier is left out for 290 m (`medians.py` puts none inside a carriageway);
- the lanes, markings and the traffic AI's lanes are 2 m off too.

Moving the four pieces 2 m in an offline run of the game's own median code (`medians.modules` on the cached cells)
closes the barrier's gap.

How common it is, from the Geofabrik Bavaria extract:

| Tag | Motorway ways | km |
|---|---|---|
| `placement=right_of:*` | 3,115 | 535 |
| `placement=middle_of:*` | 1,225 | 302 |
| `placement=left_of:*` | 25 | 4 |
| on trunk roads | 583 | 98 |
| all motorway ways (for scale) | | 5,186 |

So about 16 % of the motorway length carries the tag.

The weekly lane shards already carry `placement` (`tools/build_lane_tags.py` LANE_KEYS), and the server joins it onto
the features (`railtags.LANE_COLUMNS`, `roads.lane_tag_parts`). No data change is needed.

## Goals

- A placed one-way piece is drawn where its tag puts its lanes. That covers the asphalt, its edges and markings, the
  roadside built along it (sidewalks, guard rails, median barriers, the roads the signs see), the traffic AI's lanes,
  and a deck it runs onto.
- Continuous edges where a placed piece meets its neighbours: through the existing width tapers (`road_tapers`), and
  between pieces whose placement differs at the same width.
- Unchanged output for every way without `placement` that does not meet a placed piece end to end, and wherever the
  lane shards are not there. A neighbour of a placed piece moves within its taper only (at most half the placed
  piece's shift).

## Non-goals (this PR)

- Two-way roads' `placement:forward` / `placement:backward`: they keep today's line.
- `placement=transition`: kept as today (centred). It is 0.4 km in Bavaria.
- The far field's low-detail roads: a 2 m shift doesn't show from there.

## The rule

The shift is taken relative to how the game draws an unplaced line: centred, the lanes 1.5 m left of the middle
because of the asymmetric shoulders. That keeps a placed piece's lanes continuous with an unplaced neighbour's.

With n the way's `lanes` (the part's own tag, else the spec's), k the tag's lane number (clamped to 1..n) and w the
class's drawn lane width (`roads.LANE_W`: 4.0 m on motorways), the full shift D to the right of travel is:

| Tag | D |
|---|---|
| `right_of:k` | (n/2 - k) * w |
| `middle_of:k` | (n/2 - k + 0.5) * w |
| `left_of:k` | (n/2 - k + 1) * w |

There is no shift without n, with a k that doesn't parse, or for a link.

The rule is global: the tag's definition is geometric, with no country or driving-side variant. The accepted
proposal (Proposed_features/placement) says "the lanes are numbered from left to right with the leftmost lane equal
to lane number 1", and "the road is viewed in the direction of the OSM-way". So the shift's side follows the way's
drawn direction, not the driving direction. That differs only on `oneway=-1` ways: in Bavaria all 4,947 placed
motorway and trunk ways are `oneway=yes` or untagged (implied one-way), none `oneway=-1`, none
`placement:forward`/`:backward`.

Left-hand traffic needs nothing of its own. A British exit lane on the left, as lane 1 of 3, is mapped
`right_of:2` or `left_of:2`, and the same formula moves the carriageway left, toward the added lane.

Real lane widths (3.5, 3.65, 3.75 m) don't enter either. The shift uses the class's drawn lane width, so it matches
the game's own drawing everywhere.

At a joint where two pieces meet end to end, `road_tapers` runs both pieces' widths to the mean of the two (16 m
between 14 and 18), each over its own taper. Their centres do the same: each piece's centre runs from its own shift
to the mean of both pieces' shifts at the joint, with the same smoothstep and span as its width. Both edges are then
continuous at the joint, each at the mean of the two pieces' edges. Take a 2-lane piece (shift 0) going on as a
placed 3-lane one (`right_of:1`, 2 m right): both stand 1 m right at the joint. The 2-lane piece widens by 2 m while
it moves 1 m, so its left edge stays 7 m left of the line on both sides, with the lane opening on the right. (On the
A93 itself every end of a placed piece is a junction with a ramp, below: there the plain shift does it.)

A deck's end keeps its width, and the ground runs to it, so the ground's centre runs to the deck's shift. Every
piece of a feature (its ground and its own deck) takes all of the feature's joints, so a deck that begins inside a
ground joint's taper eases with the ground. Two
pieces of one width have no taper. Their centres still run to the mean, eased about the joint over half the easing
below on each side. Where more than two roads meet (an exit lane's end, where the ramp leaves) nothing is eased: the
taper kernel leaves the width to change across a junction, and a placed exit lane's median edge already lines up with
the next piece's there.

Where neighbouring parts' D differ at the same width (a way split into placed and unplaced parts, or several OSM
ways merged into one Overture feature), the change is eased with a smoothstep over 40 m per m of change, kept
between 40 and 160 m, as `lane_shifts` eases its line shifts.

Where a placement shift applies, the feature's `lane_shifts` (the lane-line shifts that guess which side an added
lane opens on at a merge or split) are not added: the mapped placement is the answer to that question.

## Where it applies

One shared object, `roads.placement_context(cells, urban_fn, widths, left_hand)`, made once per tile, gives a
piece's lateral shift per point (m, + left of the geometry: `offset_polyline`'s sign) from its tags and its joints,
from the joint's own cell as `joint_desc` takes tapers. Without a `placement` in the cells it moves nothing, at no
cost. The callers move every piece's line by it where they turn the piece into drawn geometry:

- `roads.build_tile`, as the tile's ground roads are listed. Markings, sidewalks, the blockers and `roads_out`
  follow, so the median barriers (`medians.tile_rows`) see the moved carriageways.
- `surface.py`: the asphalt painted into the ground photo and its road mask (`_road_segments`: by default, with
  `roads.ROADS_IN_GROUND`, the visible asphalt is this, not a mesh), the terrain's material under the wheels with the
  verge and the median's lawn (`paint_roads`), and the ramp gores' paving (`_gore_fills`).
- `rampmerges.ground`: the exit and entry lanes' paint (US), so a placed piece's exit lane goes on into its ramp.
- `deck_shift_fn`: a deck adds the same shift to its lane shifts.
- `grading`: the graded parts and the lines its abutments and chains read.
- `navgraph`'s lanes (the traffic AI).
- `signroads`' pavement rows (the signs' road context).

The raw feature geometry (`feats.lonlat`) stays untouched. So do the joints the kernels find from it (`road_tapers`,
`lane_shifts`), the connectors and the graph's topology.

## Formats

Planning output changes wherever placement is mapped: `plancache.PLANS_FORMAT` + 1, and the golden is re-recorded
(the bake fixture has no placement, so its heights stay the same). `roads.FACTS_FORMAT` is unchanged: the specs and
level ranges are untouched. No native change.

## Verification

- **Unit tests:** the three rules, direction, clamping, no lanes, links, the taper interplay (the placed edge
  continuous), the easing between parts, and no lane shift where a placement applies.
- **Tile test** (synthetic fixture: two carriageways, one with a placed 3-lane piece): no overlap, a continuous
  median barrier, edges continuous at both joints, the markings and AI lanes moved with the asphalt.
- **Offline probe** on the cached A93 cells: median modules continuous, the edge-to-edge gap >= 0.
- **In game:** a before / after screenshot at 49.00528, 12.06115, and the quality gate.
