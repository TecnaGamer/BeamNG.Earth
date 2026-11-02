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

Not drawn: vegetation, gas station props.

Materials: the mod's own definitions (every `*.materials.json` under its `levels/` folder, merged by
`/api/materials`) are used as the game uses them wherever a material needs nothing outside the mod
folder. That covers base colour, mod-folder textures (served by `/game/`), the opacity map with alpha
test, and vertex colours. The signs' lettering is the mod's opacity atlas, `sign_letters_o.data.png`.
Materials whose base colour texture is one of the game's (`/assets/...`: most building materials)
are coloured by name instead, and buildings keep their vertex colours.

As the mod does after every earthd `hello`, the viewer sends `gas_stations: on` and
`highway_signs: on`, so earthd builds the same road tiles for it as for the game.

## Highway signs

Sign tiles (`roads_tile` with key `s<tx>_<ty>`, `server/signstream.py`) are drawn on their own
layer, "Highway signs". Each has up to four render-only shapes in the anchor's world: `exits` (the
green guide panels with their exit tabs), `mileposts`, `merges` and `speeds`. They use pooled slots
like the other shapes. earthd builds them only where it finds a US state, except the speed limits,
which it builds everywhere.

### Known earthd bug: sign tiles stop after a late job for an old anchor

Seen while testing the viewer: the layer panel shows "Highway signs … unavailable", and earthd logs
a `KeyError: <anchor id>` from a sign worker. From then on that connection gets no more sign tiles.

The cause is in `server/blockworker.py`, `BlockWorker.anchor()`. It keeps state for only the
`ANCHORS_KEPT = 2` highest anchor ids. When a job asks for an anchor it doesn't have, it creates it,
then deletes every anchor except the two highest (`sorted(self.chunks)[:-ANCHORS_KEPT]`). If the
requested anchor is older than those two, it deletes the anchor it just made. The next line,
`return self.graders[aid], self.chunks[aid]`, then raises `KeyError`.

This happens when a job for an older anchor arrives after two newer anchors exist:

- several viewer pages, or quick teleports, create anchors in rapid succession;
- the game and the viewer are connected at once. They share one anchor counter, so each client's
  anchors push the other's out.

Workaround: restart earthd. Fix (server code, not applied): don't delete the anchor being requested,
for example `for old in sorted(a for a in self.chunks if a != aid)[:-ANCHORS_KEPT]:`.

## Props (street furniture and guard rails)

The road tiles' `props` and `guardrails` rows (street lights, poles, hydrants, benches, bins, doors,
fences, signals, stop and speed signs, substations, rail crossings, guard rails, jersey and cable
barriers) are drawn as the mod's `trees.lua` makes them (`web/props.js`):

- **Shapes:** each group maps to the game's own shapes (`CATALOG`). The variant is picked by the
  row's height and rank (`pick`), and scaled to that height.
- **Placement:**
  - A geotile's rows go through its placement at the content's mean pivot (`roads.lua` `rowPivot`), and the
    placement's turn is added to the yaw. Guard rails use the basis in their row.
  - An anchor tile's rows sit in the anchor's world.
- **Visibility:** the same rules as the mod at its "normal" quality and distance: the rank rings and
  per-tile cap at the box distance from the camera, the per-group distance caps (`GROUP_MAX_M`), and
  the lamps' glow off as in daytime.
- **Where the shapes come from:** `/game/<path>.cdae` (`src/game.rs`) serves the compiled shape the
  game loads, unchanged. It looks in the engine's compiled cache (`<user-dir>/temp/<path>`), then the
  mod folder, then the game's content zips (`content/art_shapes.zip`, `content/levels/*.zip`; each
  `.dae` there ships with its `.cdae`). The game install is found from the launcher's `gameDir` or
  Steam's library folders (`--game-dir` to set it).
- **Node trees:** stock shapes have them (each object under a node with a rotation and translation),
  and the decoder applies them.
- **Markers:** the mod's own shapes ship as `.dae` only. Until the game has compiled one (it does so
  when it first loads it), that prop is drawn as a magenta marker of the item's height.

Click a prop to see its group, shape, height, scale, yaw, rank and tile. The layer panel has two
switches: "Street furniture (props)" for the `props` rows, and "Guard rails and barriers" for the
`guardrails` rows (guard rails, jersey and cable barriers).

## Night lighting

The game lights the night from its own time of day (`nightlights.lua`, switched by the stock environment's
day / night). The viewer has a **Night** switch instead (in the layer panel, remembered), and does what the mod does
after dusk (`web/lighting.js`):

- **Windows and lamp lenses:** the mod's night materials (`NIGHT_MATERIALS`, as `nightlights.lua` `M.MATERIALS`) get
  their glow colour: the buildings' facades, offices, shopfronts and skyline through their night emissive maps (from
  the mod folder), and the lamp lenses. The game's nits become three.js's emissive intensity over 200 (the windows'
  brightness: full), so the ratios between them are kept; the lenses are at full.
- **Far glow:** the `lampglow` group (each street lamp's glow, and the far and skyline tiles' street lamps,
  `lamps` in `bldfar_tile` / `bldsky_tile`, while the tile shows) is on at night only, as `trees.lua` / `roads.lua`
  have it. Its shape's finest detail level is a speck, so the viewer draws its levels itself: the balls of
  `tools/build_lamp_glow.py`, switched at the same distances, scaled to the screen's pixels per radian as the
  engine's detail sizes are.
- **Street lamp lights:** a pool of spot lights (no shadows), every 0.25 s moved to the lamps nearest the camera
  (`nightlights.lua` `nearest`: within 60 m all round, out to 900 m within 65° of the heading), at each lens, pointing
  down and tilted 12° toward the road, with the mod's range and cone angles. The pool is smaller than the mod's
  (48 / 128 / 256): each spot light costs every lit pixel in a browser's forward renderer, so the panel offers
  0 to 64, 32 by default. The brightness is chosen by eye (the engine's has no physical unit).
- **Sky and ground:** the daylight is turned down to a dim blue moonlight, and the sky goes dark.

Not lit: the gas stations' pump islands (the viewer does not draw gas station props) and the tunnels' lights
(`ambience.lua`).

## OSM overlay

`/api/osm?z=14&x=&y=` reads the cell straight from earthd's map-data mirror
(`<cache>/mapdata-mirror/v2/overture/<release>/roads.<n>/14/x/y/{ok,pack.bin}`, `src/osm.rs`; it
only reads, and never fetches). Where the mirror has no valid copy (on Windows a folder can be left with a
`pack.bin` and no `ok` marker), it reads the compressed cache's `.npz`, as earthd does. It returns every road way with its tags (Overture class and the OSM
`src_record` way id, lanes, width, maxspeed, surface...) and rules, plus the connectors. A cell not
in the mirror yet shows as "not cached": earthd builds it when it plans roads there.
`tests/osm_golden.py` compares the output with `mapdata._unpack`.

## Place search and location

As in the mod, the viewer sends earthd the messages the in-game map and status bar send (`web/geocode.js`):

- **Search box:** `geocode {id, q, lat, lon}` → `geocode_result`. It searches 300 ms after the last key
  once there are 3+ characters, biased toward the camera, with recent places listed first. A typed
  `lat, lon` (also `36.5 N, 118.2 W`) goes there on Enter. The arrow keys choose a result.
- **Location line:** `where {id, lat, lon, online}` → `where_result`. The street comes from earthd's
  map data; the town, region and country come from Photon's reverse lookup (position rounded to
  ~1 km), with `statusbar.lua`'s timing. The "Place names online" switch is the mod's
  `placeNamesOnline`.

earthd's geocoder is Photon (`server/geocode.py`), not Nominatim. At the time of writing Photon
answers HTTP 403 to earthd's `BeamNG.Earth/...` User-Agent (GitHub #602), and the search box says so.

**Debug switch:** "Geocode in the viewer" makes the page ask Photon itself. It sends the same URLs
and parses the answers as `geocode.py` does (a line-for-line port, checked against it). It sends one
request at a time, at most one a second, and caches the answers. The street still comes from earthd.
A line under the search box, and the browser console, show each request's URL, HTTP status, time
and result count; the browser's network tab has the full answers. Names are in English (earthd picks
the language from the country under the camera).

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
