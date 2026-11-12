# Opt-in Dutch Groenpol vehicle signals

Enable **Dutch Groenpol signals** in the Earth settings and restart. The setting
defaults to false. BGT is outside this integration. No installed game files or
user launchers are changed by this PR.

Models and textures derive from Sven's confirmed `Groenpol.fbx`, SHA256
`0c87eef5eea270f91807cabdaf9f37df3ebec64d115e6d99e6ecc28100effe77`.
The component/source-object/triangle provenance is bundled with the assets.
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
pack (about 46 MiB decoded). It is loaded only when the opt-in needs it, not by
the default stock signal path. See the adjacent `context_attribution.md` and
`context_provenance.json` for source URLs, queries, hashes and ODbL notice.
This pack does not contain the BGT road adapter, BGT polygons or the earlier
location-specific BGT pole overrides. Moving that independent context pack to
the public data host can reduce the initial mod download in a later change.

The client integration preserves current dev's independent scenery hiders and
native sign/road queues. The low-level tests exercise ownership, reloads,
rebasing, lane/head counts, mounting, headings, shield clearance and road feet.
These are offline/stubbed tests, not proof of visual correctness at every
junction. In-game placement review and performance measurement remain needed.
