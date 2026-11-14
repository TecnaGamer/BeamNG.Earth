# Polish LOD1 buildings (GUGiK 2024), opt-in

Official building footprints with measured heights for all of Poland (about 15 million buildings), from
[maalos/LOD1_2024_PL](https://github.com/maalos/LOD1_2024_PL) (GUGiK's 2024 LOD1 pack in a compact `.bld` format, 250 MB).

## Use

Set one variable before starting the server (nothing is installed or cloned):

```powershell
$env:BNE_PL_LOD1 = '1'
```

The first time a map cell is built, the repository's `index.npz` (0.6 MB) is fetched into `<cache folder>/polandlod1/`;
then, for a cell wholly in Poland, the file(s) of its county (0.1 to 3.7 MB each, 250 MB for all 433 if you drive the
whole country) are fetched from `raw.githubusercontent.com` (branch `master`), checked against the size and SHA-256
in the index, and kept there. Later cells, and later sessions, read them from disk (and keep working offline). The
index is fetched again after a week; a refresh that fails keeps the old one. Nothing changes without the variable,
and nothing changes outside Poland.

## What it does

- A map cell lying **wholly in Poland** (bundled country borders) **with LOD1 tiles in it** takes its `buildings` from
  LOD1 instead of Overture, and its `building_parts` are left empty (they belong to Overture buildings that are no
  longer drawn). Border cells, cells without tiles and cells outside Poland keep Overture, so there is a seam along
  the border and nowhere else.
- Each building gets its footprint (outer rings and courtyard holes) and its height (roof minus ground). Overture's
  names, types, roof shapes and colours do not exist in LOD1, so those columns are empty and `buildings.py` falls back
  to its height-based guesses (floors from height, flat/gabled roofs by footprint). LOD1 is a footprint and height
  source, not a style source.
- Cells are cached apart from Overture's (`<overture release>+pllod1.<VERSION>`), so turning it on or off never
  serves the other source's cells. `MapData.versions()` carries `pl_lod1` (the pinned commit) only while it is on.
- `src_dataset` is `GUGiK LOD1 2024`. `buildings.TRUSTED_FOOTPRINTS` lists it beside OpenStreetMap, so the surveyed
  outlines are not run through the machine-learned-footprint filter (`_ml_false`: buildings on roads, bridges, water).

## How the files are found and fetched

- `index.npz` at the root of [maalos/LOD1_2024_PL](https://github.com/maalos/LOD1_2024_PL) (built by
  `tools/build_polandlod1_index.py` from a clone, and committed there) holds every file's name, size, SHA-256 and
  building count, and the rectangle of each of the 65,745 tiles with the file and block it sits in. So "is this cell
  covered?" is answered without any file on disk, and always the same way: the cell files' cache path depends on it,
  and it must not change as files arrive. Rebuild and commit the index whenever a file in `bld/` changes.
- The files are `https://raw.githubusercontent.com/maalos/LOD1_2024_PL/refs/heads/master/bld/<name>` (what
  `github.com/maalos/LOD1_2024_PL/raw/refs/heads/master/bld/<name>` redirects to; the request goes there directly,
  through `netfetch`, which does not follow redirects).
- Downloads go through `netfetch`'s lane `lod1` of `raw.githubusercontent.com`, which has a policy of its own (600 a
  day soft, 1000 hard, 2 at once, prefetch allowed) apart from the update check's 24 a day on the same host.
  `SOURCES.md` lists it.
- Without an index (offline on the first run, GitHub down, a file that does not load) the feature is off and Overture
  serves; it asks again after a minute. The cell files are cached under their own path (`+pllod1`), so Overture's
  cells built meanwhile are not mistaken for LOD1's.
- A *file* download that fails (host down, budget, `--offline`, size or hash not as the index says) raises netfetch's
  error: the cell is not built and not cached, and is asked again later. It is never filled in from Overture
  meanwhile, which would keep the wrong source under the LOD1 cache path.
- A file is written whole (`.part`, then rename); one fetch per file however many cells wait on it.
- The cell files' identity (`MapData.versions()`, `pl_lod1`) carries a digest of the index, so a new index is a new
  identity.

## Files

- `server/polandlod1.py`: the index and downloader (`Manifest`, `Index`), the block decoder, the cell query; no
  dependency beyond numpy.
- `tools/build_polandlod1_index.py`: builds `index.npz` for the dataset repository.
- `server/pl1992.py`: EPSG:2180 to WGS84 and back in pure numpy (Transverse Mercator, Kruger series, error below
  0.001 mm), so no pyproj.
- `server/mapdata.py`: `polandlod1.configure` in `MapData.__init__`, the hook in `MapData._build`, the cache path,
  `versions()` and the credit line.
- `server/netfetch.py`: the `lod1` lane's policy and role text (a lane may now have a policy of its own, keyed
  `host#lane`).
- `server/tests/test_polandlod1.py`: format, projection, cell, download and index tests (a small file written by the
  test itself and a fake fetcher: no network).

## Format notes (for the next reader of the decoder)

The dataset's README leaves two details open; both were settled on real tiles (Wroclaw old town):

- An *ortho* ring's `k` is its rotation in units of 90 deg / 16384, and `swap` means its first edge runs along the
  rotated y axis. Only this reading makes the corners of ortho rings coincide with neighbouring buildings' corners
  (10.7 % of them, against a 1.8 % chance level for every other angle unit, sign or swap reading tried).
- Coordinates are EPSG:2180 with x = easting. Rings are stored open (the first vertex is not repeated); the reader
  closes them.

## Open points

- It is opt-in because the first cell in a new county waits for that county's download (Wroclaw's file is 1.5 MB: the
  first cell took 1.5 s here, download and decoding together). Prefetching a saved area's counties ahead
  (`kind="predownload"`) would hide that; not done.
- Border cells keep Overture. Merging LOD1 with Overture's buildings outside Poland in those cells is possible but not
  done.
- Decoding is pure Python: about 1 s for the first cell of a block of tiles (a block holds up to a few dozen tiles),
  then milliseconds for its neighbours (4 decoded blocks are kept).
