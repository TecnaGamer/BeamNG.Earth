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

The existing bridge pipeline traces the exposed deck, aligns its ends with mapped
road ends (or terrain at the deck ends), and draws the visual model beside a
separate invisible collision slab. It suppresses procedural bridge parts within
the traced footprint. Deck tracing starts with each sample's highest open floor,
so low pier caps do not interrupt the road surface above them. Visual triangles,
texture resolution and material assignment are unchanged. Feature version 28
invalidates previous baked models and cached bridge footprints.

This does not add full glTF PBR support, Draco decoding, multi-level or arbitrary
looping-deck tracing, or a new vertical datum/anchor convention. The existing
download/triangle limits, endpoint adjustment limits and local-model collision
behaviour still apply. Spatial and driving validation in BeamNG is still needed;
synthetic mesh tests do not establish correct real-world road heights.

Regression checks:

```
python tools/run_tests.py test_model_bridge_detection test_threedmr test_buildings
```

These modules also run in the foundation quality gate. Fixtures are generated in
the tests; no third-party bridge GLBs are bundled.
