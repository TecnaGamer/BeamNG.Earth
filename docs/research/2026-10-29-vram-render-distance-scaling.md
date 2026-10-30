# Dynamic VRAM-Based Render Distance Scaling and Hardware Profiling (2026-10-29)

## Context & Problem Statement

BeamNG.Earth streams elevation, aerial imagery, road networks, vegetation, and 3D buildings in concentric rings around the player. Previously, the companion server and Lua mod used fixed budget reaches (typically ~768 m for roads, ~1344 m for trees, ~1800 m for buildings) regardless of player hardware.

This presented two trade-offs:
1. **High-End Hardware Underutilization**: Systems equipped with 12 GB, 16 GB, or 24 GB VRAM GPUs possessed the memory headroom to stream far greater road and building detail, but were constrained to default low draw distances.
2. **Handheld & Low-VRAM Risk**: Handheld systems (e.g. Steam Deck with 6–8 GB shared unified memory) and 4 GB laptops risked Out-Of-Memory (OOM) crashes or PCIe texture thrashing when navigating dense metropolitan areas.

## Architectural Decision

### 1. Hardware Memory Query

In `mod/beamng_earth/lua/ge/extensions/beamngEarth/core.lua`, during connection handshake initialization, the mod safely queries GPU memory through BeamNG's platform API:
```lua
local vramMB = nil
if rawget(_G, 'Engine') and Engine.Platform and Engine.Platform.getGPUInfo then
  local okGpu, gpu = pcall(Engine.Platform.getGPUInfo)
  if okGpu and type(gpu) == 'table' and tonumber(gpu.memoryMB) and tonumber(gpu.memoryMB) > 0 then
    vramMB = math.floor(tonumber(gpu.memoryMB))
  end
end
```
The detected `vramMB` is passed in the client `hello` packet to the companion server.

### 2. Four-Tier Hardware Profiling in Companion Server

In `server/budget.py`, the companion server determines the appropriate budget tier:

| Profile Tier | VRAM Threshold | Road Tracker Reach | Vegetation Reach | Building Reach |
| :--- | :--- | :--- | :--- | :--- |
| **Low** | $< 6\text{ GB}$ (Steam Deck, laptops) | $512\text{ m}$ | $960\text{ m}$ | $1,200\text{ m}$ |
| **Medium** | $6\text{ GB} \le \text{VRAM} < 10\text{ GB}$ | $768\text{ m}$ | $1,344\text{ m}$ | $1,800\text{ m}$ |
| **High** | $10\text{ GB} \le \text{VRAM} < 14\text{ GB}$ | $960\text{ m}$ | $1,728\text{ m}$ | $2,400\text{ m}$ |
| **Ultra** | $\ge 14\text{ GB}$ (24 GB, 16 GB desktop GPUs) | $1,152\text{ m}$ | $2,304\text{ m}$ | $3,200\text{ m}$ |

The active tier is returned to the client in the server's `hello` response and exposed via `settings.uiState()`.

### 3. GPU Performance Analysis: Storage vs. Compute Bottlenecks

A common real-world configuration is a GPU with high VRAM capacity but moderate compute throughput (e.g. AMD Radeon RX 7600 XT 16 GB, Intel Arc A770 16 GB).

#### The $O(r^2)$ Area Cost
Render distance scales quadratically with distance radius $r$:
$$\text{Area} = \pi r^2$$
Expanding road reach from $512\text{ m}$ to $1,152\text{ m}$ expands the visible mesh area by **$5.06\times$**:
- **VRAM Capacity**: Holds vertex buffers, collision geometry, and high-resolution ground textures without swapping across the PCIe bus.
- **Shader Compute & Draw Calls**: Every additional building and tree chunk incurs vertex processing, depth-pass testing, and alpha overdraw.

#### Synergy with Upstream Graphics Settings
To resolve this trade-off:
1. **Auto-Detection as Baseline**: Dynamic VRAM detection establishes the initial memory-safe streaming budget automatically.
2. **User Quality Presets Override**: BeamNG.Earth's dedicated Graphics Settings menu (`mod/beamng_earth/ui/ui-vue/mods/beamngEarth/earthSettingsStore.js`) allows players to set their graphics quality preset (`low`, `medium`, `high`, `ultra`, `extreme`) or dial down `treeDistance` and `buildingDistance` independently. A player on an RX 7600 XT can run `Medium` or `Normal` mesh distances for smooth 60+ FPS while retaining high-resolution textures (`groundPhoto: 4096`), decoupling memory utilization from shader compute.
3. **UI Transparency**: The detected hardware tier and memory are displayed under **Settings > About > Hardware scaling** (e.g. `ULTRA (16 GB VRAM)`).

## Validation

- Tested unit profile scaling in `server/tests/test_budget.py` (tier assignments, boundary conditions, and fallback values).
- Validated end-to-end handshake protocol in `server/tests/test_protocol_agreement.py` and `server/tests/test_server.py`.
