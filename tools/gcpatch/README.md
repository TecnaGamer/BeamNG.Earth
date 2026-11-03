# gcpatch: engine patches for BeamNG.Earth (optional, Linux)

One preload library, `libbngearth_gcpatch.so`, with three independent patches. Each acts only on the BeamNG.Earth
map, has its own off switch and its own status file in the game's user folder `temp/`:

| patch | source | what it does | off switch | status file |
|---|---|---|---|---|
| grass | `bngearth_gcpatch.c` | keeps the grass when the physics block moves | `BNGEARTH_GCPATCH_OFF=1` | `beamngEarth_gcpatch.txt` |
| texkeep | `bngearth_texkeep.c` | keeps the terrain's packed textures across material rebuilds | `BNGEARTH_TEXKEEP_OFF=1` | `beamngEarth_texkeep.txt` |
| dtsmooth | `bngearth_dtsmooth.c` | no fast-forward of game time after a hitch | `BNGEARTH_DTSMOOTH_OFF=1` | `beamngEarth_dtsmooth.txt` |

## Grass: keep the grass when the physics block moves

BeamNG.Earth moves its hidden physics TerrainBlock under the player every ~128 m. The engine
then frees every grass cell on that block. The mod hides this by regenerating the grass in the
same frame, at the cost of one slower frame per move (docs/ARCHITECTURE.md "Ground surfaces").
This preload library removes the cause instead. It changes one byte of the engine's GroundCover
terrain-update handler in the game's memory at startup, so that height and layer updates no longer
free grass. The mod still rebuilds the grass itself where the world really moves: teleports and
floating-origin rebases.

- **Written for BeamNG.drive 0.39.4, Linux x86-64.** It checks the handler's first 54 bytes at its
  0.39.4 address. In another build (a game update) it searches the game's code for the same bytes, with
  the frame size and the two jump distances masked, and patches only a single match. If the handler
  changed, it does nothing (the status file says so).
- Only on the BeamNG.Earth map. The mod creates `temp/beamngearth_gcpatch_on` in the game's user folder when its
  level starts and removes it when the level ends; the library looks for that file every 250 ms and puts the byte
  back to stock without it, so other maps (a terrain edited in the World Editor) keep the stock behaviour.
- Nothing on disk changes. Without the launch option the game is stock.
- It acts only in the main `BeamNG.drive.x64` process, not in the CEF helpers or Steam.
- Unsupported by BeamNG. A wrong patch crashes the game, which a restart without the launch
  option fixes.

## texkeep: terrain textures kept across block swaps
Every terrain block swap (a new `.ter`) and every base-photo refresh (`updateMaterial`) makes the engine rebuild the
terrain's material data on the next frame. For each ground material it asks the texture manager again for six packed
textures (`temp/art/terrainmaterialcache/<hash>.dds`) and creates each one again from its file (a Vulkan image and an
upload), although only the block's photo pack changed. texkeep replaces the texture manager's `createTexture` in its
virtual table: for the terrain pack loader's call (after the engine has checked that the cache file is up to date) it
returns the texture it kept from the last rebuild of the same file, else it calls the stock function and keeps the
result. A texture not asked for in the last two rebuilds goes (an old block's photo), and so do the least recently used
beyond 96 textures / ~160 MB; all go when the level ends.

- **Written for BeamNG.drive 0.40 Linux (the 2026-10-29 executable).** It compares the code and data it relies on
  byte for byte at startup (the vtable slots, `createTexture`, both pack-loader call sites, the texture release path).
  In any other build it does nothing and says so in its status file.
- Measured (PC, I-10 Phoenix at 120 mph, detail mode, 8 ground materials, two drives each way): the frame after a
  block swap 60.8 -> 51.8 ms median (n 74 / 68), its Render time 21.6 -> 13.0 ms; a base-photo refresh
  (`terrain.setBase`, 8 slots) 83 -> 58 ms, its Render time ~31 -> ~7 ms. The Lua part (the `.ter` parse, ~30 ms; the
  synchronous `updateMaterial`, ~45 ms) is unchanged.
- `BNGEARTH_TEXKEEP_DRY=1`: installed but never hands back a kept texture (counts and times the stock calls);
  `BNGEARTH_TEXKEEP_LOG=1`: one line per terrain texture request in `temp/beamngEarth_texkeep.log`.
- A kept texture whose file goes (the server prunes an evicted block's photo pack) makes the engine log
  `Failed to create resource: /TEMP/ART/TERRAINMATERIALCACHE/<hash>.DDS` (it tries to reload it); with the old photos
  kept until the size cap this happened about once per 10 km drive, hence the two-rebuild rule.

## dtsmooth: no fast-forward after a hitch
BeamNG advances game time by a smoothed frame time (dtReal) that keeps a "time debt": after a hitch it repays the
whole hitch, so the game runs up to ~2x fast for a few dozen frames (a pulse at speed). dtsmooth rewrites 95 bytes of
that filter (FrameTime::update) once at startup so that the filter sees min(frame time, previous dtReal + C),
C = 10 ms: a hitch enters it as at most +10 ms and its excess is dropped instead of replayed. The raw frame time Lua
sees is unchanged. The constant sits in the patched code; C = 2 s computes exactly the stock result, which is what it
holds outside the BeamNG.Earth level (one 8-byte store switches it). `BNGEARTH_DTSMOOTH_C=<seconds>` sets C for tests.

- Written for BeamNG.drive 0.40 Linux; in another build it looks for the same instructions (addresses masked), builds
  the patch from that build's addresses and patches only a single match, else nothing.
- Measured (PC, Manhattan "uptown" at 35 mph, 60 s per case, frame pacing off / on): game time repaid in the 60
  frames after a hitch +75 / +76 ms -> -83 / -58 ms (none); game speed over 0.25 s windows p99 1.48 / 1.50 ->
  1.28 / 1.26. Not fixed by it: the spread between hitches (p10 0.87-0.93, p90 1.09-1.13), which comes from the
  frame time itself varying under the filter's slow tracking. Game time over a minute: 0.987 -> 0.963-0.967 of real
  time (the hitches' dropped time).

## Install
The Linux download (the AppImage) and `python3 launcher.py` start the game with this library already preloaded:
nothing to install. By hand, for a game started from Steam:
```sh
tools/gcpatch/build.sh
```
Then go to Steam > BeamNG.drive > Properties > Launch options and enter the line that
`build.sh` prints:
```
LD_PRELOAD="/path/to/tools/gcpatch/build/libbngearth_gcpatch.so:$LD_PRELOAD" %command%
```
Then restart the game.

## Check
After the game starts, look at
`~/.local/share/BeamNG/BeamNG.drive/current/temp/beamngEarth_gcpatch.txt` (and `beamngEarth_texkeep.txt`,
`beamngEarth_dtsmooth.txt`).
It contains one line, such as `applied: grass kept across terrain block moves (BeamNG.Earth level)`,
`stock: another level`, or the reason it did nothing.

## Turn off
Remove the launch option (all patches), or add `BNGEARTH_GCPATCH_OFF=1` / `BNGEARTH_TEXKEEP_OFF=1` /
`BNGEARTH_DTSMOOTH_OFF=1` in front of it (one patch). `python3 launcher.py --no-gcpatch` starts the game without the
library.
