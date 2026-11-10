# French motorway decision gantries

Previously, a single motorway exit normally received roadside boards at 500 m
and near the split. A paired overhead was only possible at 500 m when the
mainline had destination names. Weekly sign shards also discarded the mainline
reference when those names were absent. This left some junctions with only a
white roadside exit board and omitted the blue continuing-route board.

French motorway exits now use the shared overhead support planner at the 500 m
advance station and the existing decision station, when there is sufficient
mainline information or the exit board explicitly carries a motorway reference.
The continuing-route board and exit board retain separate labels and arrows.
Nearby branches share a gantry and retain their own exit directions. Small gore
markers remain roadside. A failed support plan retains the roadside information.

Mainline destination names come only from the mainline's posted source tags.
Without those names, an explicit current motorway reference can produce a blue
route-only board. An exit's destinations or destination:ref never supply the
continuing route. Updated shard builds retain the mainline highway/ref fields.
For existing shards, an optional lookup reads highway/ref for the exact OSM
mainline way IDs from the renderer's pinned Layercake source. This uses cached
range reads without requesting geometry, memoizes results, rejects conflicting
references and throttles failures. Unreachable or incomplete metadata preserves
existing signs. No destination names are guessed or copied from another road.

The structures are representative two-column gantries planned from generated
road geometry, not surveyed reproductions of Street View supports or locations.
Ground columns clear the pavement; bridge footprints are excluded and the
existing minimum six-metre underside clearance is retained. Exact potences,
lane-by-lane arrows and mixed green/white destination blocks are outside this
change. Regional activation and town-sign models are unchanged.

Validation: 99 Python tests passed across 11 relevant modules; one optional
pyosmium builder test was skipped because pyosmium is unavailable. Coverage
includes mainline/exit separation, route-only and missing-data cases, exact-ID
Parquet queries, ambiguous references, offline and changed-snapshot handling,
multi-branch arrows, support clearance, bridge exclusion, duplicate records,
tile ownership, French meshes and existing non-French layouts. No BeamNG runtime
is available here: test the A4 exit 18 and A355 exit 2 in the game before pushing.
