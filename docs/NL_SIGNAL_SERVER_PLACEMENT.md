# Native signal placement migration

This is an intermediate server API, not a completed replacement for the current
client planner. It is not enabled by a setting or installed into a live game by
the producer or tests. Do not remove the client pack until the complete assembly
transport and renderer have replaced it.

## Physical surface snapshot (earthfast 293)

`nlsignalassembly.make_scene` marshals the actual drawn road and raised walk
triangles, the decoded quantised terrain grid, its hole mask, and the rectangles
of completely received road tiles into `earthfast.NlSignalScene`. Fresh and
cached roads use the same float32 vertex positions. Coarse visual LODs and
hidden collision objects are excluded from cached mesh inputs.

The Rust index owns its snapshot and releases the Python interpreter lock while
building and searching it. Callers must rebuild it when the terrain, drawn
surfaces or anchor changes. A source DEM alone cannot substitute for the game's
quantised terrain or its holes. Callers pass the actual terrain hole mask as
`idx_ter == heightmap.TER_HOLE`.

The native planner verifies all incoming lane centres against carriageway
surfaces and searches safe feet on either side, including raised islands. Each
foot must fit its surface; a raised walk surface overrides the road skin below
it, but does not excuse a foot extending past its edge. Height layers distinguish
roads on bridges from roads and verge below. Columns and overhead spans are
checked against other road and walk decks. Terrain holes never provide ground.

One cantilever carries at most three lane heads. A portal has no arbitrary
six-head limit, but must have two clear feet and fit the configured 40 m span.
Alternatives include mirrored cantilevers, four-lane 3+1 arrangements and safe
standing heads under a bridge. Each successful result covers every requested
incoming lane once. The search can move the stop station within the caller's
bounded before/after allowance; it does not claim those are surveyed stop lines.

Planning waits for a complete local candidate-search snapshot. Missing surface
or terrain tiles return a retryable result instead of a permanent omission or a
different mast position every time another tile arrives. Known but unsafe
geometry returns a distinct rejection. The caller must retain stock signals
until a complete replacement assembly has actually been accepted.

`nl_signal_lens_mm` selects 200 mm only for an entirely confirmed built-up
junction. Mixed, outside or unknown context and motorway exits select 300 mm.

## Renderer experiment

`tools/build_groenpol_forest_probe.py` prepares a self-contained diagnostic from
the supplied Groenpol geometry. It compiles shared body, optical channel, post
and beam assets. It does not install a mod. Materials use fixed optical emission,
not mutable per-instance diffuse or palette colours. The diagnostic stays outside
the runtime mod and retains the model asset licence notice.

`tools/groenpol_forest_probe.lua` can be staged with those assets in the game's
temporary directory. Run it on a stock map such as Gridmap, with Earth streaming
inactive; the probe refuses the Earth level. Running it in GE-Lua creates three diagnostic posts and
two differently stretched instances of one shared beam in an owned Forest.
It never saves the level. `bneNlForestProbe.cleanup()` deletes its own objects;
running the probe again replaces the previous diagnostic.

The offline Lua test checks shared models, matrix columns, repeat-run cleanup,
and no TSStatic creation. It cannot verify BeamNG's proprietary renderer.
Manual confirmation is required for fixed emissive Forest lenses and nonuniform
beam scaling that keeps the cross-section unchanged. A successful script call
alone is not proof that either renders correctly.

The first live attempt exposed BeamNG's factory distinction: `TSForestItemData`
must be created through `worldEditorCppApi.createObject`, as in Earth's tree
renderer. The diagnostic now prepares all six datablocks before creating a
Forest, and the regression test rejects the generic factory for that class.
A later native crash was logged in Earth's existing tree `removeItem` path;
its relationship to the failed diagnostic is unproven. The next diagnostic is
isolated on a stock map rather than run alongside active Earth streaming.

## Work still required before switching runtime

- Native expansion into authored mounting feet, independently aimed head/socket
  transforms, shared supports and double shields, plus conflicts between complete
  assemblies. Numeric support selection alone does not implement these.
- Server integration using existing road and graph inputs; assembly ownership,
  transport and invalidation across tile arrival and anchor rebases.
- A small client consumer and verified distant Forest representation, followed
  by removal of client road-shape rereading, AI graph augmentation and the
  bundled context pack.
- Publication of reproducible context tiles on the maintainer's data host and
  relocation of the public-data build workflow to that repository.
- Rotterdam or Amsterdam on/off frame-time and memory measurements, including
  stock-mode isolation and repeated travelling/rebasing.

See `NL_SIGNAL_CONTEXT_DATA.md` for public source provenance and data-host status.
