# Cached, authentic source inputs

Retrieved 2026-11-05. SVG author: Bouwe Brouwer, source RVV 1990. Each linked Commons file page declares public domain, PD Dutch traffic signs. No Informatiebord product art, starterpack textures or AI-drawn pictograms are included.

- A1-50: https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_A1_2023.svg
- B6: https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_B6_2023.svg
- C1: https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_C1_2023.svg
- J16: https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_J16_2023.svg

`prepare.cjs` rasterizes the cached SVGs, extracts convex silhouette contours for the two rounded triangles and produces RGB 8-bit 1024×1024 PNGs. The triangle render is resampled to a square texture; the model retains its real width/height aspect, restoring the original proportions through UV mapping. Circle geometry is shared as the same 48-segment contour. No sign artwork is invented. The source conversion was visually inspected for all four codes.

Optional regeneration of the cached inputs:

```
npm install --prefix tools/nl_test_source
node tools/nl_test_source/prepare.cjs
node tools/generate_nl_testset.mjs
```

The regular generator only needs the cached inputs and Node 18+, no network or external packages. Source SVG and converted texture hashes are in the generated manifest.

The three pole DAEs and single clamp DAE are original files from the user-supplied `BeamngEarth_Nederlandse_Flespalen_LowPoly.zip`. Original geometry and source-origin conventions are retained. That archive has no separate license text, so no wider grant is asserted on behalf of its creator. The pair of clamps is constructed by translating two copies of the source clamp to ±0.14 m.

## Added variants (v5, retrieved 2026-11-05)

A1-30-ZB and A2-30-ZE are the exact cached zone-30 SVGs by Fry1989, colour updates SilverJapan2006, derived from Bouwe Brouwer artwork; PD Dutch traffic signs. OB504 is by Ad Verburg, CC0 1.0. Source pages and per-file hashes are in manifest.signs. See docs/NL_VARIANTS_V5.md. No other speed or supplementary content is inferred from these assets.

Trailing line whitespace in the OB504 source cache is normalized; its SVG geometry/artwork is unchanged. The manifest hash identifies the normalized cached file.
