# Polish LOD1 buildings (GUGiK 2024), opt-in

Official building footprints with measured heights for all of Poland (about 15 million buildings), from
[maalos/LOD1_2024_PL](https://github.com/maalos/LOD1_2024_PL) (GUGiK's 2024 LOD1 pack in a compact `.bld` format, 250 MB).

## Use

Set one variable before starting the server (nothing is installed or cloned):

```powershell
$env:BNE_PL_LOD1 = '1'
```

The server fetches the repository's `index.npz` (0.6 MB) into `<cache folder>/polandlod1/` in the background when it
starts (or when the first Polish cell needs it); then, for a cell wholly in Poland, the file(s) of its county (0.1 to
3.7 MB each, 250 MB for all 433 if you drive the whole country) are fetched from `raw.githubusercontent.com` **at one
pinned commit** of the dataset repository, checked against the size and SHA-256 in the index, and kept there. Later
cells, and later sessions, read them from disk (and keep working offline). The index is the pinned one and is not
refreshed: new data arrives with a new release of this project (see "Updating the data"). Nothing changes without the
variable, and nothing changes outside Poland.

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
  serves the other source's cells. `MapData.versions()` carries `pl_lod1` only while it is on: `{version, commit,
  index}`, constants of this code (`VERSION`, `COMMIT`, `INDEX_SHA256`), so the key is the setting and the pin, never
  whether the index has arrived yet.
- `src_dataset` is `GUGiK LOD1 2024`. `buildings.TRUSTED_FOOTPRINTS` lists it beside OpenStreetMap, so the surveyed
  outlines are not run through the machine-learned-footprint filter (`_ml_false`: buildings on roads, bridges, water).

## How the files are found and fetched

- **Pinned.** `polandlod1.COMMIT` is a commit of [maalos/LOD1_2024_PL](https://github.com/maalos/LOD1_2024_PL) (never a
  branch) and `INDEX_SHA256` is the SHA-256 of its `index.npz`. The index is only accepted (from the network or from
  the cache folder) when it hashes to that value, so whoever controls the repository's branches decides nothing for
  players. The index in turn lists every file's size and SHA-256, and a download is only kept when it matches.
- `index.npz` (built by `tools/build_polandlod1_index.py` from a clone, and committed there) holds every file's name,
  size, SHA-256 and building count, and the rectangle of each of the 65,745 tiles with the file and block it sits in.
  So "is this cell covered?" is answered without any file on disk. **Every file name in it must match
  `[0-9]{4}_[A-Z]-[0-9]{2}\.bld`** (ASCII, no path, no drive, no separator), or the whole index is refused: a name goes
  into a local path and, quoted (`urllib.parse.quote`), into the URL.
- The files are `https://raw.githubusercontent.com/maalos/LOD1_2024_PL/<COMMIT>/bld/<name>` (what
  `github.com/maalos/LOD1_2024_PL/raw/<COMMIT>/bld/<name>` redirects to; the request goes there directly, through
  `netfetch`, which does not follow redirects).
- Files are kept as `<name>.<first 16 hex of their SHA-256>`, so a file that changed without changing its size is never
  read with another version's tile table; older files of the same name are removed when a new one is fetched.
- Downloads go through `netfetch`'s lane `lod1` of `raw.githubusercontent.com`, which has a policy of its own (600 a
  day soft, 1000 hard, 2 at once, prefetch allowed) apart from the update check's 24 a day on the same host. The lane
  is **shared** between the server's processes (one bucket and one daily budget for road and block workers together)
  and **not optional**: a Polish cell, and the road tiles whose widths read the buildings of the cells around them,
  wait for these files, so while the host is down the status line says so. `SOURCES.md` lists it.
- **A cell's source is decided once.** `polandlod1.resolve(key)` answers "is this cell LOD1's?" and `MapData` uses
  that one answer for the cache path (`+pllod1.<VERSION>`) and for the build, so a cell is never stored under the other
  source's path. Three states: not ours (option off, not wholly in Poland, or no tiles there: Overture, always), ours
  (LOD1), and *not yet known* (wholly in Poland, the index not on disk yet): that cell's build **waits**
  (`resolve` fetches the index, and raises netfetch's errors when it cannot; a failure is remembered for a minute so the
  cells behind it do not each wait out a request). Overture never serves a Polish cell meanwhile.
- **No network where it is not wanted.** `MapData.versions()` and `path()` run for every tile key in the world; they
  call `index()`, `covers()`, `signature()` and `routes_to_lod1()`, which read the disk at most (the index is hashed
  once) and never fetch, and no lock is held over a request (`_fetch_lock` is taken only by builds of Polish cells that
  need the index).
- A *file* download that fails (host down, budget, `--offline`, size or hash not as the index says) raises netfetch's
  error: the cell is not built and not cached, and is asked again later.
- A file is written whole (`.part`, then rename); one fetch per file however many cells wait on it.
- **Bounds.** What is parsed is bounded: a block inflates to at most 8 MiB (`zlib.decompressobj(-15)` with a limit, and
  an unfinished or larger stream is refused), at most 4,096 blocks and 65,536 tiles in a file, 250,000 buildings in a
  block (checked before anything is allocated), varints of at most 10 bytes, and every ring is checked against the
  length of the stream it reads. (Real data: at most 422 tiles, 17 blocks, 15,000 buildings and 0.33 MB inflated.) A
  file that still does not parse is one `netfetch.BadResponse` naming the file, logged once; the cells that reach it
  fail the same way without decoding it again.
- **Border test.** A cell is "wholly in Poland" when its four edges, sampled every 10 m (the borders are simplified to
  about that), and a 9 x 9 grid inside it are all in Poland (the bundled borders have only a point lookup, so this is as
  fine as their own detail). Unknown borders (not downloaded yet) count as "not Poland" and are not remembered.

## Updating the data

A data update is one reviewed PR: rebuild `index.npz` in the dataset repository (`tools/build_polandlod1_index.py`),
commit it with the files, then here set `COMMIT` and `INDEX_SHA256` (the tool prints the latter) and bump `VERSION`. The
cell files (`+pllod1.<VERSION>`) of the old `VERSION` stay on disk until the cache is cleaned; the downloaded files of
the old pin are replaced as counties are visited. Mirroring the files to the project's own data repository or CDN
(`docs/HOSTING.md`, "Weekly data mirror") would replace the GitHub raw host, and the 250 MB a player might fetch, and
only changes `BASE_URL`; not done yet.

## Files

- `server/polandlod1.py`: the pin, the index and downloader (`Manifest`, `Index`), the block decoder, the cell
  decision (`wanted`, `covers`, `resolve`) and query; numpy and the deterministic maths (`dmath`).
- `tools/build_polandlod1_index.py`: builds `index.npz` for the dataset repository.
- `server/pl1992.py`: EPSG:2180 to WGS84 and back in numpy and `dnp` (Transverse Mercator, Kruger series, error below
  0.001 mm), so no pyproj. It uses `dnp`'s sin / cos / sinh / cosh / arcsin / arctanh / arctan2 (and `dmath` in the
  decoder's rotated rings), never numpy's or `math`'s: these coordinates feed building meshes and road widths, hence the
  plans players share, and must be bit-identical on every platform (GitHub #1169, `tests/test_determinism.py`).
- `server/mapdata.py`: `polandlod1.configure` in `MapData.__init__`, the one decision in `layer_arrays` /
  `predownload` passed to `path()` and `_build`, `versions()`, the index's background fetch in `warm_manifests` and
  the credit line.
- `server/netfetch.py`: the `lod1` lane's policy (shared, not optional) and role text (a lane may have a policy of its
  own, keyed `host#lane`).
- `server/tests/test_polandlod1.py`: format, projection, bounds, index names, cell, download, the one-decision rule in
  `MapData` and the lane's policy (a small file written by the test itself and a fake fetcher: no network).

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
- **Decoder speed (follow-up PR).** `decode_block` is Python per building and per ring: about 1 s for the first cell of
  a block of tiles (a block holds up to a few dozen tiles), then milliseconds for its neighbours (4 decoded blocks are
  kept). The project's rule is that compute-heavy server code goes into `native/earthfast` (Rust), and the 500 mph
  keep-up goal cannot afford a 1 s CPU wait on a new county. The plan: one kernel taking the inflated block, the grid
  steps and the tiles' origins and counts, and returning exactly the arrays `Block` holds (the Python decoder stays as
  the test oracle until the kernel is trusted, then goes, as the other kernels' twins did), with the same bounds as
  `MAX_*` above and the rotated rings' cos / sin from `dm.rs`, so the bits are the same on every platform; the
  projection to lon / lat (`pl1992`) can go in the same kernel.
