# gcpatch: engine patches for BeamNG.Earth (Linux preload library; Windows: `enginepatch.py`, `enginepatch_terfast.py`)

One preload library, `libbngearth_gcpatch.so`, with four independent patches. Each acts only on the BeamNG.Earth
map, has its own off switch and its own status file in the game's user folder `temp/`:

| patch | source | what it does | off switch | status file |
|---|---|---|---|---|
| grass | `bngearth_gcpatch.c` | keeps the grass when the physics block moves | `BNGEARTH_GCPATCH_OFF=1` | `beamngEarth_gcpatch.txt` |
| texkeep (opt-in) | `bngearth_texkeep.c` | keeps the terrain's packed textures across material rebuilds | on with `BNGEARTH_TEXKEEP=1` | `beamngEarth_texkeep.txt` |
| terfast | `bngearth_terfast.c` | a terrain block swap's cell scans without their per-sample divisions | `BNGEARTH_TERFAST_OFF=1` | `beamngEarth_terfast.txt` |
| dtsmooth | `bngearth_dtsmooth.c` | steady game speed: no catch-up burst after a hitch, no drift between | `BNGEARTH_DTSMOOTH_OFF=1` | `beamngEarth_dtsmooth.txt` |

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

## terfast: the block swap's cell scans without their divisions
A `.ter` swap (`TerrainBlock::setFile`) rebuilds the block's cell tree on the main thread (`TerrCell::init`). For
every 64 x 64 leaf cell the engine scans 65 x 65 samples three times: `TerrCell::_updateBounds` (the cell's box from
the heights), `TerrCell::_updateMaterials` (which layers it holds) and a loop at the start of
`TerrCell::_updateVertexBuffer` (does it hold a hole sample). Each loop computes every sample's index as
`(y % size) * size + (x % size)` with two 32-bit divisions, and the bounds loop stores the box after every sample.
perf on a Steam Deck (2026-11-13, a 45 mph Baltimore drive, 512² blocks at 2 m): of a swap's 23 ms in `setFile`,
6.9 ms are the bounds loop, 5.6 ms the layer loop and 6.1 ms `_updateVertexBuffer`; the parse of the file itself is
~1 ms. On Windows (1024² blocks at 1 m, Ryzen 5 3600) the swap call is 53 ms.

terfast replaces the two functions and jumps over the third loop, computing the same results row by row (the wrap
resolved once per column, the heights' minimum / maximum as integers, the engine's own float coordinate arithmetic
for the hole scan): the same box, radius, layer mask and hole flag, bit for bit.

- **Linux** (`bngearth_terfast.c`, BeamNG.drive 0.40.4, the 2026-11-06 executable): both functions and the engine
  functions the replacements call are compared by hash at startup, the vertex-buffer function whole; any difference
  and nothing is patched. The functions' entries get a 14-byte jump before the game's code runs (the displaced
  instructions run from a trampoline when the stock function is wanted); the hole loop's first instruction gets a
  5-byte jump to a stub near the game's code.
- **Windows** (`enginepatch_terfast.py`, the launcher, as dtsmooth below): the three functions are found by their
  whole code (call targets and rip-relative displacements masked, one match each, the constants they load checked);
  the block holds `win/terfast_win.c` compiled to Win64 machine code (`win/build_terfast.sh` ->
  `enginepatch_terfast_code.py`), a stub (`win/terfast_stub.asm`) and two trampolines. The game's threads are
  suspended for the three writes and none may be inside the bytes replaced (GetThreadContext). The Windows build
  keeps the height scale at another offset and reaches the TerrainFile through another field of the resource's
  holder; the cell layout is the same.
- The stock code runs outside the BeamNG.Earth level, and for any input the fast path does not expect.
- `BNGEARTH_TERFAST_CHECK=1`: both run and the status file counts differences. Checked 2026-11-13: Steam Deck, a
  Baltimore drive (1,696 leaf cells' bounds and holes, 4,304 layer scans) 0 differences; Windows test PC, 31 swaps of
  1024² blocks (7,936 cells) 0 differences. `win/test_terfast.sh` runs the Windows code on Linux on stand-in cells
  against the loops written out sample by sample; `server/tests/test_enginepatch_terfast.py` covers the search and
  the block layout.
- Measured (the same build, the patch switched by its environment variable / its state): 
  - Steam Deck (512² blocks at 2 m, 30 fps cap, Fells Point -> Dundalk at 45 mph, alternating two real blocks for
    the swap alone): `setFile`'s main-thread CPU 22.9 -> 1.9 ms a swap (perf); the swap call 18.4 -> 11.3 ms (what is
    left is mostly the wait for the GPU device, ~4.5 ms, and the mod's own Lua); in the drive the swap's frame
    62 -> 54 ms and the mod's `world` part 24.9 -> 18.8 ms. The drive's totals did not move (frames over 50 ms
    60.5 -> 74.2 a minute, 1 % low 13.0 -> 12.9 fps: other hitches and run-to-run noise are larger).
  - Windows test PC (1024² blocks at 1 m, Phoenix 8.5 km at 45 mph, one session, the patch switched off then on):
    the swap call 53.0 -> 30.7 ms, its two frames 61 + 56 -> 41 + 37 ms (30 swaps standing still); in the drive the
    swap's frame 65.7 -> 46.0 ms, `world` 54.0 -> 34.0 ms, the worst frame per swap 66 -> 51 ms (max 120 -> 68),
    frames over 50 ms 10.9 -> 6.2 a minute, over 100 ms 3 -> 1, p99.9 61.6 -> 51.1 ms, 0.1 % low 13.0 -> 15.2 fps
    (average fps 105.6 -> 93.2 with more of the world loaded in the second run; the 1 % low 26.9 -> 26.6).
- Off: `BNGEARTH_TERFAST_OFF=1` (both), `BNGEARTH_TERFAST_WIN=0` (Windows only).

What it does not touch: the rest of `setFile` (the vertex / index buffer set-up per cell, the height texture) and the
frame after the swap, where the engine rebuilds the terrain's material textures (texkeep below).

## texkeep: terrain textures kept across block swaps
**Opt-in (`BNGEARTH_TEXKEEP=1`).** Ported to the 2026-11-06 executable on 2026-11-13, with the 2026-11-03 review's
deadlock paths removed: no probe of the device mutex's owner in glibc's layout (releases inside the pack loader's
calls run under the engine's lock, as before; the kept textures of an ended level are let go by the watcher thread,
2 s after the flag went, never from another caller of createTexture). Still open before it can be on by default: a
teleport soak (a crash at a teleport's teardown was seen with it on, 2026-11-05, cause unknown).
Steam Deck, 2026-11-13, the same drive with terfast on: 995 kept-pack hits to 51 loads; the frame after a swap
50.5 -> 25.9 ms (its Render 35.6 -> 13.0 ms), the rebuild's main-thread CPU 23.6 -> 1.5 ms (perf), the swap's two
frames over the ordinary frame 49 -> 20 ms, the worst frame per swap 67 -> 53 ms (max 221 -> 55).

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

## dtsmooth: steady game speed
BeamNG moves the world each frame by dtReal, a smoothed frame time (FrameTime::update). Its filter tracks the frame
time slowly (a 13-40 frame time constant) and keeps a "time debt": while our streaming makes frame times uneven the
game runs at 0.84-1.19x speed over quarter seconds (a stock map: 0.99-1.01x), and after a hitch it replays the whole
hitch at up to ~2x for ~0.5 s (the "pulse"). dtsmooth runs its own filter (`bngearth_dtfilter.h`) once a frame right
after the engine's and overwrites dtReal:

- dtReal = the mean of the last 3 frame times + an error term that keeps game time on the clock (game time follows
  real time within a few frames: no slow drift, no catch-up burst);
- a hitch (a frame longer than the last 16 ordinary frames' longest + max(10 ms, the mean); at most 3 in a row, more
  is a new frame rate) enters as an ordinary frame, its excess goes to a debt paid back at 10 % (no frame runs faster
  than 1.1x for it; at most 0.5 s owed, beyond that dropped);
- outside the BeamNG.Earth level, during an OpenXR session and for any input it does not expect it leaves the
  engine's dtReal alone. The raw frame time Lua sees is unchanged.

The hook: FrameTime::update ends in a tail call (the MainLoopTimer add, xmm0 = raw) after it stored dtReal; that
jump's rel32 is pointed at a 14-byte stub in a page mapped within +-2 GB of the game's code, which jumps to a
trampoline in this library (argument registers kept) that calls the filter and goes on to the original target. One
4-byte write at startup, before the game's code runs. The function is found by its instructions (displacements
masked, one match only) and its end by its own (the same dtReal stored, the OpenXR test); in any other shape nothing
is patched.

**On by default** (2026-11-11). `BNGEARTH_DTSMOOTH_OFF=1` or `BNGEARTH_DTSMOOTH=0` turns it off. Tests:
`BNGEARTH_DTSMOOTH_REPAY` / `_HITCH` (s) / `_TAU` / `_N` / `_MODE=2` (the engine's own filter re-implemented with the
2026-11-03 cap); `BNGEARTH_DTSMOOTH_CTL=1` reads `temp/beamngearth_dtsmooth_ctl` every 250 ms
(`mode hitch_abs hitch_rel tau repay debt_max n [top_n]`), which `tools/framerec.py` uses to cycle variants every 20 s
within one drive (the same streets and moment for each).

Measured 2026-11-11 (PC, Linux, Manhattan "uptown" at 35 mph, the ball prop, variants cycled every 20 s in one drive,
~2 min each; the PC was loaded by other test jobs at the time, load average ~40 on 16 threads, so ~45-50 fps with
many slow frames: a hard case). Game speed = game time / real time over 0.25 s windows (1.00 = steady):

| variant | fps | speed p5 | p95 | p99 | game / real | frames > 50 ms / min |
|---|---|---|---|---|---|---|
| 1.9 default: stock filter + frame pacer (P70) | 38 | 0.76 | 1.23 | 1.37 | 0.986 | 67 |
| stock filter, no pacer | 49 | 0.74 | 1.23 | 1.52 | 0.987 | 108 |
| dtsmooth (this filter), no pacer | 48 | 0.73 | 1.11 | 1.16 | 0.979 | 103 |
| dtsmooth + pacer P70 | 46 | 0.77 | 1.11 | 1.15 | 0.973 | 102 |
| dtsmooth + pacer P50 | 49 | 0.73 | 1.11 | 1.14 | 0.973 | 102 |

The fast side (the post-hitch catch-up and the drift between hitches) goes from up to 1.5x to 1.1x; the frame pacer
adds nothing to that once dtsmooth runs and costs frames, so the mod no longer paces where dtsmooth is applied
(framepacer.lua). The slow side (p5) is the hitches themselves (a frame you never see cannot move the world), for
the hitch work to fix. Other variants of the same drives: the 2026-11-03 cap 0.59-1.18 (game time 0.94, the hitches'
time dropped); this filter dropping instead of repaying 0.74-1.05 (game time 0.956); passing hitches through
(no cap) 0.82-1.05 but 2-5x bursts for a few frames and time lost to the engine's 50 ms physics step cap. On the
lightly loaded 2026-11-03 recordings (120 fps) the same filter simulated gives 0.94-1.11 against stock's 0.79-1.22.
Engine facts found on the way: physics run 2 frames behind (dtSim of frame k = dtReal of frame k - 2) and never step
more than 50 ms in a frame (a longer dtReal loses the rest).

## Windows: the launcher patches the running game (`enginepatch.py`)
Windows has no LD_PRELOAD. The launcher (`enginepatch.py`, imported by `launcher.py` on Windows and under Wine)
waits for `BeamNG.drive.x64.exe`, opens it with the documented debugging API (OpenProcess with VM read / write /
operation rights) and:

1. reads the executable's `.text` from memory and finds the inlined filter by its instructions (the same masked-
   signature rule: one match, the end checked against the same dtReal / dtRaw / OpenXR globals; 0.40.4 builds of
   2026-10-24 and 2026-11-06 both match: the MainLoopTimer call at VA 0x1407b2294 in the current one);
2. allocates one 8 KB block within +-2 GB of that call (VirtualAllocEx): the trampoline (`win/dtsmooth_tramp.asm`:
   keeps every volatile register, Win64 ABI), the same filter compiled to Win64 machine code
   (`win/dtsmooth_filter.c`, gcc `ms_abi`, freestanding, rip-relative only), then on a read-write page the original
   callee's address and the filter's state; the code page is made execute-read;
3. suspends the game's threads, points the call's rel32 at the block (VirtualProtectEx + WriteProcessMemory +
   FlushInstructionCache), resumes them;
4. follows the level flag (`temp/beamngearth_gcpatch_on`) every 250 ms and writes the state's `enabled` field.

The machine code is in `enginepatch_code.py`, generated by `win/build_blob.sh` (gcc, ld, objcopy, nasm);
`win/test_blob.sh` checks the file matches a fresh build and runs the code on Linux behind a stand-in call site
(every argument register reaches the callee unchanged, every non-volatile one survives, dtReal bit-identical to the
Linux filter). `server/tests/test_enginepatch.py` covers the signature search and the block layout.

**On by default on Windows since the check on the Windows test PC (2026-11-11, below). Off: `BNGEARTH_DTSMOOTH_WIN=0`**
(or the off switches above). terfast is applied the same way by `enginepatch_terfast.py` (its section above). Not
ported: texkeep (a vtable hook with its own texture cache and the engine's locks; it needs the Windows texture manager
reverse-engineered and a DLL, and on Linux it is opt-in).

**Checked on Windows (2026-11-11, the test PC: Ryzen 5 3600, RTX 3060, Windows 26300, BeamNG 0.40.5 Windows build
of 2026-11-06, the dev app from source):** `--scan` found the call at VA 0x1407b2294 (the same exe as the PC's copy);
the launcher patched the main game process (block next to the image at 0x7ff738df0000, the call at 0x7ff7348b2294
under ASLR; the five CEF helper processes of the same executable left alone: pick_main), status `ready` -> `applied`
in the level -> `stock: another level (39352 frames, 410 capped, 18.61 s dropped, 19.16 s repaid)` at a level reload
-> `applied`. dtReal followed the new filter (3-frame mean) against the stock filter's slow average when switched.
Interleaved Manhattan drives (20 s segments, ~2.7-3.4 min per variant):

| drive | variant | fps | speed p5 | p95 | p99 | game / real |
|---|---|---|---|---|---|---|
| uptown, 8 km | 1.9 default (stock + pacer P70) | 42.6 | 0.73 | 1.22 | 1.43 | 0.971 |
| | stock, no pacer | 49.4 | 0.76 | 1.23 | 1.39 | 0.989 |
| | dtsmooth, no pacer | 44.6 | 0.62 | 1.13 | 1.20 | 0.960 |
| downtown, 10 km | stock, no pacer | 46.8 | 0.81 | 1.26 | 1.41 | 0.993 |
| | dtsmooth (repay 10 %, the default) | 47.9 | 0.83 | 1.11 | 1.13 | 0.999 |
| | dtsmooth dropping hitches (repay 0) | 45.3 | 0.77 | 1.03 | 1.07 | 0.961 |

(The uptown drive's dtsmooth segments drew more of its long hitches, 17.8 a minute over 100 ms against 9-13: its slow
side; the downtown drive's were even, 5.6-7.1.) Repaying keeps game time on the clock (0.999) where dropping loses 4 %
and slows the p5 windows; the repay's 1.1x shows as the p95. Renderer soaks with the default filter: DX11 the two drives above + a teleport each + a level reload (~25 min), DX12 6 min (midtown, 68 fps, speed 0.84-1.11, p99 1.13), Vulkan 6 min (67 fps, 0.83-1.11, p99 1.14): no crash, no Windows application error, the patch applied in each. Microsoft Defender
(real-time and behaviour monitoring on): no detections, no protection-history entries, only routine events in its
log (signature updates, boot) during the test; that was the source app (python.exe), not the packaged .exe.

**Checked under Proton (2026-11-11, PC):** the Windows build (0.40.4, `-gfx dx11`, Proton 10) in its Steam prefix,
`enginepatch.py --attach` run by a Windows Python (the embeddable 3.12 package) in the same prefix: it found the
call, wrote the block next to the image, hooked the running game and followed the level flag ("applied" in the
level, "stock" in the menu); dtReal followed the new filter in the game's own frames (the 3-frame mean against the
stock filter's slow average, switched live by the test control file) through a Manhattan drive without a crash.
Same drive as the Linux table (variants cycled every 20 s, ~3 min each; Proton on the loaded PC: ~30 fps, 40-50
frames over 100 ms a minute, so the slow side and game / real time are the hitches'): game speed p95 / p99 1.16 /
1.30 for the 1.9 default (stock + pacer P70, 27.7 fps), 1.18 / 1.31 stock without the pacer (30.3 fps), 1.09 / 1.11
with this patch and no pacer (31.6 fps).
What Proton cannot prove: Windows' own memory manager and code-integrity rules (Wine maps and protects pages its own
way), the antivirus reaction to a launcher writing into another process (Defender / SmartScreen and other scanners:
the .exe is unsigned or self-signed), Windows' thread suspension and timer (QueryPerformanceCounter) behaviour, and
the DX12 / Vulkan builds.

**Steps for a Windows check** (after a game update or a change to the patch; the test PC: `.claude/devtools/winstop.sh`, `winsync.sh`, then `schtasks /run /tn bnge_app`;
see the windows-test-pc notes):
1. Offline, any copy of the game: `python enginepatch.py --scan "C:\...\BeamNG.drive\Bin64\BeamNG.drive.x64.exe"`
   prints the call's address and the globals (or why the build does not match).
2. Start the app (for A/B runs with `set BNGEARTH_DTSMOOTH_CTL=1` in `run_dev.cmd`), load BeamNG.Earth. `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\temp\beamngEarth_dtsmooth.txt` should read
   `ready: applied ... (launcher patch, block at 0x..., call at 0x...)`, then `applied: steady game speed` in the
   level and `stock: another level (... frames, ... capped ...)` back in the menu; the app's console shows the same.
3. With `BNGEARTH_DTSMOOTH_WIN=0` it must say `off (BNGEARTH_DTSMOOTH_WIN=0)`; with `BNGEARTH_DTSMOOTH_OFF=1`, `off`.
4. A/B: `.claude/devtools/wintunnel.sh`, `BNGE_MCP_URL=http://127.0.0.1:29393/mcp python3 tools/framerec.py run --out
   OUT --set final` from the Linux PC (the CSV lands in the Windows user folder's `temp/`: copy it with scp and run
   `framerec.py analyze`). Expect the p95 / p99 drop the Linux table shows.
5. Soak: a 30 min drive in each renderer (`--gfx dx11 / dx12 / vk`), a teleport or two, a level reload, a BeamMP
   join: no crash; then Defender's protection history (antivirus uploads of the .exe only with the user's go).
6. (Done 2026-11-11: `env_on_windows()` defaults to on; the off switches stay.)

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
`beamngEarth_terfast.txt`, `beamngEarth_dtsmooth.txt`).
It contains one line, such as `applied: grass kept across terrain block moves (BeamNG.Earth level)`,
`stock: another level`, or the reason it did nothing.

## Turn off
Remove the launch option (all patches), or add `BNGEARTH_GCPATCH_OFF=1` / `BNGEARTH_TEXKEEP_OFF=1` /
`BNGEARTH_TERFAST_OFF=1` / `BNGEARTH_DTSMOOTH_OFF=1` in front of it (one patch). `python3 launcher.py --no-gcpatch` starts the game without the
library.
