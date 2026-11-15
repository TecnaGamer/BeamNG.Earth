# Public Dutch signal-context tiles

This public context feeds the live optional native Groenpol pipeline. The old
client pack and planner are removed. Unpublished data keeps stock signals available.

## Reproduce

The offline builder needs Python, pyproj and shapely. The runtime reader needs
neither GIS dependency. Supply the official WKD `WKD_KOM_BORDEN.gpkg` and saved
Overpass JSON results as explicit arguments:

```
python tools/build_nl_signal_context.py WKD_KOM_BORDEN.gpkg turns.json signals.json output/nl-signal-context
```

`TURN_QUERY` and `SIGNAL_QUERY` in the producer are reproducible example queries.
The second query retains parent road ways so the builder can exclude bicycle
and pedestrian-only signals. A node-only export is accepted only with an
adjacent `<input>.source.json` containing the matching SHA-256 and
`filter: nodes_on_motor_vehicle_road_ways`. That source note must describe the
actual query; it is not a claim that a new unverified node export is car-only.
Existing saved exports keep their OSM base timestamp. Suggested queries are
labelled separately from verified source-query provenance.

WKD input must declare EPSG:28992. Simplification is one metre in that metric
CRS, then coordinates are transformed to EPSG:4326. Source identifiers retain
their full integer precision as strings. This is spatial context, not proof of
surveyed stop lines or an NWB-to-Overture identity match.

Output is an immutable z14 dataset: `manifest.json` and `x/y.json.gz` files.
Records are sorted, duplicates collapse, conflicting identities fail the build,
and gzip timestamps are fixed. The manifest records source hashes, individual
tile hashes and byte counts, producer revision and an immutable build ID.
A 45 Mercator-metre tile halo supports the existing local match radii near
tile boundaries. A server query is limited to 64 tiles and keeps at most 16
decoded shards; ordinary manifest refresh, persistent cache and failure backoff
come from `railtags.Shards`.

## Rights and deployment

OSM data: OpenStreetMap contributors, ODbL 1.0. The combined derivative dataset
is labelled ODbL 1.0, with separate Rijkswaterstaat / NDW WKD attribution. Retain
the WKD source distribution's terms and provenance when publishing a build.
No third-party photographs, sign faces or private stores are included.

Deployment belongs in BeamNG.Earth-data under `nl-signal-context`, through its
ordinary manifest and shard host. Publication is a maintainer action. The
reader is disabled by default, performs no requests while disabled and returns
no context when the dataset is unpublished. It does not suppress stock objects.

`nlsignalcontext.match` now marshals local ENU coordinates into the native
`nl_signal_context_match` kernel (earthfast 302). Direction and lane counts must
agree before turn arrows are returned; equally close conflicting arrows remain
unknown. Missing WKD data remains unknown unless the caller explicitly proves
complete WKD coverage. Matching feeds the live native junction planner.

The native physical support and shared-junction solver checks actual drawn surfaces
and quantised terrain. Its live transport and ownership contract is documented in
[NL_SIGNAL_SERVER_PLACEMENT.md](NL_SIGNAL_SERVER_PLACEMENT.md).
The companion data workflow fetches pinned WKD and attributed Overpass sources,
builds deterministic shards and validates them before the existing publication
and mirror job. Raw national sources never enter the mod.
