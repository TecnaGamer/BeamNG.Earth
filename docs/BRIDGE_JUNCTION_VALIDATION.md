# Bridge junction collision correction

Status: **offline regression tests passed; physical driving in BeamNG remains unverified**.

At the captured Swiss fork near 46.66729 N, 7.72157 E, independently sloped bridge slabs and junction pads overlap at different heights. The correction gives eligible connected short bridge parts, their markings and pads a shared triangulated height surface. A pad at a matching ground-road endpoint is cut out of the existing road carriageway. Separate stacked levels remain separate, and bridge collision remains enabled.

Complete profile metadata survives clipping and anchor transforms. Short-branch context includes neighboring source cells; eligibility checks use that same context so a sidewalk, width taper, lane shift, long profile or ambiguous multipart arm excludes the whole fork. Unsupported long and multipart profiles contribute only their endpoints for this check. Missing required source context fails the tile build instead of emitting a partial fork. Existing geometry remains in use for excluded arms.

## Reproducible offline replay

Baseline is upstream DEV commit `7198d96e083dc08ccbe126a5afe57ee05aeb03d3`. The compact fixture is described in [bridge_fork_swiss.md](../server/tests/fixtures/bridge_fork_swiss.md). Triangle intersections are sampled on road centres and tyre tracks.

| Metric | DEV baseline | With correction |
|---|---:|---:|
| Maximum overlap height spread | 0.360426 m | 2.27e-13 m |
| Samples over 1 cm overlap | 426 / 510 | 0 / 510 |
| Samples with bridge collision | 510 / 510 | 510 / 510 |
| Wall above driving surface on tracks | 0 m | 0 m |
| Vertices | 2,117 | 5,072 |
| Triangles | 1,947 | 6,883 |
| Mesh construction median, 11 warm runs | 11.07 ms | 81.03 ms |

Timings are local observations and vary by machine. The added geometry and construction cost occur during tile preparation; this is not a loading-time or frame-rate improvement claim.

```text
python server/tests/bench_bridge_junction.py --baseline-ref 7198d96e083dc08ccbe126a5afe57ee05aeb03d3
```

The combined 18-module road and bridge suite ran **354 tests: 353 passed, one skipped** because LuaJIT was unavailable. It includes the fixture, ground approach, stacked levels, source-cell boundaries, unsupported arms, adjacent tiles, rebase, sidewalks and width behavior. A separate focused run of 20 tests includes the subsequently added stacked-level and multipart ordering regression; it overlaps the broad suite. The overlap and ground-approach regression tests also failed with the original DEV mesh before the correction. Run with `server` and `server/tests` on `PYTHONPATH`:

```text
python -m unittest test_bridge_junction_surface test_deckpad_levels test_roadmesh test_bridgeprofile test_bridge_clearance test_bridgetypes test_bridgelook test_otto_bridge test_shortdecks test_roads test_geotiles test_deckjoins test_roadwidth test_ondeck test_sidewalk_collision test_bridge_scenarios test_under_deck test_tram_bridges
```

Eligibility is bounded to 3–8 incident short parts of at most 60 m, with matching endpoints and complete, unambiguous profiles. Unsupported sidewalks, tapers and shifts, long spans, two-way bends, unrelated crossings, larger networks and old inputs without metadata retain existing behavior. This does not solve every terrain or bridge elevation issue. There are no native module changes. The source fingerprint must be updated in any packaged build so old cached road content is invalidated.

Before calling this gameplay-verified, drive each Swiss branch and its ground approach in both directions at low and normal speeds, including a tile boundary and rebase. Check wheel contact, visual/collision alignment and stacked bridge guardrails. The offline fixture does not establish vehicle behavior or diagnose native crashes.
