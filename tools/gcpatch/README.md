# gcpatch: engine patches for BeamNG.Earth (Linux preload library; Windows: `enginepatch.py`)

One preload library, `libbngearth_gcpatch.so`, with three independent patches. Each acts only on the BeamNG.Earth
map, has its own off switch and its own status file in the game's user folder `temp/`:

| patch | source | what it does | off switch | status file |
|---|---|---|---|---|
| grass | `bngearth_gcpatch.c` | keeps the grass when the physics block moves | `BNGEARTH_GCPATCH_OFF=1` | `beamngEarth_gcpatch.txt` |
| texkeep (opt-in) | `bngearth_texkeep.c` | keeps the terrain's packed textures across material rebuilds | on with `BNGEARTH_TEXKEEP=1` | `beamngEarth_texkeep.txt` |
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

## texkeep: terrain textures kept across block swaps
**Opt-in for now (`BNGEARTH_TEXKEEP=1`):** a code review (2026-11-03) found a rare path to a permanent deadlock (the
device mutex's owner read from glibc's layout) and a lock-order risk in `release_all`; on by default once fixed.

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

**Off on Windows until checked on a Windows PC: `BNGEARTH_DTSMOOTH_WIN=1` turns it on** (the off switches above
win). Not ported: texkeep (a vtable hook with its own texture cache and the engine's locks; it needs the Windows
texture manager reverse-engineered and a DLL, and on Linux it is opt-in and written for the 2026-10-29 build only).

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

**Steps on a Windows PC** (the test PC: `.claude/devtools/winstop.sh`, `winsync.sh`, then `schtasks /run /tn bnge_app`;
see the windows-test-pc notes):
1. Offline, any copy of the game: `python enginepatch.py --scan "C:\...\BeamNG.drive\Bin64\BeamNG.drive.x64.exe"`
   prints the call's address and the globals (or why the build does not match).
2. Add `set BNGEARTH_DTSMOOTH_WIN=1` (and for A/B runs `set BNGEARTH_DTSMOOTH_CTL=1`) to `run_dev.cmd`, start the app,
   load BeamNG.Earth. `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\temp\beamngEarth_dtsmooth.txt` should read
   `ready: applied ... (launcher patch, block at 0x..., call at 0x...)`, then `applied: steady game speed` in the
   level and `stock: another level (... frames, ... capped ...)` back in the menu; the app's console shows the same.
3. Without the variable it must say `off on Windows until tested there`; with `BNGEARTH_DTSMOOTH_OFF=1`, `off`.
4. A/B: `.claude/devtools/wintunnel.sh`, `BNGE_MCP_URL=http://127.0.0.1:29393/mcp python3 tools/framerec.py run --out
   OUT --set final` from the Linux PC (the CSV lands in the Windows user folder's `temp/`: copy it with scp and run
   `framerec.py analyze`). Expect the p95 / p99 drop the Linux table shows.
5. Soak: a 30 min drive in each renderer (`--gfx dx11 / dx12 / vk`), a teleport or two, a level reload, a BeamMP
   join: no crash; then Defender's protection history (antivirus uploads of the .exe only with the user's go).
6. If all holds: make `env_on_windows()` default to on (the off switches stay), and mention it in the changelog.

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
