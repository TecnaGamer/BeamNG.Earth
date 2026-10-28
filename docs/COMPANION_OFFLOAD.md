# BeamNG.Earth Companion Compute Offloading Guide

> **Turbocharge your Steam Deck, ROG Ally, Lenovo Legion Go, or lightweight laptop by offloading heavy far-terrain generation to your desktop PC or home server.**

---

## 1. Executive Overview

BeamNG.Earth renders 1:1 real-world planetary terrain in BeamNG.drive. Generating kilometers of real-world terrain on the fly—decoding elevation DEMs, synthesizing road networks, generating 3D terrain meshes with geometric skirts, and compressing mipmapped DXT1 aerial imagery—is computationally demanding.

On handheld APUs like the **Steam Deck** (4-core / 8-thread Zen 2 APU with a 15W shared CPU/GPU power envelope), running both the 60 Hz vehicle physics simulation, Lua game engine logic, rendering pipeline, *and* multi-process terrain streaming simultaneously leads to severe CPU and memory-bus contention.

**Companion Compute Offloading** (`peerwork.py` and the standalone `BeamNG.Earth-Sidekick` executable) provides a native, zero-friction client-server compute architecture:
- Your **Desktop PC or Home Server** runs `BeamNG.Earth-Sidekick`, serving as an accelerator co-processor.
- Your **Handheld or Laptop** connects to the desktop over local LAN or Tailscale Mesh VPN.
- **Far-field terrain generation** is seamlessly delegated to the desktop's multi-core CPU.
- Local handheld CPU cores and memory bandwidth remain 100% dedicated to vehicle physics, game rendering, shadows, and near-field collision meshes.

```
+-------------------------------------------------------------+
|               HANDHELD DEVICE (Steam Deck)                  |
|                                                             |
|  [ Vehicle Physics ]    [ Lua Engine ]    [ GPU Rendering ] |
|  [ Near Road Tiles ]    [ Spline Math]    [ Local Graders ] |
|                                                             |
|                   SharedFarPool (Client)                    |
+------------------------------+------------------------------+
                               |
                   BNGE Framing TCP / Port 47620
                   allow_pickle=False .npy + DDS
                               |
                               v
+-------------------------------------------------------------+
|              DESKTOP RIG (BeamNG.Earth-Sidekick)             |
|                                                             |
|  [ Socket Server ] <---> [ RespawningPool of N Workers ]    |
|                                                             |
|  Worker 01: far_geometry / far_tile (16-thread / 24-thread) |
|  Worker 02: DEM Bilinear Filtering                          |
|  Worker 03: DXT1 Texture Compression & Mipmap Generation    |
|  Worker ..: Fast Rust Kernel Accel (earthfast ABI3)         |
+-------------------------------------------------------------+
```

---

## 2. Under the Hood: The Companion Architecture

### 2.1 Multi-Core Auto-Scaling & Worker Pools
When `BeamNG.Earth-Sidekick` launches on the companion machine:
1. It queries `os.cpu_count()` and reserves 2 threads for system responsiveness (`workers = max(1, os.cpu_count() - 2)`).
   - On a 12-core / 24-thread AMD Ryzen 9 5900X, it automatically spawns **22 parallel workers**.
   - On a 16-core / 32-thread AMD Ryzen 9 5950X / 7950X or Intel Core i9, it spawns **30 parallel workers**.
   - With `--turbo`, it reserves only 1 thread for the OS (`os.cpu_count() - 1`).
2. Workers run in an isolated `pools.RespawningPool` using multiprocessing's `spawn` context, initializing `blockworker.init` with local image and elevation caches.
3. If any worker process crashes or leaks memory, the pool automatically respawns it without dropping the TCP server session.

### 2.2 Wire Protocol & Bit-Identical Determinism
Communication uses a low-overhead, binary-safe TCP protocol over port `47620`:
- **Framing**:
  `b"BNGE"` magic (4 bytes) + `Header Length` (uint32) + `JSON Header` + `Blob Count` (uint16) + `[Blob Length (uint64) + Bytes, ...]`.
- **Zero-Code / Zero-Pickle Safety**:
  Geometry arrays (`positions`, `normals`, `uvs`, `planar`, `tris`, `grid`) are serialized using standard NumPy `.npy` format with `allow_pickle=False`. No executable bytecode or arbitrary objects cross the network.
- **Cryptographic Code Fingerprinting (`code_id`)**:
  Far tiles are pure mathematical functions of coordinate anchors, zoom levels, and layout settings. To prevent terrain tears or mesh seams between machines, the `hello` handshake enforces identical code signatures:
  $$\text{code\_id} = \text{source\_sha256}[:16] - \text{native\_version}$$
  where `source_sha256` hashes all Python server modules and `native_version` verifies the Rust `earthfast` binary kernel (e.g. `87fc9b4bffc916ed-87`). If code fingerprints do not match, the connection is cleanly rejected before any jobs are scheduled.
- **Dynamic Mipmap Shrinking (`dds_shrink`)**:
  When a desktop helper generates high-resolution 512px textures for far tiles, but the client handheld runs the `small` profile (requiring 256px textures for VRAM conservation), `dds_shrink` strips the top mip level directly from the DXT1 DDS bitstream without re-encoding, transferring smaller payloads and preserving handheld VRAM.

### 2.3 Handheld Integration (`SharedFarPool`)
On the handheld side, `server/peerwork.py` injects `SharedFarPool` into `earthd`:
1. **Parallel Slot Expansion**:
   The client opens parallel persistent TCP connections to the helper up to `MAX_SLOTS = 32`. `earthd` dynamically expands its concurrent in-flight far-tile budget:
   $$\text{streaming\_budget} = \text{local\_workers} + \text{remote\_slots}()$$
2. **Autonomous Failover**:
   If a companion server becomes unreachable or drops connection, `SharedFarPool` catches the exception, immediately resubmits the job to the handheld's local worker pool, and puts the failed peer on a retry cooldown (`HELPER_RETRY_S = 60.0s`). The game never stutters, hitches, or crashes if the companion goes offline.

---

## 3. Real-World Benchmark Analysis: Mont Ventoux D974

To evaluate the real impact of companion offloading on low-power handhelds, we conducted controlled end-to-end telemetry benchmarks on the **Steam Deck** (4c/8t Zen 2 APU @ 15W TDP) climbing **Mont Ventoux (D974)** from `START` (`44.171279, 5.139169`) to `TOP` (`44.173433, 5.277728`):
- **Corridor Distance**: 3.0 km continuous spline drive
- **Target Velocity**: 120.0 mph (53.64 m/s) uncapped 60 Hz physics
- **Companion Host**: Desktop PC (AMD Ryzen 9 5900X, 22 workers over Gigabit LAN)

### 3.1 Teleport & World-Build Milestones

Cold generation milestones measure the time required from vehicle spawn to world settlement and queue stabilization:

| Milestone | Baseline (Upstream 1.7.0-beta) | Solo Experimental (Handheld Only) | Companion Offload (Deck + 5900X) | Improvement vs Solo | Improvement vs Upstream |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **`anchor` (Placement)** | 4.7s | 2.4s | **4.3s** | - | -8.5% |
| **`roads9` (First 9 Road Tiles)** | 12.5s | N/A | **12.1s** | - | -3.2% |
| **`roads36` (36 Road Tiles)** | 22.7s | N/A | **21.8s** | - | -4.0% |
| **`roads` (All Initial Roads)** | 29.6s | 39.4s | **26.1s** | **-33.8%** | **-11.8%** |
| **`far` (Far-field Terrain)** | 33.8s | 74.9s | **42.4s** | **-43.4%** | - |
| **`backlog` (Zero Queue)** | 30.7s | 2.4s (early abort) | **42.4s** | - | - |
| **`settled` (Full Quiet State)**| 54.1s | 82.0s | **45.2s** | **-44.9%** | **-16.5%** |

> **Key Finding**: Initial road generation (`roads`) accelerated from **39.4s to 26.1s (-33.8%)** on the Steam Deck simply by offloading far tiles. Freeing the Steam Deck's 4 CPU cores from DEM elevation processing allowed the local road and bridge procedural generator to finish significantly faster.

---

### 3.2 In-Engine Driving Frame Latency & Smoothness

Recorded during high-speed 120 mph autonomous driving along the twisting mountain pass:

| Performance Metric | Baseline (Upstream) | Solo Experimental | Companion Offload | Improvement |
| :--- | :---: | :---: | :---: | :---: |
| **Mean Frame Rate** | 19.1 FPS | 24.6 FPS | **28.5 FPS** | **+49.2% vs baseline** (+15.9% vs solo) |
| **Frame Time (p50)** | 45.2 ms | 36.1 ms | **33.8 ms** | **-25.2% vs baseline** |
| **Frame Time (p95)** | 97.6 ms | 84.5 ms | **86.3 ms** | **-11.6% vs baseline** |
| **Frame Time (p99)** | 224.2 ms | 164.0 ms | **188.4 ms** | **-16.0% vs baseline** |
| **Maximum Hitch** | 433.6 ms | 311.5 ms | **313.6 ms** | **-27.7% vs baseline** |

---

### 3.3 Engine Subsystem Latency Breakdown (p95 ms)

Examining the in-engine performance diagnostic timers (`Ctrl+Shift+F` / `.bngperfgraph`) reveals how offloading unburdens specific CPU & GPU rendering stages:

| Subsystem | Baseline p95 | Solo Exp p95 | Offload p95 | Delta vs Baseline | Delta vs Solo | Max Spike (Offload vs Solo) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`RenderShadows`** | 17.87 ms | 7.05 ms | **5.98 ms** | **-66.5%** | **-15.2%** | 66.30 ms |
| **`Lua`** | 14.82 ms | 22.04 ms | **17.83 ms** | - | **-19.1%** | **75.96 ms vs 139.00 ms (-45.4%)** |
| **`LuaGC`** | 5.91 ms | 7.05 ms | **5.44 ms** | **-8.0%** | **-22.8%** | 41.79 ms |
| **`PhysicsAndVLUA`** | 8.83 ms | 7.63 ms | **7.97 ms** | **-9.7%** | - | 94.36 ms |
| **`Ticking`** | 5.06 ms | 6.68 ms | **5.43 ms** | - | **-18.7%** | 21.28 ms |
| **`FileWatchers`** | 0.70 ms | 0.59 ms | **0.73 ms** | - | - | 5.68 ms |

> **Contention Elimination**: On a unified memory architecture (like the Steam Deck APU), heavy Python multiprocessing pushes memory bus saturation and causes CPU throttling. Offloading reduced **Lua p95 latency by -19.1%** and slashed **maximum Lua spikes from 139.00 ms down to 75.96 ms (-45.4%)**.

---

### 3.4 Terrain LOD Quality & Coverage

Without compute offloading, high-speed driving outruns the handheld's generation rate, causing the camera to look at low-detail placeholder LOD terrain (`underLevel = 1`).

| Quality Metric | Solo Experimental (Handheld Only) | Companion Offload (Deck + 5900X) | Impact |
| :--- | :---: | :---: | :---: |
| **Highest Detail (`underLevel = -1`)** | 73.0% (1,282 / 1,755 frames) | **97.8% (1,712 / 1,751 frames)** | **+24.8% high-res fidelity** |
| **Low-Res Placeholder (`underLevel = 1`)**| 27.0% (473 frames) | **2.2% (39 frames)** | **91.8% reduction in pop-in** |
| **Remotely Generated Far Tiles** | 0 tiles | **801+ tiles** | Generated at 0.15s - 1.0s each |
| **Finish Line Server Backlog** | 192 tiles queued | **0 tiles queued (`far.queued = 0`)** | Complete real-time streaming |

---

## 4. Hardware Tier Classification

Sidekick automatically classifies host rigs into quirky hardware tiers based on logical thread count:

| CPU Threads | Tier Nickname | Example Hardware | Recommended Role |
| :--- | :--- | :--- | :--- |
| **$\le$ 4** | **Pocket Potato** | Steam Deck, Surface Go, Intel Celeron/N100 | Target client (not suitable as helper) |
| **5 .. 8** | **Scrapyard V6** | Core i7-7700K, Ryzen 3 3300X, Apple M1 (base) | Modest helper (3 - 6 workers) |
| **9 .. 16** | **Twin-Turbo V8 Rig** | Ryzen 7 5700X/7700X, Core i7-10700K | High-performance helper (14 workers) |
| **17 .. 24** | **12-Cylinder Thread Ripper** | Ryzen 9 5900X/7900X, Core i7-13700K | Heavy-duty co-processor (22 workers) |
| **25+** | **Orbital Geo-Foundry** | Ryzen 9 5950X/7950X, Core i9-14900K, Threadripper | Enterprise tile foundry (30+ workers) |

---

## 5. User Setup & Quickstart Guide

### 5.1 On Your Companion PC (Desktop or Server)

#### Option A: Running the Standalone Executable (Recommended)
Download and run `BeamNG.Earth-Sidekick-<version>.exe` (Windows) or `BeamNG.Earth-Sidekick-<version>-x86_64` (Linux):
```powershell
.\BeamNG.Earth-Sidekick-1.7.0-experimental.exe
```

#### Option B: Running from Source
```powershell
python tools/sidekick.py
# Or with max turbo allocation:
python tools/sidekick.py --turbo
```

Sidekick will output a banner with your local LAN and Tailscale connection commands:
```text
[+] Handheld Connection Setup (Steam Deck / ROG Ally / Legion Go / Laptop):
    * 192.168.1.50     (Local LAN / Wi-Fi)
      -> Launch option:   --peers 192.168.1.50:47620
      -> Environment var: BNGE_PEERS=192.168.1.50:47620
    * 100.64.0.10      (Tailscale Mesh VPN)
      -> Launch option:   --peers 100.64.0.10:47620
      -> Environment var: BNGE_PEERS=100.64.0.10:47620
```

---

### 5.2 On Your Handheld Device (Steam Deck / ROG Ally / Legion Go)

#### Method 1: Steam Launch Options (Recommended for Steam Deck Game Mode)
1. Open Steam on your handheld.
2. Right-click / press Gear icon on **BeamNG.drive** -> **Properties** -> **General** -> **Launch Options**.
3. Add:
   ```bash
   BNGE_PEERS=192.168.1.50:47620 %command%
   ```
   *(Replace `192.168.1.50` with your desktop PC's IP displayed in Sidekick).*

#### Method 2: Command-Line / Terminal
If launching `BeamNG.Earth` or `earthd` directly:
```bash
./BeamNG.Earth-1.7.0-experimental-x86_64.AppImage --peers 192.168.1.50:47620
```

---

### 5.3 Remote Play Anywhere via Tailscale Mesh VPN

Want to play BeamNG.Earth on your Steam Deck while on a train, in a hotel, or at a friend's house, while your powerful gaming PC at home does the heavy lifting?

1. Install **Tailscale** on both your home PC and your Steam Deck (`sudo steamos-readonly disable && sudo pacman -S tailscale`).
2. Run Sidekick on your home desktop. It will automatically detect your `100.x.y.z` Tailscale IP.
3. On your Steam Deck, set the launch option:
   ```bash
   BNGE_PEERS=100.64.0.10:47620 %command%
   ```
4. Even with 40-80ms of internet latency, companion offload provides near-instant tile delivery because tile generation (several seconds locally on Deck) takes only ~0.2s on desktop.

---

## 6. Troubleshooting & Diagnostics

- **Code Mismatch Error (`theirs ... ours ...`)**:
  Both the companion helper and the handheld client must run the same `BeamNG.Earth` version and build. Run `BeamNG.Earth-Sidekick.exe --check` on both sides to verify that `code_id` matches.
- **Firewall Notice**:
  Ensure inbound TCP traffic on port `47620` is permitted on the companion PC. In Windows PowerShell as Administrator:
  ```powershell
  New-NetFirewallRule -DisplayName "BeamNG.Earth Sidekick" -Direction Inbound -LocalPort 47620 -Protocol TCP -Action Allow
  ```
- **Fallback Verification**:
  If the desktop is powered off while playing, the handheld automatically logs `compute sharing: helper ... failed; built here` and seamlessly continues local generation without gameplay interruption.

