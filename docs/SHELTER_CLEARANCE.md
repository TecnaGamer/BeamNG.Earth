# Transit shelter evidence and full-body road clearance

Related: #804, #889. A `bus_stop` alone is weak evidence of a physical
shelter, and in some regions it is not even beside the road: the
Hautes-Alpes and Grenoble map nearly every stop as a `highway=bus_stop` +
`public_transport=stop_position` node on the carriageway, often on a
junction node, with no platform and no `shelter` tag (PR #869's review:
31 % of Risoul's shelters stood in a carriageway). The evidence levels:

| Evidence | Shelter |
| --- | --- |
| `shelter=no` | none |
| `shelter=yes` on a stop or bus platform, or `amenity=shelter` + `shelter_type=public_transport` | yes |
| untagged stop or bus platform beside the road | only where its whole body clears every road |
| untagged `public_transport=stop_position`, or an untagged `bus_stop` node on the carriageway | never (where the bus halts) |
| `shelter=yes` on a stop position | beside the road, unless a bus platform is mapped within 30 m: that one carries it |

Untagged platforms stay (the evidence-only rule dropped 74 % of Pontivy's
stops, 40 % of Manchester's), and the full-body check decides.

The pose follows #889's rules: the road whose **edge** is nearest (not its
centreline) gives the orientation, the signed face stands 1.2 m past that
kerb, and the base is the ground under the body's lowest corner. #889's
post-by-post push is replaced by the full-body check below: a shelter whose
body meets the tile's solved roads (wider than the mapped width where the
deck has a sidewalk) steps back 0.5, 1.0, 1.5, 2.0 m, then is left out.

Classification and nominal roadside placement run in Rust. Python parses
the source tags, packs the existing road segments and marshals the result;
there is no parallel Python placement algorithm. Rebuild the required native
module (API 170) with `python tools/build_native.py`. Platform binaries are
not included in this contribution.

After elevation sampling and coordinate/yaw rounding, the complete stock
shelter body is checked by the existing native `RoadClearance`. Both world
generation paths pass the tile's shared context, including shifted road
surfaces and stacked decks. A shelter that cannot clear another carriageway
is omitted. A fallback context supports callers supplying only road lines.
The shape envelope uses the stock shelter's documented bounds already
recorded in `shelters.py`: x Â±0.74 m, y -1.45..1.44 m, z -0.20..2.70 m.

Source points still own their shelter in a half-open tile box, with the
existing ID-derived side/yaw and source feature deduplication. Placement
changes invalidate generated content through the ordinary source fingerprint;
road facts and grading plans are unchanged.

## Validation

`test_shelters` covers explicit and missing tags, opposite roadside poses,
tile ownership, complete-body obstruction by crossing and parallel roads,
retention beside a clear road and below a high bridge. `test_furniture`
checks the shared native clearance consumer. Run:

```text
python tools/run_tests.py test_shelters test_furniture test_blockworker test_fast test_roadwidth test_revisit
```

The reported Otervegen location was inspected on aerial imagery as a rural
road with houses and trees. The behavior is a general data/clearance rule;
no location is special-cased. The reporter's four sites remain in-game
acceptance cases. Mapillary credentials and a running game were not
available in this environment, so visual/collision acceptance in BeamNG
has not been established by the synthetic regressions.

Local Windows Release validation: all 13 shelter tests and the five focused
modules above passed (68 additional tests). The first version's 1,000 seeded nominal poses matched the old Python placement; the
poses now differ on purpose (#889's 1.2 m kerb gap and nearest-edge rule).
The Rust classification unit test passed. The broader foundation run passed
42 of 44 Python modules; the cache symlink and process-lifecycle fixtures
failed identically on the unchanged base on this Windows installation.
All 97 offline Lua modules, both Node map-math suites, and the client/BeamMP
package builds passed. Linux execution and in-game acceptance remain open.
