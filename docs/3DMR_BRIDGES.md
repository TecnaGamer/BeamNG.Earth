# 3DMR bridge imports

Bridge uploads use the same GLB visual mesh and materials as other 3DMR models.
They do not need a simplified visual copy or extra geometry to trigger detection.

The importer recognises `bridge=*` (except `no`, `false`, `0`, `none`, `null`),
`man_made=bridge`, `building=bridge`, or the `bridge` category in the cached 3DMR
information record. An explicit negative bridge tag overrides the category and
geometry heuristic. Add `bridge=yes` or the bridge category to short or curved
bridge uploads whose shape alone is ambiguous. Metadata is read from the record
already cached during position discovery; missing or invalid cached information
falls back to geometry without an additional network request.

For legacy uploads without bridge metadata, the horizontal extents along the
mesh's principal axes must exceed the existing 100 m span and 3:1 aspect
thresholds. Unlike east/north bounds, this test does not depend on bearing.
It remains a heuristic: elongated buildings can still resemble untagged bridges.

Recognised bridges retain the upload's geographic orientation instead of aligning
to a nearby building's OSM footprint. Explicit per-model yaw corrections still
take precedence. Ordinary buildings retain footprint alignment.

The bridge pipeline traces the exposed deck and aligns it with mapped motor-road
ends. When no motor-road profile exists, it retains the authored deck clearance
above the model's lowest support datum, using the highest terrain sample at the
centre and deck ends. Fitting a footbridge walkway directly to river-edge terrain
had submerged Elizabeth Quay Bridge. This fallback is not a surveyed vertical datum.

The visual model is drawn beside an invisible collision slab. If mapped road ends
extend beyond the model, collision retains the full
authored span instead of cutting off the opposite end. The existing sinking aprons
remain at the slab ends.

Only covered sections of procedural road bridges and their markings/barriers are
removed; uncovered approach sections remain. Node heights and widths interpolate
together at these cuts. Elevated generated railway segments are also suppressed
inside model bridge footprints. Ground tracks below overpasses remain intact.
This assumes the imported bridge already supplies any elevated railway it covers.
For models identified as pedestrian bridges, covered, aligned procedural footbridge
sections are also removed; uncovered ends and crossing paths are retained.

Deck tracing starts with each sample's highest open floor, so low pier caps do not
interrupt the road surface above them. After road-height alignment, vertices at
the model's lowest footing tier extend down to 0.5 m below local terrain, by at most
50 m. This retains triangle counts, textures and UVs, but stretches those faces;
models with multiple foundation tiers may still need authored foundation edits.
Feature version 34 invalidates previous baked models and cached bridge footprints.

For bridges with an explicitly named `Asphalt` surface, motor-road entrances grade the connected ground road to the imported deck's
actual edge and slope, using one cubic Hermite profile for both terrain and retained
procedural connectors. These local profiles extend up to 180 m along connected
roads and refuse endpoint corrections over 8 m. The native network grader remains
the fixed reference for model placement, preventing terrain/placement feedback.
The abutment fill blends over up to 12 m of the retained connector's last section;
terrain beyond that short embankment is unchanged. Lower crossing roads and tracks
are protected with smooth shoulder transitions. Profiles
are shared between anchor views and apply only with 3DMR enabled. Where a road
approach must pass above a lower road, the importer can generate an elevated deck
using the same profile, leaving the lower terrain unchanged. The candidate skips an
approach when sampled crossing headroom is insufficient or uncertain; this sampling
is not a full clearance guarantee. An existing mapped
end already overlapping the model at the same height does not need a new profile.
Terrain noise outside these local approach corridors is unchanged.

Generic exposed floors are insufficient evidence for reshaping terrain: existing imports
without an identified asphalt surface retain native approach grading. Road skins and
off-block collision workers reconstruct the same grading context from the saved content
flag, including when another process built that content. This prevents road skins being
buried by a separately graded terrain block.

On recognised bridges, a material named `Asphalt` (case-insensitive) uses the same
`bne_bridge_asphalt` material as generated bridges, with the game's terrain asphalt
maps. Other materials, including timber and markings, retain their authored appearance.
Exposed downward-wound asphalt and timber floor sheets are turned upward; closed slab
undersides are retained. Neither operation adds triangles. This is an explicit material-name convention,
not colour-based detection of arbitrary uploaded surfaces.

Mount Henry (53) and Narrows (54) have position overrides matching the geographic
origins in their GLB scene extras; their published pins were approximately 20 m south
of those origins. This corrects these uploads locally without changing public metadata.

This does not add full glTF PBR support, Draco decoding, multi-level or arbitrary
looping-deck tracing, or a new vertical datum/anchor convention. The existing
download/triangle limits, endpoint adjustment limits and local-model collision
behaviour still apply. Spatial and driving validation in BeamNG is still needed;
synthetic mesh tests do not establish correct real-world road heights.

Regression checks:

```
python tools/run_tests.py test_model_bridge_detection test_threedmr test_buildings test_railways test_bridgejoins
```

These modules also run in the foundation quality gate. Fixtures are generated in
the tests; no third-party bridge GLBs are bundled.
