# Rural US-30 infrastructure

Draft scoped to roadside decisions in `server/roads.py`, based on upstream
`0bf942c22e1ff98ce994338668d5e7fa5b089254`. It does not change grading widths,
terrain support, banking, the bridge solver, collision meshes or vegetation.

The two US-30 primary carriageways near 41.71311,-105.87816 are mapped with
two lanes each. Their opposite pavement satisfies one-sided development sampling,
so the original classifier calls them urban. Paired primary carriageways now
need bilateral development, as trunk roads already do. Lane counts and mapped
widths survive pairing. Ordinary undivided town main streets retain their rule.

Near 41.60284,-105.72247, a 28.7 m primary link through the median was separately
classified urban and produced isolated sidewalk slabs. Short links inherit rural
context only if their actual endpoints meet distinct rural divided major roads.
Mapped sidewalk overrides remain authoritative, and connectivity is retained.

Divided primary embankments also triggered mountain-road dry-stone parapet
inference. That inference now excludes paired divided roads. Deep-cut retaining
walls and undivided-road drop protection retain their existing paths. Wall tests
are representative geometry; this PR's compact fixture contains actual road and
land-cover inputs, not elevation data or a live wall-object inventory.

The fixture `server/tests/fixtures/us30_rural_inputs.json` contains selected real
Overture 2026-09-23.1 roads.6 geometries/tags and the captured warped WorldCover
queries from read-only local caches, captured 2026-10-28. Source attribution:
Overture Maps Foundation, OpenStreetMap contributors and ESA WorldCover
v200/2021; see CREDITS.md. Coordinates and cover values are inputs, not solved
roadside output. Tests make no external geographic requests.

Automatic primary-road lamps are suppressed as a consequence of rural context.
Explicit OSM lighting overrides are implemented by the existing separate PR #404,
not duplicated here. This draft must be integration-tested with that PR before
claiming complete mapped-lighting behavior. Utility-pole rules are unchanged.

Automated validation: 185 tests passed, zero failed/skipped, across
test_us30_infrastructure, test_roads, test_furniture, test_osmtags, test_islands,
test_sidewalk_collision, test_roadwidth, test_roadmesh, test_bridge_clearance and
test_otto_bridge. Tests loaded roads/grading/roadmesh from this isolated upstream
tree, rather than the reporter's modified installation. Nine of the 16 new
checks fail with upstream's original roads.py; all pass with this patch.

No in-game visual or performance success is claimed. Review/test this draft
independently before merging. In-game acceptance should inspect both US-30
carriageways, the median opening near 41.6011833,-105.7206587, and the reported
wall area at 41.70776,-105.86927; compare mapped pedestrian infrastructure,
developed arterials, undivided mountain roads and genuine cut retaining walls.
