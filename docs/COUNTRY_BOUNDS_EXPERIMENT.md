# Worldwide position-based country lookup — experiment

Base: BeamNG.Earth 1.7.1-beta, commit
`8aaf4ebdd0485facadc755a612f4b0a2b55e4c57`. Contribution revision `europe-1.7.1-v4`,
29 October 2026. Opt-in prototype; see EUROPE_171_REVIEW.md for the current scope.

## Behaviour and scope

The previous pilot used eleven country polygons. Its FR/DE model transitions
have now been confirmed in game by the user. This revision replaces the pilot
database with all territorial `country` and `dependency` features in the pinned
Overture divisions release 2026-09-23.1: **272 territorial records**, 1,493 polygon
parts, 5,758,924 vertices. These are not 272 sovereign countries.

`countrybounds.bundled().at(latitude, longitude)` returns the source's country
or territory code, or `None`. It is independent of the French assets and reusable
by other consumers. The current integration uses it at the actual position of
regional poles, lanterns, markers and signs. A road can change profile within a
tile. In `fr-varied`, outside FR the stock furniture is retained. In `europe`, the
explicit 56-code registry receives country-specific poles, delineators,
chevrons, town signs and a sidewalk palette. The French kit remains FR-only.
Russia and Turkey follow their full source country geometries. STOP selection
and placement remain upstream; only the visible STOP label changes in Quebec
and Turkey after generation. Other Canadian scenery remains stock. Overseas codes such as GF
remain distinct; they do not automatically receive the metropolitan French kit.

The module does not replace every country-dependent rule in BeamNG.Earth.
Driving side, speed units, underground-line probability, existing lighting
spacing, geocoding and the map's border drawing keep their upstream behaviour.
Those consumers need their own review before a general country-service PR.

## Database and accuracy

`server/data/country_bounds.zip` is 43,832,254 bytes. It preserves original
coordinates, rings, islands and holes without simplification, rounding or extra
clipping. Territorial water is included, allowing classification on bridges.
Each ZIP member is one full polygon; `index.json` retains source IDs, versions,
provenance, country codes and bounding boxes. See the adjacent NOTICE for ODbL.

Coverage follows this dated source, including its special/disputed territory
codes and its Mercator polar limit, about ±85.05°. This is worldwide coverage
within that source, not a guarantee that every coordinate has an unambiguous
country. Open sea, an exact polygon edge, conflicting overlaps and missing
coverage yield `None`; no airport fallback is used. Political perspective and
boundary accuracy are source limitations, not facts inferred by the algorithm.

## Runtime

The archive checksum is checked once per process. Only the compact index is
loaded initially. Latitude buckets identify candidate polygons; an LRU of eight
loaded polygon parts bounds the working set. Each ring has its own edge buckets.
Longitudes wrap across the antimeridian; holes and enclaves are tested explicitly.
The reader uses NumPy and Python's standard library already present in the app.
There is no native rebuild, online lookup or new runtime dependency.

Missing/corrupt data fails visibly when a regional mode is enabled. `off` loads
none of this data. Cache tokens include algorithm version and data SHA-256;
`fr-varied` additionally includes both sign database hashes and the kit version.
No manual deletion of the geographic download cache is needed.

## Validation

Twelve country tests cover real controls in Europe, North/South America, Africa,
Asia, Oceania and French Guiana, plus enclaves, islands, borders, invalid points,
wraparound and cache/worker behaviour. The user's Nothweiler position
49.06593, 7.79547 is DE. Earlier synthetic Rhine crossings still switch within
one tile and agree with split-tile output.

An independent Shapely/GEOS classifier agrees on **7,493 positions**: one interior
representative per polygon part and 6,000 seeded random global points. This checks
the algorithm against the same data, not the source against surveyed reality.
Indicative local measurements: 0.043 s initial index/hash load, 0.454 ms per warm
lookup around Haguenau. These are not in-game FPS measurements.

## Rebuild and review

Developer prerequisites: normal server dependencies plus Shapely. Players do not
run these tools.

```sh
python tools/fetch_country_bounds.py --cache-dir CACHE --output RAW_COUNTRIES
python tools/build_country_bounds.py RAW_COUNTRIES server/data/country_bounds.zip
```

The fetch is pinned to release 2026-09-23.1. The builder validates the source and
prints the checksum. Review changed data, then deliberately update the runtime
pin. Byte reproducibility assumes the same compression toolchain; decoded
geometry and coordinates are the substantive identity.

A useful upstream proposal can separate this general lookup and its tests from
the French asset kit. The maintainer still needs to agree on a ~44 MB distribution
strategy, update policy, territory conventions and which consumers to migrate.
Port and retest on the maintainer's current branch before submitting. Compatibility with future upstream versions is not implied.
