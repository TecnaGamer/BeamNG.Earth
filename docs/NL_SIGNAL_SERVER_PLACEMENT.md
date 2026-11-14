# Optional native Dutch vehicle signals

The `dutchSignals` setting is off by default. Its server worker, context reader,
assembly consumer and Forest backend load lazily. Stock mode does not parse a
country outline, refill fitting queues or refresh signal trees every frame.
The old client planner, cdae reads, AI graph augmentation and 46 MiB pack are removed.

## Live pipeline (earthfast 300)

The server captures a ready graph serial, exact road/skin versions, quantised
terrain and material holes. Public z14 WKD/OSM shards provide motor-road signal
points and lane context. Rust matches these to directed incoming graph arms;
ambiguous matches are refused. No source evidence means no invented junction.

Rust checks actual road/walk triangles, height layers, safe feet, columns and
span clearance. Exact convex geotile coverage handles rotated seams. Missing
neighbours retry rather than becoming safe ground. Mirrored cantilevers,
portals, 3+1 layouts and standing heads are alternatives. Portals have no
six-head count limit within their physical 40 m span limit. Every accepted
approach covers all requested lanes once.

A whole junction is solved together. Nearby feet can be replanned onto a
verified common foot. Authored quarter-turn mounts, double shields and bare
housings are selected by fit; shields stay wherever they fit. Cross-approach
steel/head collisions trigger bounded upstream station searches before rejection.
Accepted neighbouring assemblies participate in clearance checks.

Stop stations are inferred from graph/mapped signal context, not surveyed stop
lines. Upstream shifts update controller stopping coordinates consistently.
Entirely confirmed built-up junctions may use 200 mm; mixed, outside or unknown
context and motorway exits use 300 mm. Shared junctions use compatible sizes.

## Transport and rendering

Each connection writes its own augmented graph and complete native assembly
packet. Stale workers cannot gain ownership. Missing surfaces retry; an unchanged
previous complete junction may remain pending. Rejected owners keep stock heads.

The mod validates finished matrices and waits for the exact committed terrain
and visible road/sidewalk versions. Stock heads detach only after every new
item exists, followed by an exact epoch/revision acknowledgement. It performs
no geographic placement.

Shared Forest items render all steel, bodies, feet and optical faces, including
far assemblies. The compiler emits 89 reusable meshes (438,923 bytes together),
reusing existing DDS textures. There are no per-lamp models or distant TSStatic
assemblies. Runtime catalogue and audit metadata live outside the mod.

Existing approach controllers supply lamp phases; only optical faces change.
Unchanged geometry and rebases do not rebuild booms. Unknown phases render dark.
Lane arrows follow the existing approach phase: independent protected-turn
programmes and surveyed Dutch phasing are not synthesised. Dutch red-to-green
omits red+amber. Authored sockets remain, with the requested 5 cm post extension
and 5 cm additional low-bracket clearance. Beams remain straight and head
bearings independent of the steel.

## Data and verification

The companion BeamNG.Earth-data workflow owns reproducible WKD/Overpass fetching,
derived shards and optional publication. Without publication, stock signals
remain available. Context is ODbL 1.0; source hashes/queries and attribution
accompany the data. Sven (svenegaming-glitch) made the Groenpol models and offers
them under CC BY-SA 4.0. CREDITS.md includes PDOK/Kadaster.

Native tests cover geometry, shared feet, conflicting arms, relocation, rotated
coverage and deck layers. Server tests cover real input contracts, graph/context
integration and stale-generation ownership. Lua tests cover off-mode laziness,
surface readiness, Forest reuse and optical-only phase changes. Earlier Gridmap
component probes were visually confirmed by the user.

The actual Rotterdam/Amsterdam on/off frame-time and process-memory comparison
is still pending: the user is testing the normal game and requested the measurement
later. Offline/stubbed timings are not substituted. Public data deployment is a
maintainer action.
