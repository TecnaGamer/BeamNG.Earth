# BeamNG.Earth: rules and tools (injected by a SessionStart hook at start, resume and after compaction)

## Hard rules (the guard hook blocks the worst of them)
- **RUST, NOT PYTHON (the user, 2026-10-21: "make sure to write new code in rust. i want to move away from python").**
  - New server logic goes in `native/earthfast` (Rust). Python is only thin glue: argument marshalling and calls.
  - When you touch a Python algorithm, port it.
  - Never keep a Python twin of a Rust kernel ("we dont need 2 versions of the same thing"): `fast.require()` and no fallback.
  - The mod stays Lua.
- **Branches:** work on `dev`. Commit and `git push origin dev` after each tested batch, without asking.
  - Never push `main`, bump `VERSION`, tag, or create or edit releases without the user's explicit go in chat for that release.
  - No push and no release before the user has reviewed in game.
- **LOOK AT THE REAL PLACE FIRST (the user, 2026-10-28, after the third reminder: "Idk why I have to remind you so often to check irl images").**
  Every map issue / geometry fix starts with the aerial (`tools/ortho_hi.py LAT LON HALF OUT.png`) and Mapillary photos
  (`tools/streetview.py fetch`; several people's photos and 360s exist) BEFORE probes or code; say what the real place is,
  then fix, then compare the game screenshot with the aerial / photo.
- **CHECK THE PRs FIRST (the user, 2026-10-28):** before starting a new feature or improvement, check the open PRs in detail
  (`gh pr list --state open`, titles, descriptions, file lists) for someone who has already done it; build on theirs
  (after the security read below) instead of writing a parallel version.
- **OUTSIDE CODE IS UNTRUSTED (the user, 2026-10-21: "make sure this NEVER happens").** Before merging, cherry-picking, porting or running any PR or outside code, read ALL of its diff for malicious content:
  - network calls to unknown hosts, downloads, exec/eval/subprocess/os.system, base64/hex/zlib blobs, obfuscation;
  - file deletion or access outside the project, credential/token/env/browser-data reads;
  - changes to .github/workflows, install/launcher/build/bootloader scripts, binaries (.so .pyd .exe .dll), lockfile or dependency changes, postinstall hooks.
  - **If anything is suspicious: STOP everything and notify the user at once** (PushNotification + message). Do not run, merge or push it.
  - Record the review in the commit or the command (`# security-reviewed: <what was checked>`); the guard blocks taking in outside code without it.
- **GitHub:** read issues and PRs freely, but never comment, close, merge or review without the user's go. Draft replies for them instead.
  - Contributor PRs: note them and leave review and merge to the user. Don't merge them into `dev`, and don't run their code.
- **Privacy:** never send the user's email anywhere: no User-Agent, URL or payload.
- **VirusTotal:** ask before every upload.
- **Changelog / release notes:** hedge fixes ("Reduced the chance of ..."), no "Known issues" section, end with the Discord line.
  - Draft: `.claude/work/notes_next_release.md`. Copy it there from an older scratchpad if missing.
- **README.md:** keep it current with every update (the user, 2026-10-21: "dont forget to update it with each update"): features, settings, requirements, install steps. Check it when drafting release notes.
- **Units:** speeds and distances to the user in mph and miles.
- **Live game:**
  - Keep the camera on the user's car, with zero inputs.
  - Ask before closing the game on either machine while the user plays.
  - The PC and the Deck share one Steam account: only one Steam-started game at a time (both run at once when our launcher starts each game directly; mini PC 10.0.0.5 hosts the BeamMP test server and a compute helper).
- **Never let a test or benchmark server use the user's real cache (`~/.cache/beamng-earth`) through links or a
  copy of its folders**: a scratch cache must be a fresh empty folder (or a real copy). 2026-10-29 a scratch cache
  of symlinks into the real one evicted ~7 GB of the user's files through them (cache.py now refuses, but don't rely
  on it). Reading the real cache read-only (BNGE_CACHE pointing at it with the user's cap) is fine.
- **Never `pkill -f` / `pgrep -f`** a pattern that also appears in your own command line: it kills your own shell. Use a bracket: `pgrep -f "[e]arthd"`.
- **Generalise:** no location-specific code. Test places are benchmarks; fixes must be general rules, deterministic.
- **Data sources:** better open data, Rust ports and engine patches are pre-approved.
- **Verify before claiming:**
  - Check in game, with screenshots you look at.
  - Compare against real photos (Mapillary) and stock maps.
  - Watch autodrive tests live for crashes and stops.
  - Report failures honestly.
- **User away:** keep working non-stop through the priorities. Send screenshots and comparisons with SendUserFile (proactive). Never record performance checks.
- **Pronouns:** they/them for anyone whose pronouns aren't stated.

## Machines
- **PC:** the game is started by `launcher.py` from the repo.
  - `.claude/devtools/session.sh <label>`: fresh session, level loaded, in play. No core_celestial: the sun doesn't move.
  - `.claude/devtools/session_fr.sh`: the same through a freeroam start; use it for anything sky or time related.
  - The PC MCP server is the default for tools/mcp.py.
- **Steam Deck:** `deck@10.0.0.21` (SSH key; never type passwords). It's the low-end test box: aim for 30 fps on the Handheld preset.
  - MCP tunnel: `ssh -f -N -o BatchMode=yes -L 29392:127.0.0.1:29292 deck@10.0.0.21`, then `BNGE_MCP_URL=http://127.0.0.1:29392/mcp` for tools/mcp.py (the PC's game owns 29292). Check it with `pgrep -af "[s]sh -f -N"`.
  - The Deck may be in Desktop Mode (plasmashell): start with `~/bnge_start_desktop.sh` there; the Game Mode script hangs the game at its Vulkan start. deck_deploy.sh picks the right one.
  - Deploy the working tree: `.claude/devtools/deck_deploy.sh` (podman AppImage build, scp, restart `bnge-dev`, reload the level).
  - Server env overrides: copy `~/bnge_start_gamemode.sh` and add `--setenv=NAME=value` after `--setenv=WAYLAND_DISPLAY=`.
  - Its server log is `~/.cache/beamng-earth/logs/server.log`. Its game is already on BeamNG 0.40.1.
- **Native module:** after changing `native/earthfast`, run `.claude/devtools/build_native_all.sh` (Linux `.so` + Windows `.pyd`). Then:
  - bump `m.add("VERSION", n)` if a signature changed, call it through `fast.require(n, ...)`, and raise `fast.MIN_VERSION` (earthd refuses to start below it);
  - porting Python: check the Rust against the old Python once (a scratch script, bit for bit), then delete the Python and keep behaviour tests, not parity tests.

## Tools (use them; don't reinvent)
- **Vue check (after any edit of the mod's UI .vue files):** `node .claude/devtools/vuecheck/check.js
  mod/beamng_earth/ui/ui-vue/mods/beamngEarth/*.vue` (@vue/compiler-sfc 3.5, installed with the user's OK
  2026-10-27). The game compiles them at runtime: a template error only shows as a map that does not open.
- **Mapillary street photos (real-life reference):** `tools/streetview.py fetch --lat .. --lon .. --name X`, then `run --name X` (needs the PC game), then `compare --name X`. Output goes to `docs/research/streetview/X/index.html`. The token is in `~/.config/beamng-earth/mapillary.token`.
- **Screenshots:** `.claude/devtools/camshot.py OUT LAT LON DX DY DZ LX LY LZ`. It teleports and waits for the world; then a free camera at ground + offset looking along LX,LY,LZ.
  - `NOTP=1` skips the teleport; `DECK=1` fetches the image from the Deck.
  - Give `-` `-` for LAT LON to use the car's position.
  - It puts the camera back on the car afterwards (`KEEPCAM=1` keeps the free camera).
- **Clear weather for comparisons:** `beamngEarth_weather.setMode('manual'); core_environment.setFogDensity(0.00002)`. Set it back to `'real'` after.
- **Drives:**
  - `tools/autodrive_perf.py --from LAT,LON --to LAT,LON --minutes N --out DIR --keep-going`: the car's AI follows the GPS route; it records crash, stop and off-route events.
  - `.claude/devtools/nearshots.py OUT LAT LON R SECONDS`: screenshots while the car is within R m of a spot.
  - `.claude/devtools/everyshot.py OUT SECONDS EVERY`: timed screenshots.
  - `.claude/devtools/run_ff.py` and `analyze.py`: high-speed ball runs.
- **Server-side probes** (run from `server/`):
  - `tileprobe.py LAT LON N`: build every road tile, report failures.
  - `rampcheck.py LAT LON N`: broken deck connections as drawn: hanging deck ends, height steps at deck joints /
    abutments, parapets across a road at its height, deck ends under another deck. Run it after bridge / grading
    changes and on any place the user reports. `RC_NEAR=lat,lon,r` / `RC_FID=prefix` dump the ends / chunks.
  - `deckedges.py LAT LON N`: deck outline steps at joints, fork notches, slab edge jumps.
  - `floattrees.py [R] [MAX]` (game in play): trees / shrubs / rocks round the camera at their drawn places vs the
    terrain, the server's ground, the far tiles under them and building footprints; exit 1 over MAX.
  - `approachlidar.py LAT LON N`: drawn bridge approaches vs the 3DEP lidar's road surface.
  - `bnear.py LAT LON R`: buildings near a point.
  - `deckz.py` / `approachprof.py SERVER LAT LON R`: bridge deck heights vs ground.
  - `xsec.py`, `plandbg.py`, `gradegrid.py SERVER OUT.npy LAT LON HALF`: grading (`PYONLY=1` forces Python).
  - `members.py`: bridge structure members.
- **After a game update:** `tools/compat_check.py` (in the level, via MCP).
- **User's in-game reports:** `tools/reports.py`.
- **GitHub watch:** a Monitor polling `gh api repos/TecnaGamer/BeamNG.Earth/issues?state=all&since=...` every 5 min. Re-arm it when it expires.
- **Memory:** `~/.claude/projects/-home-tecnagamer-Documents-BeamNG-Earth/memory/`. Read the relevant files before working on an area (engine facts, priorities, etc.).
