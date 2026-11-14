# Native signal placement migration

This is an intermediate server API, not a completed replacement for the current
client planner. It is not enabled by a setting or installed into a live game by
the producer or tests. Do not remove the client pack until the complete assembly
transport and renderer have replaced it.

## Physical surface snapshot (earthfast 294)

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

## Mounted parts and far display

`NlSignalScene.assemblies` combines physical support selection with Rust socket
expansion using the authored component catalogue. A successful result contains
every finished part matrix, lane identity and repeat-head identity. Posts and
beam frames are independent of driver-facing head frames: mirroring the steel
does not turn the lamps away from the approaching lane. Original hanger tilt,
loose feet, per-lane arrows, straight beams and the left portal leg's actual
ground height are preserved. Eight-head portals are included in the tests.
Python only marshals arrays, catalogue data and context; it calculates no poses.

`tools/build_groenpol_forest_assets.py` reproducibly compiles all twelve supplied
vehicle variants into shared dark bodies and fixed red/amber/green optical
surfaces. It also compiles the steel and feet. It reuses existing DDS textures;
it does not make a texture or a unique 3D model for each placed lamp. Source
audit output and the server catalogue are separate from runtime asset output.
Pedestrian heads are excluded. These generated assets have not been added to
the runtime mod while the coordinated migration is still incomplete.

The new `nl_signal_assemblies` consumer validates identities, model roots and
finished matrices, retains accepted parts if a replacement fails, and reuses
unchanged parts. `farParts` selects a received fixed optical model from an
explicit existing controller state; it never guesses a phase. The lazy
`nl_signal_forest` backend uses the BeamNG editor datablock factory, shared
model types and one Forest per receiver. Its frame transform rebases the
existing Forest without rebuilding its beams. It creates no TSStatic objects.
Level teardown forgets native handles without dereferencing deleted objects.
These modules are not yet wired into the live protocol or enabled by settings.

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

The corrected primitive diagnostic was visually confirmed on Gridmap: all
three fixed lens colours render, and the 5x beam instance retains its
cross-section. This is proof of those two rendering behaviours, not a city
performance measurement. `tools/build_groenpol_native_demo.py` prepares a
second isolated diagnostic using actual native mounted-part output and the
new far consumer, with resource paths relocated to temporary files only.

## Work still required before switching runtime

- Shared supports and double shields, plus clearance conflicts between complete
  neighbouring assemblies. The native mounted-part API currently expands each
  approach independently; it does not yet coalesce adjacent approach supports.
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
