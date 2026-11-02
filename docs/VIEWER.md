# BeamNG.Earth viewer (`native/viewer`)

A browser view of exactly what earthd sends the game: the far field with its imagery, the physics
block's heightmap, the road meshes, sidewalks, buildings, decks and far buildings. It also overlays
the OSM ways and Overture connectors and has a fly camera and a minimap that teleports on click.

## How it gets the same bytes

The viewer does not generate anything. It is one more client of earthd (protocol 4,
`server/README.md`), like the mod:

- `bnge-viewer` (Rust) bridges the page's WebSocket `/ws` to an earthd connection (one per page).
  Messages pass through unchanged.
- The page plays the mod's part: `hello` (with `geoTiles`, `roadGeo`, `farGeo` and `globalFar`, as
  the mod), `prepare_anchor` / `commit_anchor`, `prepare_terrain` / `commit_terrain`, and poses at 2 Hz.
  earthd builds for it what it builds for the game and writes the files into the game's temp folder.
- `/data/<session>/<file>` serves those files byte for byte (`tests/parity.mjs` checks the SHA-256
  against the files on disk). The page decodes them itself: `.cdae` (`web/cdae.js`, a port of
  `server/cdae.py`'s reader), DDS (three.js), and the 16-bit heightmap PNG (`web/png16.js`).
- Placement is the mod's: `web/geodesy.js` is a line-for-line port of `geodesy.lua` and `geotile.lua`.
  Global far tiles, road geotiles and far geotiles are placed rigidly at their pivots, and anchor
  tiles sit in their anchor's world.
- Pooled shape slots (`slot_<n>.cdae`, `server/matslots.py`) are reported gone in the next pose as
  soon as their file has been read, so the viewer never holds a slot the game might need.

Not drawn: vegetation, street furniture rows, gas station props, signs. The game's materials are
not loaded either: shapes are coloured by material name, and buildings keep their vertex colours.

## OSM overlay

`/api/osm?z=14&x=&y=` reads the cell straight from earthd's map-data mirror
(`<cache>/mapdata-mirror/v2/overture/<release>/roads.<n>/14/x/y/{ok,pack.bin}`, `src/osm.rs`; it
only reads, and never fetches). Where the mirror has no valid copy (on Windows a folder can be left with a
`pack.bin` and no `ok` marker), it reads the compressed cache's `.npz`, as earthd does. It returns every road way with its tags (Overture class and the OSM
`src_record` way id, lanes, width, maxspeed, surface...) and rules, plus the connectors. A cell not
in the mirror yet shows as "not cached": earthd builds it when it plans roads there.
`tests/osm_golden.py` compares the output with `mapdata._unpack`.

## Run

1. Start earthd as usual (`run_server.bat` / `run_server.sh`, or `python3 server/earthd.py ...`).
   The game does not need to run, and it may: the viewer is a separate connection.
2. `run_viewer.bat` / `run_viewer.sh` (builds with cargo the first time), or:

   ```sh
   cd native/viewer && cargo run --release -- [--earthd 127.0.0.1:47613] [--port 47700] [--user-dir DIR] [--cache-dir DIR]
   ```

   `--user-dir` defaults to `start.py`'s `.beamng_earth.json`. `--cache-dir` defaults to the
   launcher's choice, else `~/.cache/beamng-earth`.
3. Open http://127.0.0.1:47700/ and click the minimap, or type `lat, lon`. `#lat,lon` in the URL
   teleports on load, and the last place is remembered.

Controls: drag to look, WASD to move, E / Space up, Q / C down, Shift x5, Ctrl x0.2, wheel for
speed. Click an OSM way or node, or any shape, to inspect it.

## Checks

```sh
cd native/viewer && cargo test --release
node native/viewer/tests/parity.mjs 34.0002 -118.041          # earthd and the viewer running
.venv/Scripts/python native/viewer/tests/osm_golden.py 2819 6545
```
