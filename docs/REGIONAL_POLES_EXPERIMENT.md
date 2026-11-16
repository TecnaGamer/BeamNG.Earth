# Recessed concrete poles: opt-in experiment

Current contribution base: `8aaf4ebdd0485facadc755a612f4b0a2b55e4c57`,
1.7.1-beta. The initial pole experiment began on 1.7.0-beta. This page describes
the retained French comparison modes; EUROPE_171_REVIEW.md covers the full v4
contribution. The user has confirmed the concrete-pole
rendering and the later FR/DE polygon-based border transitions in game. Collision,
LOD and FPS validation remain pending. `fr-varied` now adds wood poles and a wider
French kit; see [FRENCH_FURNITURE_EXPERIMENT.md](FRENCH_FURNITURE_EXPERIMENT.md).

## Run

With the source server and matching mod installed:

```sh
python start.py -- --regional-poles fr-demo
# All existing roadside poles in positions classified as French:
python start.py -- --regional-poles fr-all
```

Default is `--regional-poles off`. Restart the server to change it. The packaged
official executable does not contain this option or the new Python code.
Buildings/furniture must be enabled (`start.py` already requests buildings on).

Both French modes now use bundled country polygons at each pole position.
See [COUNTRY_BOUNDS_EXPERIMENT.md](COUNTRY_BOUNDS_EXPERIMENT.md) for coverage,
source data, limitations and border tests. The database now covers worldwide territorial countries and dependencies;
non-FR, uncovered and ambiguous points keep stock poles.
Other country-dependent rules retain their upstream lookup mechanisms.

## What changes

* `poleprofiles.py` selects a model from a versioned profile and stable road ID.
  `fr-demo` keeps the original demonstration mix: about half the existing
  overhead-road runs in France use concrete, the others keep stock poles.
  `fr-all` uses concrete for every such pole in positions classified as French.
  The revisit-cache token includes mode, country algorithm and data version.
  A route crossing the border can change model within one tile. **Neither 50/50 nor all-concrete
  describes the actual French equipment mix.** The separate `fr-varied` mode adds wooden variants and standalone lanterns.
* The pole, 8.5 m above its ground origin, has 13 blind recesses on each broad
  face, 6 cm deep, leaving a solid concrete web. Its dimensions and small bundle
  clamp are illustrative, not a manufacturer's design or a voltage standard.
* One simplified bundled cable attaches at local `(0.25, 0, 7.85)`, with radius
  0.025 m. Cable geometry uses the chosen model's attachment and existing sag,
  road orientation and tile ownership. Stock poles keep their three conductors.
  A span between different attachment layouts is omitted in this prototype.
* Placement, buried-line selection, pole-mounted lamps and their lenses/glows
  are retained. These French comparison modes do not add pole positions or enable other
  countries. The separate `europe` mode enables the full country registry.
  Existing pole placement still guesses overhead lines; it does not use mapped
  individual `power=pole` nodes or their material tags.
* The profile is passed through both the direct and road-worker paths and is
  part of the worker revisit key. No protocol/schema change is required.
* `trees.lua` loads the new model as an ordinary Forest item, with the existing
  streaming, rebase, pooling and near collision lifecycle. Its material directory
  is explicitly loaded. No new per-frame Lua system is introduced.

## Asset and cost

`tools/build_regional_poles.py` reuses `build_rail_crossing_props.Mesh/write_dae`.
Run it with Python + NumPy + Pillow; Blender is not required. The three visual
LODs contain 940 / 736 / 64 triangles (detail sizes 96 / 24 / 2); the collision
mesh is one closed tapered hull of 12 triangles. There are three materials and
one original deterministic 128 x 128 concrete grain texture. No Street View
pixels, game assets or third-party models are included in the new asset.

Triangle counts are measured; the model's visible import is confirmed by the
user's screenshots. Actual LOD switching, nighttime lighting, collisions and FPS
still need in-game checks. The normal pole-mounted lamp remains
the stock one; these two modes only change poles; the wider kit is enabled by `fr-varied`.

## Tests

```sh
python -m unittest discover -s server/tests -p test_regional_poles.py -v
python -m unittest discover -s server/tests -p test_furniture.py -v
luajit mod/tests/test_trees.lua
```

The Python placement tests require the project's native module matching the
pinned 1.7.1 source (the reported checks used official Linux VERSION 100). Additional regression checks cover blockworker,
revisit, powerlines, roadmesh, CLI defaults and server/mod protocol agreement.
Synthetic tests cannot certify the BeamNG importer or a real drive.

## Before proposing upstream

1. Test near Haguenau, plus non-French controls away from the border. Compare
   screenshots and frame times with `off` from identical positions.
2. Inspect grounding, both recess faces, cable endpoints, LOD transitions,
   night lamps, collision, tile reloads and rebases. Preserve failure logs.
3. Agree on a data-led selection policy with maintainers. The roadmap opposes
   location-specific generation; current country-specific spacing/burial tables
   already exist, but are not proof this new profile policy will be accepted.
4. Extend the border checks beyond the FR/DE cases and add mapped pole/network
   data before claiming accurate individual equipment. Keep the geometry family reusable
   outside France; do not encode Haguenau coordinates in the generator.

The reusable country reader is independent of pole families. This contribution
does not replace every upstream country-dependent subsystem; integration with
other regional work should be reviewed with the maintainers.

New code and original assets follow the repository's MIT license. The separately
licensed country database is ODbL-1.0; see its NOTICE file.
