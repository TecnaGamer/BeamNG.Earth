# Nationwide highway shields

Prepared on upstream `dev`. Uses the current French, German, welcome/recreation,
railway, speed-sign and guide-board integrations.

## Runtime data

Normal streamed road geometry supplies junctions and OSM way IDs. The application
fetches `routes/manifest.json` from BeamNG.Earth-data through `pagesdata.Manifest`
and downloads its content-addressed gzip SQLite database through the shared
network fetcher. The manifest's HTTPS data host is preferred; the existing Pages
fallback handles mirror failures. Only US road-route identities and memberships
are downloaded, with no geometry or precomputed sign locations.

Before use, the reader checks format, filename, decompressed size, SHA-256,
SQLite integrity and query schema. Atomic writes publish verified databases to
`<raw_dir>/pagesdata/routes/<sha256>.sqlite`. The manifest refreshes daily;
failed database downloads retry after one hour. A verified cache works offline,
and a failed weekly update retains the last verified database, including after
restart. Changed memberships invalidate in-memory joins and candidate caches.
Neither the database nor any pinned manifest is committed or packaged.
`osmtags.RETRY_S` stays at 3600.

Only explicit relation memberships identify streamed routes. Class never assigns
a route number. Streamed one-way rules control physical travel because streamed
geometry can reverse the original OSM way; relation member roles remain available
in the index. Incomplete road context is retried. The existing bounded Layercake
fallback can use explicit way references when membership data is unavailable.

## Placement and rendering

Generated highway shields use 2,400-meter spacing. Candidates from neighboring
source cells participate before clipping to the requested tile, making duplicate
suppression independent of tile query order. Existing mapped signs are retained.
The existing pavement matching, bridge, building, terrain, one-way and ownership
checks still govern physical placement. Generation runs in the sign worker.

The configurable registry supplies standard designs for all 50 states and
selected specialty networks (including Texas FM/RM and Tennessee primary/secondary).
Concurrent networks remain distinct. Unavailable designs use an explicit labelled
fallback. Numberless templates and subset Roadgeek lettering produce cached DDS
faces shared by roadside markers and state/specialty insets on existing green
boards. Ordinary federal meshes and composite-label fallbacks are retained.
A four-entry background cache avoids repeated decoding without changing shared
artwork when a different route number is drawn.

## Sources, tests and limits

Artwork provenance, authors and checksums are in `server/data/shields/NOTICE.md`,
`artwork_sources.json` and `coverage.json`. Standard artwork is public domain
except the attributed Minnesota-wide derivative (CC BY-SA 4.0); Roadgeek is MIT.
Runtime membership metadata credits OpenStreetMap contributors under ODbL 1.0.
The weekly membership builder and its tests live in BeamNG.Earth-data.

The application tests cover downloads, verification, mirror fallback, offline
restart, failed updates, memberships/roles, all-state artwork, concurrent routes,
tile seams, placement and guide textures. Existing French/German and newer sign
regressions are included in validation. Source and BeamMP packages use
`python tools/package.py --beammp`; the native desktop app uses
`python tools/build_app.py` on its target OS.

Missing/stale OSM memberships, short approaches and clearance checks can omit
markers. Not every specialty network has exact artwork. Nationwide in-game
placement and performance have not been measured. The Wyoming station fixture
is test-only and excluded from player packages; review screenshots and extra
workflows are absent from this change.
