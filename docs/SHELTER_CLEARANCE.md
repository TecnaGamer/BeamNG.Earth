# Explicit transit shelters and full-body road clearance

Related: #804. A `bus_stop` is not evidence of a physical shelter. A stop
now needs a positive `shelter` value; explicitly mapped public-transport
`amenity=shelter` features still qualify. Negative shelter tags veto both.

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
modules above passed (68 additional tests). One thousand seeded nominal poses
matched the baseline Python placements after the existing output rounding.
The Rust classification unit test passed. The broader foundation run passed
42 of 44 Python modules; the cache symlink and process-lifecycle fixtures
failed identically on the unchanged base on this Windows installation.
All 97 offline Lua modules, both Node map-math suites, and the client/BeamMP
package builds passed. Linux execution and in-game acceptance remain open.
