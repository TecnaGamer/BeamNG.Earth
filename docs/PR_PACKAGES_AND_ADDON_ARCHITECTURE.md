# Modular Upstream PR Packages & Addon Architecture Guide

This document explains how `dev-experimental-branch` (`1.7.3-dev-experimental`) is split into **4 independent, self-contained PR packages** against `TecnaGamer/BeamNG.Earth` `upstream/dev` (`ad9ae5a1`), and details the engineering rationale behind the **Declarative Addon Refactor** and **Live World Toggle Reload**.

---

## 1. Why Split Into 4 Modular PR Packages?

Maintainers of large simulation projects prefer small, focused PRs where **performance optimizations**, **core plugin infrastructure**, **regional content packs**, and **benchmarking tools** do not depend on each other:

```mermaid
flowchart LR
    UP["upstream/dev (ad9ae5a1)"] --> PR1["PR #1: Performance & Frame-Pacing\n(Deferred Collisions, Shadow Culling, Tree Load Cap)"]
    UP --> PR2["PR #2: Addon Architecture & Live Reload\n(Layer A Registry, Layer B Effort, Live Toggle Refresh)"]
    PR2 --> PR3["PR #3: Provence Ground Truth Pack\n(IGN BD TOPO/RPG, 20cm Ortho Roofs, Mediterranean Crops)"]
    UP --> PR4["PR #4: Road Benchmark & Telemetry Suite\n(road_bench.py, cold_ab_bench.py, perf_capture.py)"]
```

| PR Package | Branch Name | Category | Coupling to Other PRs | Key Files |
| :--- | :--- | :--- | :--- | :--- |
| **PR #1** | `pr/perf-frame-pacing-and-shadows` | **Performance Only** (Zero terrain-gen changes) | **Standalone** (merges directly into `upstream/dev`) | `roads.lua`, `trees.lua`, `shadowctl.lua`, `buildings.materials.json`, `tunnel.materials.json`, `pools.py`, `test_roads.lua`, `test_trees.lua`, `test_shadowctl.lua` |
| **PR #2** | `pr/addon-architecture-and-effort-governor` | **Addon Infrastructure & Live World Reload** | **Standalone** (merges directly into `upstream/dev`) | `server/addons.py`, `server/effort.py`, `server/earthd.py`, `server/blockworker.py`, `server/revisit.py`, `mod/.../addons.lua`, `earthAddonsWindow.js`, `earthSettingsStore.js`, `test_addons.py`, `test_effort.py` |
| **PR #3** | `pr/addon-provence-ground-truth` | **Regional Terrain & Biome Pack (`provence`)** | Depends on **PR #2** (`addons.py` / `effort.py` hooks) | `addons/beamng_earth_provence/*`, `server/ign_fr.py`, `server/vegetation.py`, `server/buildings.py`, `server/mapdata.py`, `bne_lavender_row.dae`, `test_buildings.py`, `test_vegetation.py` |
| **PR #4** | `pr/benchmark-and-telemetry-suite` | **Developer Benchmark & Telemetry Harness** | **Standalone** (merges directly into `upstream/dev`) | `tools/road_bench.py`, `tools/cold_ab_bench.py`, `tools/perf_capture.py`, `tools/drive_driver.lua`, `tools/mcp.py` |

---

## 2. Interest & Value of the Addon Architecture Refactor (Layer A vs. Layer B)

### 2.1 Zero-Rebuild Regional Extensibility (Native BeamNG Mod Zips)
Previously, adding a regional dataset (such as French IGN BD TOPO 3D buildings, IGN RPG crop parcels, Swiss/German forestry data, or state-level LiDAR/orthophotos) required modifying core server files (`vegetation.py`, `buildings.py`, `mapdata.py`) and maintaining a separate fork of the server.

With the **Declarative Addon Architecture (`server/addons.py` + `lua/ge/extensions/beamngEarth/addons.lua`)**:
- Community modders ship a standard BeamNG mod zip (`mods/beamng_earth_<region>.zip`) containing declarative JSON manifests (`addon.json`, `data_sources.json`, `biomes.json`) and custom 3D `.dae` / `.png` assets.
- **Strict Version & Integrity Gate**: Every addon declares `requires_beamng_earth` (e.g. `>=1.7.0 <1.8.0`) and is verified against `APPROVED_MANIFEST_HASHES` (SHA-256) before activation. Unapproved or incompatible mods are cleanly rejected without crashing or polluting the server.

### 2.2 Decoupling "Ground Truth" (Layer A) from "Performance Effort" (Layer B)
In traditional world generators, turning down graphics settings deletes whole feature types or changes what is generated in the world. Our two-layer split keeps **what exists** separate from **how hard the hardware works**:
- **Layer A — Ground Truth (`addons.py`)**: Defines *what* exists at `(lat, lon)` and *which data sources* are queried (e.g. "Parcel `#4812` is a vineyard oriented at `114°` azimuth; Building `#902` has a terracotta tile roof").
- **Layer B — Effort & Tradeoff Governor (`effort.py`)**: Controls *how densely* Layer A is synthesized based on the user's hardware or target frame rate (`low`, `balanced`, `high`, `ultra`, `auto_fps`):
  - `veg_density_scale` (`0.35x` on Steam Deck / `low` up to `1.50x` on `ultra`)
  - `crop_row_step_scale` (wider row spacing on weak CPUs/GPUs while preserving the exact parcel boundaries and row orientation!)
  - `max_items_per_cell` (`2,500` on `low` vs `12,000` on `ultra`)
  - `ortho_tint_sample_budget` (caps per-tile HTTP/orthophoto roof color samples)

---

## 3. Interest & Value of Live Toggle Reload for Terrain & World Generation

### 3.1 The Problem Before Live Toggle Reload
Previously, toggling a world-generation setting in-game had **no visible effect** on already-visited areas because of three caching layers:
1. **Disk Cache Collisions (`vegetation/v20/<release>/<x>/<y>.npz` and `revisit/`)**: Once a cell was generated with an addon off, its `.npz` vegetation array and revisit `.cdae`/`.json` tiles were written to disk under a static path. Enabling the addon later still read the old un-enriched `.npz` file from disk.
2. **Multi-Process Worker Isolation (`blockworker.py`)**: `earthd.py` offloads `veg_geo`, `roads_geo`, and `far_geo` to persistent `ProcessPoolExecutor` worker processes (`road_pool`, `far_bld_pool`). Changing a setting in `earthd.py`'s parent process left worker subprocesses running with stale addon/effort state and stale `_s.veg._mem` / `_s.md._mem` caches.
3. **Server Stream Tracker State (`Connection.veg`, `Connection.roads`, `Connection.bldfar`)**: Even if the client cleared its trees (`trees.clear()`), the server's `vegetation.Tracker` still marked those geotiles as "already sent" to this client and never re-streamed them until the player teleported kilometres away.

### 3.2 How the Live Toggle Reload Solves All Three
1. **Fingerprint-Keyed Disk & Memory Caches (`vegetation.py`, `revisit.py`)**:
   - Every vegetation cell (`Vegetation._path`) and revisit tile (`revisit.key`) is now tagged with `f"{addons.cache_fingerprint()}_{effort.effort_cache_tag()}"`.
   - Switching `provence` OFF → ON (or `balanced` → `ultra`) switches instantly to the corresponding cache namespace—preserving both versions on disk so toggling back and forth is instantaneous on warm cache!
2. **Zero-IPC Worker Subprocess Synchronization (`runtime_addon_effort.json`)**:
   - Whenever an addon or effort setting changes, `earthd.py` atomically writes `cache_dir/runtime_addon_effort.json`.
   - Before each tile job (`veg_geo`, `roads_geo`, `far_geo`, `build`), `blockworker.py` checks `st_mtime_ns` of `runtime_addon_effort.json` (~10 µs stat call) and, when changed, imports the updated `AddonRegistry` and `EffortController` state and flushes its in-memory cell caches.
3. **Instant Client + Server Tracker Reset (`Connection.reset_world_trackers()` & `Refresh World`)**:
   - When `cache_invalidated` is true (or when the user clicks **`Refresh World`**), `core.lua` clears active vegetation and building sessions (`trees.clear()`, `roads.newSession()`, `roads.incrementBldGen()`) while `earthd.py` resets `self.veg`, `self.roads`, `self.signs`, and `self.bldfar`/`bldsky`/`brfar`.
   - On the very next `pose` tick (~100 ms later), the server immediately re-streams all surrounding tiles using the newly selected addons and effort profile—allowing players and modders to compare Ground Truth packs and Effort levels live in 1–4 seconds without moving or restarting!

---

## 4. Benchmark & GitHub Telemetry Ticketing Configuration (Fork vs. Upstream Maintainer Guide)

### 4.1 Why BeamNG 0.40 Blocked Direct `openWebBrowser` Calls to GitHub
In BeamNG.drive 0.40+, the engine's C++ `openWebBrowser(url)` function enforces an internal domain whitelist (`E|platform| unable to open webpage due to domain filter: https://github.com/...`). Calling `openWebBrowser` directly from GE Lua silently fails when trying to open GitHub issue pages.

To make **`Post GitHub Ticket`** work reliably across Windows and Linux without ever overwriting the user's clipboard automatically:
1. [`report.lua`](../mod/beamng_earth/lua/ge/extensions/beamngEarth/report.lua) sends `{ type = 'open_page', url = url }` via [`core.lua`](../mod/beamng_earth/lua/ge/extensions/beamngEarth/core.lua) (`beamngEarth_core.openPage(url)`) to the local Python companion server (`server/earthd.py` -> [`server/updates.py`](../server/updates.py) `open_page(url)`).
2. On Windows, when the URL exceeds Win32 `ShellExecuteW`'s 2,083-character limit (`len(url) > 2000`), `updates.open_page(url)` resolves the user's default HTTPS browser executable via `winreg` (`HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice`) and spawns `[browser_exe, url]` via `CreateProcessW` (which supports up to 32,767 characters, passing the full `7,000`-character GitHub issue URL with **zero truncation**).
3. A separate **`Copy Report`** button (`beamngEarth_report.copyLastReport()`) is provided alongside **`Open Folder`** and **`Post GitHub Ticket`** so the user's clipboard is only modified when they explicitly click **`Copy Report`**.

### 4.2 Where the GitHub Ticket Goes & How to Configure It per Branch
To ensure experimental branch benchmarks (`Madderose/BeamNG.Earth`) do not spam upstream (`TecnaGamer/BeamNG.Earth`)—while letting the upstream maintainer route tickets to `TecnaGamer/BeamNG.Earth` on upstream branches—the target repository is configured in **three synchronized places**:

| File | Constant / Variable | Value on `Madderose/BeamNG.Earth` (`dev-experimental-branch`) | Value on `TecnaGamer/BeamNG.Earth` (`upstream/dev` & PR branches) |
| :--- | :--- | :--- | :--- |
| [`mod/beamng_earth/lua/ge/extensions/beamngEarth/issuefmt.lua`](../mod/beamng_earth/lua/ge/extensions/beamngEarth/issuefmt.lua) | `M.ISSUE_REPO` | `'Madderose/BeamNG.Earth'` | `'TecnaGamer/BeamNG.Earth'` |
| [`server/updates.py`](../server/updates.py) | `ISSUE_REPO` | `os.environ.get("BEAMNG_EARTH_ISSUE_REPO", "Madderose/BeamNG.Earth")` | `os.environ.get("BEAMNG_EARTH_ISSUE_REPO", "TecnaGamer/BeamNG.Earth")` |
| [`tools/reports.py`](../tools/reports.py) | `REPO` | `os.environ.get("BEAMNG_EARTH_ISSUE_REPO", "Madderose/BeamNG.Earth")` | `os.environ.get("BEAMNG_EARTH_ISSUE_REPO", "TecnaGamer/BeamNG.Earth")` |

- **Runtime Override (No Code Edit Needed)**: Setting the environment variable `BEAMNG_EARTH_ISSUE_REPO="Owner/Repo"` before launching `launcher.py` or `tools/reports.py` overrides the target GitHub repository at runtime.
- **Security Whitelist (`updates.OPEN_PREFIXES`)**: `server/updates.py` only permits opening `https://github.com/<REPO>/` and `https://github.com/<ISSUE_REPO>/` URLs, preserving the security guarantee tested by `server/tests/test_updates.py`.

---

## 5. Dual-Mode In-Game Benchmark, Multi-Route Registry (`BENCH_ROUTES`) & Speed Tiers (`BENCH_SPEEDS`)

To support both quick spot-checks and reproducible A/B comparisons across hardware and addon configurations, the **`Generation Addons`** floating window ([`earthAddonsWindow.js`](../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/earthAddonsWindow.js)) and [`report.lua`](../mod/beamng_earth/lua/ge/extensions/beamngEarth/report.lua) provide **two benchmark modes**:

1. **Mode 1 — `Free Drive (30s)` (`mode = "free"`)**:
   - Immediately records 30 seconds of frame times (`fps`, `avgMs`, `p50`, `p95`, `p99` / `1% low`, `maxMs`), hitches (`> 50 ms` with coordinates), 1 Hz per-second world telemetry, and hardware/addon settings from wherever the player is currently driving.
2. **Mode 2 — `Route A -> B (Auto)` (`mode = "route"`)**:
   - **CEF-Native Route Selector**: Because BeamNG's offscreen CEF renderer suppresses native OS `<select>` popup menus, routes are selected via a `<` / `>` stepper bar and direct one-click route pill buttons:
     - **`ventoux_d974`**: *Mont Ventoux D974 Climb (Malaucène → Summit)* (`44.171279, 5.139169` → `44.173433, 5.277728`, ~18 km route)
     - **`tavel_vineyards`**: *Tavel Vineyards → Lirac Village (D4 / Route de la Commanderie)* (`43.994226, 4.740771` → `44.036874, 4.690922`, ~6.5 km route)
     - **`lirac_tavel`**: *Lirac Narrow Streets → Tavel Vineyards* (`44.036874, 4.690922` → `43.994226, 4.740771`, ~6.5 km route)
     - **`sault_lavender`**: *Sault Plateau Lavender Fields → Aurel (D942)* (`44.081847, 5.365593` → `44.129500, 5.428000`, ~7.5 km route)
   - **3 Speed Tiers with Route Distance Adapted to Speed (`M.BENCH_SPEEDS`)**:
     - **`Slow: 80 km/h (50 mph)`** (`22.22 m/s`) → **`1.00 km`** drive (~45 s)
     - **`Fast: 200 km/h (124 mph)`** (`55.56 m/s`) → **`2.50 km`** drive (~45 s)
     - **`Ultra: 300 km/h (186 mph)`** (`83.33 m/s`) → **`3.75 km`** drive (~45 s)
   - **OSRM Road-Spline Kinematic Driver**:
     - Uses the same per-frame ENU road-spline placement (`geodeticToEnu` + `castRayStatic` + `setClusterPosRelRot` + `applyClusterVelocityScaleAdd`) as `tools/drive_driver.lua`, keeping the vehicle smoothly on the OSRM road centerline even at `200 km/h` and `300 km/h` across curves and world rebases.
   - **Full World Load vs. Immediate Start Readiness (`loadMode`)**:
     - **`Wait Full Load (100%)` (Default, `loadMode = "full"`)**: Instead of a fixed countdown, waits until the terrain block under the vehicle is committed, the OSRM road route is ready, **and** all initial tiles/buildings/vegetation finish loading (`far.done == true`, `far.queued == 0`, `serverBacklog == 0` for 2.0 consecutive seconds) before starting the drive and recording frames.
     - **`Immediate (Stream on Move)` (`loadMode = "cold"`)**: Waits only until the start terrain block under the car and OSRM road route are ready, then begins driving immediately to measure cold world streaming while moving.
   - **Adding New Routes**: Call `beamngEarth_report.registerBenchRoute({ id = '...', name = '...', shortName = '...', startLat = ..., startLon = ..., endLat = ..., endLon = ... })` or append to `M.BENCH_ROUTES` in `report.lua`.
