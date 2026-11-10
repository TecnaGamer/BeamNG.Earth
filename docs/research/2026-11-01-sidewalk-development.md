# Built-environment context for procedural sidewalk decoration

This change classifies local development only to select procedural trees, hydrants, benches and
bins on sidewalk runs that already exist. It neither creates sidewalks nor changes their existence
rules. Municipal population and municipal boundaries are not inputs. Existing bridge, banking,
rural road-classification, lighting, and mapped-infrastructure logic remain outside this classifier.

## Existing geographic observations

The shared `mapdata.py` pipeline already downloads vector data in global Web-Mercator z14 cells,
about 1.8 km across in Wyoming. Decoded layers are cached as
`mapdata/v2/overture/<release>/<layer>/<z>/<x>/<y>.npz`; immutable source range blocks are reused
under `mapdata/raw`. `mapgeom.Features` preserves source geometry and attributes and memoizes
frame coordinates and feature bounding boxes.

| Existing layer | Useful observations | Existing availability |
|---|---|---|
| `buildings` | Footprints, holes/multipart geometry, class, subtype, underground flag, height/floors, source records | Default vector layer; also used for road widths and building generation |
| `land_use` | Commercial/retail/residential/industrial polygons; class, subtype, original `source_tags` JSON | Default vector layer; also used by surfaces and vegetation |
| `places` | Shop, office and amenity locations; `basic_category`, `taxonomy`, status, confidence | Optional layer already requested for gas stations |
| `infrastructure` | Explicit benches, street lamps, bins, hydrants, crossings and other mapped objects | Default vector layer; independent of procedural decoration |
| `landcover`, `canopy` | Existing land-cover/rural classification and observed vegetation | Their existing pipelines are retained |

These are Overture observations, including OSM footprints, classifications and land-use polygons,
plus other building and POI contributors. The runtime is not a complete raw OSM-tag store:
`osmtags.py` separately joins OSM highway tags, while buildings and land use arrive through
Overture. Blank building classes are common, so subtype is also useful. Source POI coverage and
quality vary; absence is not proof that an area has no shops.

No new API, Overpass query, dependency, or download layer is introduced. The scorer reads only
existing decoded mapdata memory entries or existing NPZ files. It never invokes `MapData.cell`,
prefetch, or another network-capable lookup. Missing observations provide no positive evidence.
Missing/corrupt inputs remain sparse. Source file timestamps and sizes, including missing-file
stamps, are included in generated-content keys so later arrival, replacement, or change can
invalidate an earlier result without disabling generated-content reuse indefinitely.

## Scoring algorithm

`development.Context` is reusable independently of road generation. It accepts a local frame,
tile box and lists of source Features, or a lazy loader. `for_tile` supplies the cache-only loader.
`sample(road_center_xy, outward_unit, road_half_width)` returns diagnostic measurements and two
continuous decoration weights. Sampling uses the road centre; the outward vector selects the
frontage on one side of that road.

The default neighborhood radius is 100 m. `BNE_DECOR_RADIUS_M` configures it, clamped to
25–250 m. A 2 m raster with a halo covers the complete neighborhood and frontage window outside
the tile. Polygon masks preserve holes and multipart geometry; union masks prevent overlapping
footprints or copied source features from inflating area. Whole buildings and POIs are deduplicated
by source ID. Land-use pieces clipped to different cells are all retained.

Six raster channels represent raw footprint, occupied footprint, commercial footprint, commercial
land use, residential land use, and industrial land use. Underground buildings are excluded;
agricultural/outbuilding/roof/ruin/construction classes contribute to raw footprint diagnostics but
not occupied development. Residential polygon membership describes the pattern and cannot enable
decoration by itself. A lone warehouse or rural residential polygon cannot establish downtown.
The existing 2 km² oversized-building veto is also applied to local candidate polygons, including
direct cache reads; malformed geometry or attribute arrays are ignored before indexing.

Circular coverage is an approximate union area at 2 m resolution, rather than centroid inclusion
or summed uncut footprint areas. Row prefix sums make a disk query independent of building count.
Let `B` be occupied building coverage, `C_b` commercial building coverage, `C_l` commercial land-use
coverage, `R` residential land-use coverage and `I` industrial land-use coverage.

Same-side frontage `F` examines a 120 m window at 2 m stations. At each station it tests occupied
building pixels 6, 14, 22 and 30 m beyond the carriageway edge. With station occupancy `h`,
`F = (mean(h) + mean(h[i] and h[i+1])) / 2`. This rewards developed frontage and adjacent occupied
samples without the abrupt longest-run change caused by one small gap in a street wall.

Accepted pedestrian POIs exclude closed entries and finite confidence below 0.5. Categories include
shops, offices, restaurants, cafes, banks, libraries, schools, museums and related local services.
The effective nearby count is `P = sum(max(0, 1 - distance² / radius²))`. Its contribution is capped;
many colocated or remote business entries cannot substitute for occupied buildings and frontage.

Commercial context and the combined development score are:

```text
C = min(1, max(C_b / max(B, 0.04), C_l / 0.35, P / 4))
S = min(1, 0.50 * min(B / 0.28, 1)
           + 0.30 * F + 0.15 * C + 0.05 * min(P / 4, 1))
```

For `smooth(x,a,b)`, clamp `(x-a)/(b-a)` to 0–1 and apply `t²(3-2t)`. The two weights are:

```text
limited = smooth(B, .025, .14) * smooth(S, .13, .32)
urban   = limited * smooth(B, .10, .22) * smooth(F, .25, .65)
                  * smooth(C, .15, .65) * smooth(S, .45, .70)
urban  *= 1 - smooth(I, .3, .7) * (1 - smooth(P, 1, 4))
```

The diagnostic tiers are rural/sparse when `limited == 0`, rich urban when `urban >= .5`, and
limited otherwise. Dense residential frontage remains limited without local commercial or pedestrian
evidence. Industrial land without a pedestrian cluster is reduced toward limited decoration.
Actual placement uses continuous weights rather than switching decoration spacing at tier borders.

Existing furniture stations, spacing, deterministic hashes, junction clearance, median exclusion
and bridge clearance stay in place. At an eligible station, procedural tree acceptance is
`.25*limited + .75*urban`; hydrants use `.5*limited + .5*urban`; benches/bins use
`.25*limited*C + .75*urban`. Existing class suitability filters remain. Limited residential context
therefore produces restrained vegetation/infrastructure and requires commercial context for benches
and bins. The classifier has no authority over mapped observations or the streetlight/pole branches.

Overlapping neighborhoods and the same metric raster lattice give neighboring tiles and road splits
the same context at the same position. The frontage window also spans road splits. Smooth weights
produce a gradual change in procedural acceptance, subject to the 2 m observation resolution and
the discrete positions of individual props. Different sides may legitimately differ in frontage.

## Geographic replay

Original source Features were captured read-only from release `2026-09-23.1` into a portable
814,198-byte NPZ plus its provenance manifest. The fixture selects complete features intersecting
a 900 m square and retains actual named road geometry. It does not invent sidewalk existence.
See `server/tests/fixtures/sidewalk_development.md` for exact coordinates, source IDs and capture
details. The Round Top sample is the actual `Roundtop Road` feature, rather than a neighboring
Horse Creek segment.

The default 100 m query at the actual road centre gave:

| Sample | Raw footprint coverage | Frontage, two sides | Score, two sides | Tier, both sides |
|---|---:|---:|---:|---|
| Cheyenne downtown, Capitol Avenue | 49.15% | .719 / .686 | .916 / .906 | Rich |
| Cheyenne residential, Bent Avenue | 28.34% | .702 / .520 | .711 / .656 | Limited |
| Laramie downtown, South 2nd Street | 54.05% | .810 / .810 | .943 / .943 | Rich |
| Horse Creek Road | 3.51% | 0 / 0 | .063 / .063 | Rural/sparse |
| Roundtop Road | 0.84% | 0 / 0 | .015 / .015 | Rural/sparse |
| Happy Jack Road | 0% | 0 / 0 | 0 / 0 | Rural/sparse |

Both downtown samples had commercial/POI support; the residential sample had none nearby and its
rich-urban weight remained zero. All rural samples had zero limited and urban weights even though
source residential land-use polygons intersect the neighborhood. A replay using full original
cached cells, rather than the selected fixture vectors, returned the same tiers and scores.
These samples do not establish conditions along every part of the named roads.

## Performance and cache cost

Work is per tile, not a network operation per road. Building the index filters source feature
bounding boxes, transforms coordinates once per frame and paints local polygons. Each disk query
then visits approximately `2*radius/2m + 1` prefix-sum rows; frontage uses a fixed 61×4 set of pixel
probes. POI weights are vectorized over the tile's local accepted points. No geometry engine or
external spatial-index dependency is needed.

On this workstation, the corrected geographic audit measured 28–54 ms to build a city index from
already loaded fixture arrays and 3–4 ms for rural samples. The complete cache-reader audit also
includes source validation and the oversized-footprint veto on local polygons. Source imports
occur before the measurement, as they do before road-tile builds in a running server.
The recorded final full-source run measured:

| Sample | First cached-cell decode and index, ms | Subsequent sample, ms |
|---|---:|---:|
| Cheyenne downtown | 41.444 | .1190 |
| Cheyenne residential | 73.449 | .1021 |
| Laramie downtown | 49.191 | .1046 |
| Horse Creek Road | 14.286 | .1006 |
| Roundtop Road | 10.366 | .0995 |
| Happy Jack Road | 8.865 | .0982 |

The operating system's file cache was warm, and timings vary with concurrent activity. These
are first index builds from complete cached cells, not physical cold-storage benchmarks.
Subsequent tile construction can reuse decoded source
cells; repeated samples within a tile use its completed index. These are development measurements,
not guaranteed frame timings or an in-game performance claim. Missing source files perform only
local checks. The measurement artifacts are `build/sidewalk-development-audit.json` and
`build/sidewalk-development-source-cache-audit.json`.

A default 256 m tile has six approximately 272×272 byte masks and six row-prefix arrays, roughly
2.2 MB of temporary index arrays, plus local POIs and vector views. Index construction is lazy:
tiles without procedural decoration candidates need no raster. The shared decoded-source memo
has a 24 MiB budget accounting for source arrays and the derived coordinate and bounding-box arrays.
Existing mapdata disk caches are reused. Generated road content keys include source-file stamps,
and the existing source-code hash invalidates outputs when the implementation changes.

Reproduce portable scoring and full source-cell timing without downloads:

```powershell
python tools/audit_sidewalk_development.py --repeats 100 --output build/sidewalk-development-audit.json
python tools/audit_sidewalk_development.py --source-cache --repeats 100 --output build/sidewalk-development-source-cache-audit.json
```

## Relevant modules and regression scope

| Module | Responsibility |
|---|---|
| `server/development.py` | Cache-only source access, reusable raster context, score and continuous weights |
| `server/furniture.py` | Apply weights only to optional procedural trees/hydrants/benches/bins on existing walks |
| `server/roads.py` | Pass context to furniture after existing sidewalk generation; existing existence and road-specification rules retained |
| `server/blockworker.py`, `server/earthd.py` | Supply lazy context in worker/direct builds and include source stamps in generated road keys |
| `server/mapdata.py`, `server/overture.py`, `server/mapgeom.py` | Existing source schemas, cache paths and source geometry APIs; no source/download changes |
| `tools/audit_sidewalk_development.py` | Offline fixture capture, replay, category inspection and timing |
| `server/tests/test_development.py` | Synthetic geometry/scoring, source-cache behavior, continuity and captured Wyoming replay |

Regression coverage checks rural suppression, residential/commercial separation, large isolated
buildings, agricultural footprints, industrial context, POI confidence/status, holes/multipart and
overlap, duplicate source observations, tile/road-split consistency, smooth transitions, cache-only
behavior and optional-POI cache arrival. Furniture tests preserve lighting and infrastructure
placement branches and verify absent sidewalk runs do not receive procedural sidewalk props.

Working-tree verification: 26 scorer tests, 27 furniture tests, 2 generated-cache tests, 141
road/sidewalk/vegetation/guardrail tests, and 50 of 52 rural/banking/bridge tests passed.
That is 246 passing tests and two pre-existing errors in captured US30 fixtures, reproduced
without the integration changes. Their stored cover lookup lacks sample keys
`41583083906,-105699463409` and `41716986606,-105888203870`. Logs are in
`build/sidewalk-development-tests`. No in-game drive or visual verification is claimed.


## Isolated PR validation (2026-11-02)

The published patch is based directly on upstream main
`8b0e9b04592fdc27c16a85aeaa055aa3dda58e10`. The original upstream archive was checked
against GitHub blob hashes before applying only the decoration integration and new files.
The separate grass, wind-turbine and rural-road-classification changes are excluded.
The installed working tree retains those independent fixes.

On this isolated 13-file patch, **305 tests passed, 3 optional/live cases were skipped, and
there were no failures or errors** across 17 modules: development, decoration cache, furniture,
roads, US30 infrastructure, I80 banking, Otto bridge, bridge clearance, guardrails, sidewalk
collision, vegetation, road mesh, road widths, road seams, blockworker, road geotiles and geotiles.
All imported project modules came from the isolated checkout. The existing local native extension
was used for testing and is not included in this PR.

Both US30 cases that fail in the broader working tree pass with upstream's original rural
classification. This patch neither changes that classification nor alters its source fixtures.
No game/server restart, in-game drive or visual verification was performed for publication.
