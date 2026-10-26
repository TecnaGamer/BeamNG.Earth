## Summary

Migrate the modified 1.6.2 road infrastructure to 1.6.3 while retaining the newer road generation and cache behavior. Restore zipper merges, extended ramp tapers, asymmetric shoulders, travel-lane dimensions and driver-left yellow markings. Apply the project's regional paint dimensions, including the confirmed final Kentucky entry.

Ramp lanes are now 4.8 m wide. Unmapped one-way highways default to two lanes; resolved OSM counts feed both rendering and AI. Roundabouts use 9 m circulating lanes plus edge allowances, with wider short entry/exit roads and sidewalk, prop, vegetation and parking clearance.

Recognize Public Square's untagged ring, including its short straight connecting arcs. Replace its narrow divided-road treatment with a 10 m circulating carriageway, approximately 6.6–7 m approaches and a narrow 0.4 m central curb. Omit guessed roundabout/approach sidewalks and require sustained development for automatic rural sidewalks; preserve explicit mapped sidewalks and driveway access.

Widen paved two-way, two-lane rural roads globally to at least 4.25 m per travel lane plus 1.25 m shoulders: 11 m pavement. Keep this minimum through mapped/building width overrides and use the same clearance on two-lane bridge/tunnel profiles. Resolve eligible missing counts to two so pavement, edge lines and AI agree. Preserve larger widths, urban streets, one-way roads, ramps and roundabouts. Repair isolated narrow rural sections only when both through-road continuations have two lanes; stop one-ended single-lane guesses from spreading through untagged roads.

Match bridge width and edge margins to the actual approaches, with smooth width transitions where needed. Add rounded intersection pavement and curved edge lines without requiring sidewalks; share the new pavement with grading, material/photo masks and vegetation/prop clearance.

Keep short overpass decks on their approach grade when existing clearance permits, without adding a synthetic crest. Extend visible approach grading through L1 and use adaptive 2 m ground sampling around bridge ends to prevent coarse terrain triangles from leaving a step below the deck. Preserve required crossing clearance and real terrain obstructions. Regressions include both carriageways at I-165 Exit 3 in Bowling Green, Kentucky.

Include the user's current worker-count, budget and priority settings; actual concurrency remains subject to resource-budget and command-line overrides.

Reduce the road-loading stalls caused by repeated roundabout clearance checks during intersection construction. Skip distant corridors, reuse prepared segments, batch nearby projections, and share cached corner geometry/geographic polygons between concurrent requests. The cached Exit 3 road-photo benchmark improves from 6.64 s to 0.78 s median with pixel-identical output in three comparisons; live driving verification remains pending.

Replace quadratic junction-node comparisons with a spatial search that preserves pairing and merge order. Against the first faster-loading implementation, cached road-photo generation improves from 0.89 s to 0.68 s near the interchange and from 4.36 s to 2.19 s in the denser northbound area, with identical pixels. Add stage timings for road tiles exceeding 10 seconds; the reported 91-second worker stalls still need live verification.

Fix three-to-two-to-three lane gaps caused by inconsistent cached copies of the same highway segment. Recover exact-feature/source-revision lane evidence using bounded local reads, and bridge missing/inferred gaps only with matching three-lane continuations at both ends. All six cached copies of the reported I-65 North segment near Plum Springs now retain three lanes and 18 m pavement. Preserve explicit lane drops, ramps and ambiguous topology; share corrected counts across rendering, grading and AI.

Give A Collins Road in Logan County, Kentucky gray loose gravel with two pale cream compacted wheel tracks, matching the supplied real-life reference. Use stock gravel/worn-track textures with feathered edges and continuous texture placement through render seams and wall cuts. Keep GRAVEL tire physics, the existing road width and matching distant imagery. Remove its generated utility poles and wires using a road-name/location override; preserve neighboring roads.

Include the packaged source fingerprint in generated-world cache keys. Previously, different custom EXEs reused the same cache identity and could display old road meshes and grading plans. Changed builds now regenerate derived geometry automatically, preserving raw downloads and saved positions.

Fix custom EXE HTTPS initialization by bundling the build runtime's required SSL certificate-policy file. Missing policy data previously caused all network requests to fail before connecting.

## Scope and base

- Branch: `port/road-infrastructure-1.6.3`
- Base: `ed2fd3f`, `port/highway-signs-1.6.3`
- Details and dimension table: [ROAD_INFRASTRUCTURE_CHANGELOG.md](../../ROAD_INFRASTRUCTURE_CHANGELOG.md)
- Road data cache version: 7; incomplete OSM data retries and is excluded from durable generated caches.
- Existing signs, tree-density and per-player autosave changes remain inherited from the base.

## Validation

- [x] Verify optimized clearance against the previous calculation, including strict edges, vertical separation, negative margins and degenerate segments. Check shared concurrent corner builds, retry after failure, invalidation and bounded LRU reuse.

- [x] 51 Python test modules: 798 passed, 4 skipped (three live-data checks and one existing protocol constant check).
- [x] Verify spatial junction pairs/order against the original calculation and distant-node rejection; run both cached interchange/northbound comparisons with pixel-identical output.
- [x] Verify all six cached Plum Springs copies and ten highway-continuity regressions, including source refresh, cache-context retry, bounded reads, paint/AI/deck agreement and intentional lane-drop preservation.
- [x] Check flat/sloping overpasses, cached Exit 3 profiles, clearance/terrain exceptions, L1 grading and fine approach meshes. Offline Exit 3 deck-to-ground separation is 0.025–0.049 m at sampled abutment points; in-game verification remains pending.
- [x] Test global rural widening, left/right-hand traffic, width overrides and non-target exceptions; check mesh/photo/material pavement, paint, grading, AI and bridge/tunnel profiles. Cached West Maumee Street's mapped two-lane profile grows from 9.5 m to 11 m pavement.
- [x] Verify actual dash lengths/gaps/widths, reversed road directions, bridge/tunnel paint and adjacent merge clearance.
- [x] Verify mapped lanes, relation-derived roads, fallback retry, roundabout sidewalk/prop/vegetation clearance and ordinary city-block preservation.
- [x] Test the real Public Square geometry offline: all eight arcs and eight approaches, monument/island clearance, continuous directed AI circulation and entry/exit connections. Check mapped sidewalk clearance and suppression of untagged sidewalk slabs.
- [x] Verify rural inference against clustered farm vertices and preserve explicit sidewalk tags. Test frozen cache invalidation across build fingerprints and unchanged-build reuse.
- [x] Check the 1.6.3 bridge, tunnel, railway, sidewalk, cache, gas-station and highway-sign regressions.
- [x] Check approach/deck widths, parapets/girders, paint and AI across tile seams; verify rural corner material/photo/mesh/grade and clearance.
- [x] Test two-ended rural continuity and the cached Chandlers Road inference regression; preserve explicit tags and unrelated branches.
- [x] Test the complete cached A Collins Road, source splits, geographic scoping, gravel physics/textures and utility suppression.
- [x] Check gray stone and two pale tracks in near/far appearance, texture UV continuity through render seams and wall cuts, and unchanged neighboring gravel roads. Inspect an offline stock-material composite and verify texture references against installed game assets.
- [x] Five Lua test files passed (203 assertions), including gravel terrain-layer loading and existing terrain/road rendering contracts.
- [x] `git diff --check`.
- [x] Built the 74 MB EXE; launcher/server CLI smoke tests exited 0. Verified bundled modules/assets and the source fingerprint; checksum is in the changelog.
- [x] Execute the EXE's archived cache modules with two build fingerprints: cache identities differ, and neither reuses the old custom-build identity.
- [x] HTTPS packaging follow-up: 65 focused tests passed, 11 Linux-only skips; verified archived SSL initialization and live frozen-runtime responses from all five reported hosts.
- [ ] Drive bridge approaches, rounded junctions, rural lane transitions, A Collins Road, highway merges and roundabouts in BeamNG; attach coordinates/screenshots for any remaining geometry issues.

## Review notes

The road cache rebuilds under version 7. The EXE is the requested player build; no standalone server ZIP is included. Geometric roundabout fallback uses curved source arcs when tags are missing. Odd two-way lane totals use a balanced directional split until directional tags are available. No in-game visual verification has been completed for this branch.
