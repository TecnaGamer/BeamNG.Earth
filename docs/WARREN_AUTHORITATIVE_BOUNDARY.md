# Warren authoritative OSM boundary

The confirmed selector is **relation/252043**, with `type=boundary`, `boundary=border_zone`, and `name=Francis E. Warren Air Force Base`. The OSM relation display label "Boundary Francis E. Warren Air Force Base" identified it. The original phrase "outer boundry" is not a published tag in the downloaded source.

The approved snapshot comes directly from the current OSM API, relation version **17**, changeset **191629546**, timestamp **2026-11-08T03:07:25Z**. Overture release lag does not control this route. Raw source and machine-readable provenance are in `probes/warren_authoritative_relation_252043.osm` and `probes/warren_authoritative_review/source_audit.json`.

## Route selection

Only outer member ways of this relation supply Warren's main fence coordinates. All original vertices remain; ways connect at exact shared endpoints. There is no military property union, roadside offset, automatic routing, gap chord, or building detour for this main route. The previous Happy Jack constraint and Warren public-exclusion/override routes do not move this source. Other facilities retain their normal routing.

The 11 members are ways 462694600, 462694597, 462694592, 40856809, 40856783, 40856781, 462694617, 468200954, 468200956, 1576457908, and 468200979. Relation 21918541 and way 1576457907 describe separate military land-use geometry and are excluded from this main route.

The current source has **178 unique segments in two open chains and four degree-one endpoints**. These gaps are reported as missing source connectivity and remain open; they are not assumed to be entrances. See `probes/warren_authoritative_review/source_topology.png` and `approved_routes.geojson`.

## Placement and duplicates

Mapped gates still create openings. Existing terrain sampling and final road, bridge, rail and building clearance checks remain active. They may omit unsafe panels, with diagnostics, but cannot relocate the authoritative line. Source completeness and rendered completeness are distinct; an open source relation cannot produce a verified closed perimeter.

Source IDs and exact overlapping legacy chain-link panels are suppressed. The numerical overlap corridor is 5 mm; independent parallel fences and crossing fences remain. Independent secured compounds remain available. Connected paths retain global phase and deterministic tile ownership. Stock fence rendering and collision behavior are unchanged.

## Freshness and activation

Refresh only the approved source snapshot from the official OSM API:

```powershell
.venv\Scripts\python.exe tools/refresh_warren_boundary.py
```

The tool validates the relation identity and complete member geometry before atomic replacement. Invalid downloads retain the previous snapshot. It does not restart the server or purge caches. A changed snapshot adds a content signature to affected geographic tile keys, covering both old and new bounds; unrelated geographic tiles do not acquire that signature. A missing approved source withholds Warren's main route instead of falling back to old property outlines.

For an external persistent snapshot, set `BNE_WARREN_BOUNDARY_FILE` before starting the server, and pass the same location with `--output` when refreshing. The default snapshot is bundled by the application build.

**The running server/game has not been restarted or modified.** Source code changes need a user-controlled server restart and client reconnect/reload to activate. No in-game validation is claimed. Isolated tests and the source audit do not prove the active game's current visuals.

## Verification

Run `probes/test_warren_authoritative_suite.py` with the existing test dependencies. Results are saved to `probes/warren_authoritative_review/test_results.json`. Focused tests cover the confirmed selector, exact vertices, reversed and duplicate members, open gaps, exclusivity, missing-source behavior, independent enclosures, tile ownership, legacy duplicates, scoped freshness, and refresh rejection. Historical v6/v7 fixtures explicitly disable the new Warren policy to keep testing their original generic routing behavior independently.

Validation result: **748 tests run, 746 passed, two external-data tests skipped, zero failures/errors**. Python compilation and refresh CLI help also passed. The west-side source replay at 41.17607, -104.88373 produced 119 panels, all owned by the approved authoritative route. The source audit image was visually inspected.

Changed production files: `server/authoritativeperimeters.py` (new), `server/warren_outer_boundary.json` (new), `server/facilityperimeters.py`, `server/fencenetwork.py`, `server/perimeters.py`, `server/fences.py`, `server/blockworker.py`, `tools/refresh_warren_boundary.py` (new), and `tools/build_app.py`. Regression fixtures changed in `server/tests/test_fence_routing.py`, `test_fence_overhaul.py`, and `test_fence_regression.py`; new coverage is in `test_authoritative_perimeters.py`. New review scripts, raw source captures and results are under `probes`; this document supersedes Warren source-selection guidance in `FENCE_OVERHAUL_V7.md`.
