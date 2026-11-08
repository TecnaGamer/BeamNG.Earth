# Interchange replay inputs

Places reported as badly built bridges or interchanges, captured on 2026-11-08 and 2026-11-09 with
`tools/capture_interchange_fixture.py` into a cache of their own (nothing from a player's cache):

| file | issue | point (lat, lon) |
|---|---|---|
| `interchange_a61_928.npz` | GitHub #928 | 50.607684, 6.996477 |
| `interchange_bologna_929.npz` | GitHub #929 | 44.5, 11.3 |
| `interchange_prague_924.npz` | GitHub #924 | 50.040185, 14.481886 |
| `interchange_a20_921.npz` | GitHub #921 | 54.037418, 12.606645 |
| `interchange_castlegar_939.npz` | GitHub #939 | 49.29611, -117.65008 |
| `interchange_silistra_936.npz` | GitHub #936 | 44.097069, 27.139581 |
| `interchange_italy_967.npz` | GitHub #967 | 42.570363, 12.615334 |
| `interchange_m1_ireland_960.npz` | GitHub #960 | 54.036322, -6.385183 |
| `interchange_perth_915.npz` | GitHub #915 | -31.889125, 115.919020 |

Each holds the `roads` layer (mapgeom.Features arrays, prefix `cell<i>.`) of every z14 cell within
1200 m of the point, Overture release 2026-10-21.0, and the raw elevation `dem.Dem.sample` gives at
zoom 16 in the point's ENU frame: every 2 m within 500 m, every 16 m within 3200 m (whole
centimetres, each row stored as steps). These are inputs, before any road grading; the grading,
the carriageway solver and the bridge profiles run afresh on them (`server/tests/interchanges.py`).

Road geometry: © OpenStreetMap contributors, Overture Maps Foundation, ODbL-1.0 (data from TomTom).
Elevation: Mapterhorn (see mapterhorn.com/attribution). See the project's CREDITS.md.

Recreate one (downloads what CACHE_DIR lacks):

```text
python tools/capture_interchange_fixture.py NAME LAT LON CACHE_DIR [--cells-m 1200]
```
