# Opt-in Dutch Groenpol vehicle signals

Enable **Dutch Groenpol signals** in the Earth settings and restart. The setting
defaults to false. BGT is outside this integration. No installed game files or
user launchers are changed by this PR.
The stock path does not load the Dutch planner, country outline, component
catalogue or surface reader. Ground re-fit work and Dutch static-frame refreshes
are gated by the setting. Unchanged suppression cells do not rescan signal tiles.

Models and textures derive from Sven's confirmed `Groenpol.fbx`, SHA256
`0c87eef5eea270f91807cabdaf9f37df3ebec64d115e6d99e6ecc28100effe77`.
The component/source-object/triangle provenance is retained in `docs/assets/groenpol`,
outside the runtime mod. The runtime component catalogue remains in its required Lua module.
Model/texture author and licensor: Sven / svenegaming-glitch, **CC BY-SA 4.0**;
see [the asset licence notice](GROENPOL_ASSET_LICENSE.md). Derived assets retain
this licence independently of the project code and map-data licences.
These are the contributor's supplied signal assets, not the rejected storefront
sign faces. Signal heads reuse the engine's existing light controllers.

The existing lane-aware construction includes separate posts, independently
aimed heads, original mounting feet, shared posts with separate controllers,
200/300 mm lenses and turn arrows. Through lanes keep full round lenses.
Mixed urban/rural junctions and motorway exits use 300 mm. Dutch signals go from
red directly to green. Cantilever arms carry at most three overhead lane heads;
wide approaches may use portals, mirrored cantilevers or a 3+1 arrangement.
Placement checks use the actual drawn road/walk meshes and terrain; unsafe
positions are omitted. A successful owned assembly suppresses its old prop
locally, rather than deleting Dutch props country-wide.

WKD built-up sections and OSM lane/signal context are included as a dated offline
pack (about 46 MiB on disk). The opt-in reader uses at most eight 64 KiB raw
blocks plus a separate 16-tile decoded cache, rather than loading the national
payload into one Lua string. Signal-only pointer tables avoid reading road
records when finding nearby signal nodes. Regenerate these deterministically
with `python tools/build_nl_signal_index.py`. The default stock path never opens
the pack. See the adjacent `context_attribution.md` and
`context_provenance.json` for source URLs, queries, hashes and ODbL notice.
This pack does not contain the BGT road adapter, BGT polygons or the earlier
location-specific BGT pole overrides. The review requires moving this pack to the public data host and planning to
native server code. Producer/reader preparation is described in
[NL_SIGNAL_CONTEXT_DATA.md](NL_SIGNAL_CONTEXT_DATA.md); that coordinated runtime
switch is still outstanding.

The client integration preserves current dev's independent scenery hiders and
native sign/road queues. The low-level tests exercise ownership, reloads,
rebasing, lane/head counts, mounting, headings, shield clearance and road feet.
These are offline/stubbed tests, not proof of visual correctness at every
junction. In-game placement review and performance measurement remain needed.
Runtime textures use DDS, avoiding the engine's PNG cooking path on current dev.
