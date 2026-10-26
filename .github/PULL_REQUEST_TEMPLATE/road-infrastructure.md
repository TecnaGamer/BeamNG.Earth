## Summary

Migrate the modified 1.6.2 road infrastructure to 1.6.3 while retaining the newer road generation and cache behavior. Restore zipper merges, extended ramp tapers, asymmetric shoulders, travel-lane dimensions and driver-left yellow markings. Apply the project's regional paint dimensions, including the confirmed final Kentucky entry.

Ramp lanes are now 4.8 m wide. Unmapped one-way highways default to two lanes; resolved OSM counts feed both rendering and AI. Roundabouts use 9 m circulating lanes plus edge allowances, with wider short entry/exit roads and sidewalk, prop, vegetation and parking clearance.

Recognize Public Square's untagged ring, including its short straight connecting arcs. Replace its narrow divided-road treatment with a 10 m circulating carriageway, approximately 6.6–7 m approaches and a narrow 0.4 m central curb. Omit guessed roundabout/approach sidewalks and require sustained development for automatic rural sidewalks; preserve explicit mapped sidewalks and driveway access.

Widen paved two-way, two-lane rural roads globally to at least 4.25 m per travel lane plus 1.25 m shoulders: 11 m pavement. Keep this minimum through mapped/building width overrides and use the same clearance on two-lane bridge/tunnel profiles. Resolve eligible missing counts to two so pavement, edge lines and AI agree. Preserve larger widths, urban streets, one-way roads, ramps and roundabouts. Repair isolated narrow rural sections only when both through-road continuations have two lanes; stop one-ended single-lane guesses from spreading through untagged roads.

Match bridge width and edge margins to the actual approaches, with smooth width transitions where needed. Add rounded intersection pavement and curved edge lines without requiring sidewalks; share the new pavement with grading, material/photo masks and vegetation/prop clearance.

Give A Collins Road in Logan County, Kentucky its requested gravel textures and GRAVEL tire physics. Remove its generated utility poles and wires using a road-name/location override; preserve neighboring roads.

Include the packaged source fingerprint in generated-world cache keys. Previously, different custom EXEs reused the same cache identity and could display old road meshes and grading plans. Changed builds now regenerate derived geometry automatically, preserving raw downloads and saved positions.

Fix custom EXE HTTPS initialization by bundling the build runtime's required SSL certificate-policy file. Missing policy data previously caused all network requests to fail before connecting.

## Scope and base

- Branch: `port/road-infrastructure-1.6.3`
- Base: `ed2fd3f`, `port/highway-signs-1.6.3`
- Details and dimension table: [ROAD_INFRASTRUCTURE_CHANGELOG.md](../../ROAD_INFRASTRUCTURE_CHANGELOG.md)
- Road data cache version: 7; incomplete OSM data retries and is excluded from durable generated caches.
- Existing signs, tree-density and per-player autosave changes remain inherited from the base.

## Validation

- [x] 43 Python test modules: 683 passed, 3 live-data tests skipped.
- [x] Test global rural widening, left/right-hand traffic, width overrides and non-target exceptions; check mesh/photo/material pavement, paint, grading, AI and bridge/tunnel profiles. Cached West Maumee Street's mapped two-lane profile grows from 9.5 m to 11 m pavement.
- [x] Verify actual dash lengths/gaps/widths, reversed road directions, bridge/tunnel paint and adjacent merge clearance.
- [x] Verify mapped lanes, relation-derived roads, fallback retry, roundabout sidewalk/prop/vegetation clearance and ordinary city-block preservation.
- [x] Test the real Public Square geometry offline: all eight arcs and eight approaches, monument/island clearance, continuous directed AI circulation and entry/exit connections. Check mapped sidewalk clearance and suppression of untagged sidewalk slabs.
- [x] Verify rural inference against clustered farm vertices and preserve explicit sidewalk tags. Test frozen cache invalidation across build fingerprints and unchanged-build reuse.
- [x] Check the 1.6.3 bridge, tunnel, railway, sidewalk, cache, gas-station and highway-sign regressions.
- [x] Check approach/deck widths, parapets/girders, paint and AI across tile seams; verify rural corner material/photo/mesh/grade and clearance.
- [x] Test two-ended rural continuity and the cached Chandlers Road inference regression; preserve explicit tags and unrelated branches.
- [x] Test the complete cached A Collins Road, source splits, geographic scoping, gravel physics/textures and utility suppression.
- [x] Five Lua test files passed (203 assertions), including gravel terrain-layer loading and existing terrain/road rendering contracts.
- [x] `git diff --check`.
- [x] Built the 74 MB EXE; launcher/server CLI smoke tests exited 0. Verified bundled modules/assets and the source fingerprint; checksum is in the changelog.
- [x] Execute the EXE's archived cache modules with two build fingerprints: cache identities differ, and neither reuses the old custom-build identity.
- [x] HTTPS packaging follow-up: 65 focused tests passed, 11 Linux-only skips; verified archived SSL initialization and live frozen-runtime responses from all five reported hosts.
- [ ] Drive bridge approaches, rounded junctions, rural lane transitions, A Collins Road, highway merges and roundabouts in BeamNG; attach coordinates/screenshots for any remaining geometry issues.

## Review notes

The road cache rebuilds under version 7. The EXE is the requested player build; no standalone server ZIP is included. Geometric roundabout fallback uses curved source arcs when tags are missing. Odd two-way lane totals use a balanced directional split until directional tags are available. No in-game visual verification has been completed for this branch.
