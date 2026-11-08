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

Deck tracing starts with each sample's highest open floor, so low pier caps do not
interrupt the road surface above them. After road-height alignment, vertices at
the model's lowest footing tier extend down to 0.5 m below local terrain, by at most
50 m. This retains triangle counts, textures and UVs, but stretches those faces;
models with multiple foundation tiers may still need authored foundation edits.
Feature version 29 invalidates previous baked models and cached bridge footprints.

These changes do not alter terrain heights, terrain noise or road grading. Removing
a whole procedural approach could expose the existing rough terrain beneath it;
retaining the uncovered approach addresses that gap rather than flattening riverbanks.

This does not add full glTF PBR support, Draco decoding, multi-level or arbitrary
looping-deck tracing, or a new vertical datum/anchor convention. The existing
download/triangle limits, endpoint adjustment limits and local-model collision
behaviour still apply. Spatial and driving validation in BeamNG is still needed;
synthetic mesh tests do not establish correct real-world road heights.

Regression checks:

```
python tools/run_tests.py test_model_bridge_detection test_threedmr test_buildings test_railways
```

These modules also run in the foundation quality gate. Fixtures are generated in
the tests; no third-party bridge GLBs are bundled.
