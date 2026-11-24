# Cliff geometry and transition references

These are offline reference data, not BeamNG stock assets. Records were audited
2026-11-20. Their provenance is distinct from the visible limestone color map:

- `cliff_height.png`: low-resolution area-filtered height from the assembled CC0
  Poly Haven natural scan relief, periodically blended at edges and normalized
  to the authored relief range. See `cliff_natural.NOTICE.md`.
- `cliff_cut_height.png`: the same scan geometry with procedural bedding joints,
  chips and intermittent excavation grooves (seed 94726), area-filtered and
  normalized. Recorded by the original `bake_roadcut_material.py` authoring recipe
  on 2026-10-31. It was retained when the visible limestone imagery changed; it is
  not a height map recovered from the later AI-generated limestone image.
- `cliff_arid_cut_height.png`: offline inferred relief from the arid assembled
  image, clamped to its 1st/99th percentiles and area-filtered to 256 x 64,
  encoded as unsigned 16-bit PNG. See `cliff_arid.NOTICE.md`.
- `cliff_canyon_height.png`: the canyon authoring recipe's normalized relief,
  periodically filtered to 256 x 64 and encoded as unsigned 16-bit PNG.
  See `cliff_canyon.NOTICE.md`.
- `cliff_transition_means.json`: four mean linear RGB values, ordered natural,
  limestone, arid, canyon. Each was computed from its authored color image and
  material color factor. These are numeric calibration data under MIT, not
  an additional third-party image. Preserve their calibration when resampling
  source maps; texture resolution does not change the physical cliff scale.
- `terrain_rim.data.webp`: original numerical 16 x 16 linear coverage ramp,
  generated offline; MIT. It contains no photographic source.

The historical assembly intermediates and complete generation environment are
not shipped. The supplied small reference files are canonical runtime inputs;
this notice documents their origin without claiming byte-identical regeneration
of the original artwork from missing prompts/intermediates.

| Reference | SHA-256 |
| --- | --- |
| `cliff_height.png` | `1aa002eabb358741d3c491352773942113368caee05545ff177c5ae16b15ba25` |
| `cliff_cut_height.png` | `deb047163e05a9be001a92f19940ea5f91af18a9545471d822bc509a9df98502` |
| `cliff_arid_cut_height.png` | `cb8ddb6e11c9435d6f6d8c326afb49adc05fd36138dab33c3b70059b87aa4c0d` |
| `cliff_canyon_height.png` | `a6b22c792e3fec69177e80f54dcdcb850e6586f9c5eb0def79590bbd87bcbfe6` |
| `cliff_transition_means.json` | `6ef27dc70a94d251a4ff5f0c61897039837ff7beccdfa9f78e968b54e4afcf84` |
