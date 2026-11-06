# Dutch RVV signs: setup and review guide

This is the first integrated implementation of data-driven Dutch traffic signs.
The older `NL_*_V*.md` notes document development milestones; this page describes
the combined behavior in this branch.

## What changes in the game

- NDW/George source records select shared RVV assets. The game does not fetch
  street photographs, run OCR, or create a separate texture for every location.
- The asset inventory contains 800 catalogue entries, split into 830 plates and
  827 distinct models. Exact, unambiguous aliases expose 669 asset keys; this is
  not a claim that all catalogue entries can be matched to every NDW variant.
- Plates have a closed back, 3 mm thickness and a 12 mm rear return. Three supplied
  bottle-pole lengths and separate clamps support stacked and back-to-back plates.
  The pole top stays 5 cm below the highest plate.
- Nearby compatible source records share a pole. Mounting order and supplementary
  plates are retained; ambiguous/unsupported combinations are reported or skipped.
- Pole locations are moved outside all known nearby navigation-road corridors,
  with a bounded 15 m shift. Without a valid candidate, the sign is skipped.
- Round and triangular plate sizes use road class/legal speed metadata, separate
  from the AI's bend speed. Round diameters are 0.6/0.8/1.0 m; triangle widths are
  0.7/0.9/1.1 m. Motorways use the largest category even with a low temporary limit.
- Road context can align a source direction with the local road. Precise headings
  retain a 20-degree correction limit; cardinal headings may align by less than
  45 degrees. Ambiguous crossings, perpendicular headings and possible reversed
  faces are preserved/flagged instead of blindly flipped.

## Netherlands-only replacement

`nl_country.lua` contains the full PDOK/Kadaster Landgebied LND6030 boundary,
including enclave holes. Position checks use local latitude-indexed edges; no
country lookup request is made while driving.

Inside that boundary, RVV-only suppresses the owned legacy Forest traffic signs
and old sign meshes, including already displayed and delayed objects. It does
not sweep unrelated scene objects, advertising, vegetation or traffic lights.
Outside the Netherlands, the ordinary sign policy is restored and the Dutch
source renderer stops. Unknown/transition positions do not enable suppression.

**In the Netherlands, missing source coverage intentionally leaves gaps:** a
missing dataset, disconnected server or unsupported record does not restore
invented legacy signs. This also means the dataset must be configured for the
feature to be useful. `nlremote.report()` exposes the effective replacement state.

## Dataset and server

The nationwide source dataset and four large data archives are **not committed**.
Build/provide a completed George tile store separately using the tools described
in [the George importer](NL_GEORGE_V5.md) and [national dataset notes](NL_NETHERLANDS_V9.md).

Start the normal companion server with its existing options plus:

```text
--nl-signs-store PATH_TO_DIRECTORY_WITH_THE_FOUR_V9_DATA_ZIPS
```

The server reads those inner ZIP parts directly. Do not extract hundreds of
thousands of render-tile files, and do not substitute the outer distribution ZIP.
A completed loose `manifest.json`/`render` store also works.

The existing companion protocol advertises the capability and sends compact rows
with a shared asset dictionary. Server caching is bounded to 128 tiles/16 MiB of
original tile bytes, with one worker and bounded queued jobs. The client keeps
nine tiles, one in-flight request and at most 40 nearby source records within
120 m. Timeouts, revisions, reconnects and anchor changes invalidate stale work.

Install this branch as a **complete Earth mod**, using the project's normal
installation path. Mixing the new core with older base modules can prevent Earth
from loading. Personal update ZIPs/start scripts used during development are not
required by this PR. The generic incremental updater remains a developer tool.

## Review map

- `server/nlsigns.py`, `server/earthd.py`: bounded archive-backed protocol service.
- `nlremote.lua`, `nlndw.lua`: transport, country policy and neighborhood lifecycle.
- `nl_context.lua`, `nl_placement.lua`, `nl_testset.lua`: matching, size, mounting,
  roadside planning and retained scene objects. Empty render plans are safe.
- `trees.lua`, `roads.lua`, `nl_legacy.lua`: owner-scoped legacy removal.
- `server/navgraph.py`, `navgraph.lua`: legal road metadata in edge field 10,
  preserving both endpoint radii and upstream tunnel/traffic behavior.
- `nl_catalog`, `nl_testset`: shared generated assets, not NDW photos.
- `tools/nl_test_source`: small inputs needed for reproducible core-asset tests.
  `tools/nl_site_source/sources.json` retains catalogue provenance; the optional
  source-image cache is fetched only for a full rebuild and is excluded from Git.
  Byte-preserving attributes keep manifest hashes stable across platforms.

## Validation and remaining limits

Local checks cover importer validation, closed catalogue geometry, deterministic
asset generation, actual LuaJIT quaternion math, stacking, empty plans, boundary
transitions, stale responses, immediate legacy cleanup and upstream regressions.
LuaJIT checks can use `pip install --target RUNTIME_DIR lupa==2.6` followed by:

```text
python tools/run_lua_jit.py --runtime RUNTIME_DIR test_nl_context.lua test_nl_country.lua test_nl_remote.lua test_nl_ndw_stream.lua test_nl_george_tiles.lua test_nl_testset.lua test_highway_signs.lua test_trees.lua test_core_stubbed.lua test_core_robustness.lua test_navgraph.lua
python -m unittest discover -s server/tests -p test_nlsigns.py
python -m unittest discover -s server/tests -p test_navgraph.py
node tools/test_george_v5.mjs
node tools/test_ndw_import.mjs
node tools/test_nl_site_catalog.mjs
node tools/test_nl_testset.mjs
```

Venlo gameplay has exercised the integration and road-size behavior. The latest
cardinal-heading correction still needs visual acceptance at the reported skewed
sign. This is not a fully visually accepted nationwide release: special-format
sizes, uncertain road associations, source-facing conventions, incomplete OTHER
subplates and unmapped variants remain open. Corridor avoidance cannot guarantee
every rendered asphalt edge, building, tree or pedestrian clearance is correct.
No nationwide FPS or memory acceptance claim is made.

Source records/provenance: [NDW/George](NL_GEORGE_V5.md),
[catalogue inputs and limits](NL_V8_CATALOG_PLACEMENT.md),
[supplied poles and vector sources](../tools/nl_test_source/README.md).

## Smaller package, same catalogue

The old single-C1 prototype, its separate renderer and prototype-only tests have
been removed. The current `nl_testset` name is retained for compatibility: this
folder/module also supplies the **production** manifest, poles, core plates and
renderer, so deleting it would break streaming. Its fixture and regression test
remain useful for mounting, empty neighborhoods and scene cleanup.

All 827 catalogue models and textures remain available. Textures are losslessly
recompressed at the same 512x512 RGB resolution; decompressed PNG scanlines are
verified byte-for-byte. Materials are grouped into 26 lazy-load banks of at most
32 definitions, replacing 827 tiny per-model JSON files. `material_file` points
to the corresponding bank; the existing renderer loading API is unchanged.

The 796 downloadable source PNGs are not part of the game package. Their pinned
URLs/hashes and the cache restore tool remain; see the
[optional build cache](../tools/nl_site_source/README.md). Ordinary tests are
network-free and use the checked-in generated assets. The live catalogue builder
was also rerun against all 800 pinned source records in a temporary directory
after the repack.
