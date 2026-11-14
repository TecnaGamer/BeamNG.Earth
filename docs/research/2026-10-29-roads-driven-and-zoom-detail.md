# Persistent Roads Driven Tracker and Exploration Odometer (2026-10-29)

## Context & Problem Statement

Issue #365 requested exploration tracking and in-depth map detail similar to modern web cartography (e.g. Google Maps), revealing contextual scale and persistently tracking roads driven across game sessions.

### Previous Limitations

1. **Transient Exploration**: Players had no visual record of explored versus unexplored territory across the globe.
2. **GPS Route Overlap**: The mod's built-in GPS destination breadcrumb (`routeDone`, styled as `.route-done` in muted gray) was transient: whenever a player selected a new destination or cleared the route, `clearGps()` immediately erased the traversed path.
3. **Missing Cartographic Scale**: The map view lacked a real-world scale bar to gauge geographical distances across varying zoom levels (continent down to street level).

> **Scope Note**: This feature provides the persistent exploration tracker, odometer, and dynamic scale bar for the 2D UI map. Procedural hierarchical road classification and rendering in the 3D game world is tracked under a separate future engine roadmap item.

---

## Architectural Decision

### 1. Dedicated Exploration Tracker Engine (`roads_driven.lua`)

To avoid coupling with the transient GPS route, exploration tracking is isolated into an independent extension:

- **State Persistence**:
  Paths are recorded as polylines of geographic coordinate pairs `[lat, lon]` and saved to `<user-settings>/beamngEarth_driven.json`.
- **Sampling & Teleport Detection**:
  - `MIN_STEP_M = 25.0`: New points are recorded only when the vehicle has moved at least 25 meters from the previous point, avoiding redundant points when idling or in traffic.
  - `TELEPORT_GAP_M = 2000.0`: Movements exceeding 2 km in a single step (such as map teleports, menu resets, or mission starts) automatically start a new polyline segment, preventing visual streaks across the globe.
  - `MAX_POINTS = 50000`: Caps total stored points across all polylines. When the cap is reached, `trimPoints()` slices the oldest line in place, preserving table identity and keeping `currentLine` in sync without losing active points.
  - **Zero Mid-Drive Disk Writes**: Tracking data is saved as compact JSON (`jsonWriteFile(..., false)`) exclusively when the car is stopped (`speed < 0.5 m/s`), paused (`st.paused`), or at level exit (`onClientEndMission()`), eliminating frame drops or disk I/O while driving at speed.
- **Driving State Validation (`isDrivingVehicle`)**:
  Points are recorded only when the player is actively driving their own vehicle (`getPlayerVehicle(0)`):
  - Ignores free camera view (`commands.isFreeCamera()`, `core_camera.getActiveCamName() == 'free'`) to prevent flight exploration streaks.
  - In BeamMP multiplayer, checks `MPVehicleGE.isOwn(id)` to ignore spectated vehicles.

---

### 2. High-Performance UI Bridge (`settings.lua` & `EarthMap.vue`)

- **Revision Counter (`rev`) & Active Tail (`cur`)**:
  Instead of serializing up to 50,000 points across the bridge on every 500 ms poll, `uiState()` returns lightweight metadata:
  ```lua
  {
    totalKm = 142.5,
    sessionKm = 12.3,
    rev = 42,
    cur = { 37.7749, -122.4194 }
  }
  ```
  - `rev` increments only on new polyline segments (teleports, session start) or trimming. Normal continuous driving updates stream only `cur` (~20 bytes) which the Vue frontend appends to the active segment in place.
  - Full polyline data is queried asynchronously via `uiDrivenLines()` only when `rev` changes and the layer is active.

- **Frontend Viewport Culling & Downsampling**:
  `EarthMap.vue` computes SVG polyline coordinates with two levels of optimization:
  1. **Bounding Box Culling**: Checks the geographic bounding box of each polyline against the screen viewport (`mm.viewBox`), skipping lines entirely outside the current view.
  2. **Screen-Space Manhattan Distance Threshold**: Skips consecutive vertices whose screen distance is `< 1.5 px`. At continental zoom, thousands of points collapse into a few dozen SVG coordinates, guaranteeing 60 FPS rendering.

---

### 3. Cartographic Polish: Odometer & Dynamic Scale Bar

- **Exploration Odometer**:
  Displays cumulative distance driven across sessions in the map header. Formats distances in metric (km) or imperial (mi/ft) using `fmtMiles()` based on game settings (`gps.metric`).
- **Dynamic Scale Bar**:
  Calculates real-world ground resolution (`mPerPx = (156543.03392 * cos(lat)) / 2^z`) and displays an adaptive scale bar matching standard cartographic rounding steps (e.g. 50 m, 100 m, 500 m, 1 km, 5 km, 20 km or 100 ft, 500 ft, 1 mi, 5 mi, 20 mi).
- **Clean Styling**:
  Replaces system-dependent Unicode emoji glyphs with styled CSS badges for cross-platform visual consistency.

---

## Validation & Verification

1. **Unit & Engine Tests**:
   - Polylines correctly break on jumps $> 2000\text{ m}$.
   - Distance calculations match haversine geodesic distances.
   - Point trimming preserves polyline continuity without memory leaks.
2. **UI Bridge Verification**:
   - Benchmarked UI frame times with 50,000 points: zero frame stutter or IPC choking during rapid zooming and panning.
