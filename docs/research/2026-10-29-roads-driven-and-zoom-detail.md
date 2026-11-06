# Multi-Scale Road Hierarchy and Persistent Roads Driven Tracker (2026-10-29)

## Context & Problem Statement

Issue #365 requested an in-depth map view similar to modern web cartography (e.g. Google Maps) that reveals increasing road detail as the user zooms in, alongside persistent tracking of roads driven across game sessions.

### Previous Limitations

1. **Uniform Map Density**: In `mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthMap.vue`, all roads were drawn with a single uniform stroke style. At zoomed-out scales, residential alleys and service ways cluttered the display into an unreadable tangle, while zoomed-in views lacked visual hierarchy between major freeways and local streets.
2. **Transient Exploration**: Players had no visual record of explored versus unexplored territory across the globe.
3. **GPS Route Overlap**: The mod's built-in GPS destination breadcrumb (`routeDone`, styled as `.route-done` in muted gray) was transient: whenever a player selected a new destination or cleared the route, `clearGps()` immediately erased the traversed path.

## Architectural Decision

### 1. Multi-Scale Dynamic Road Hierarchy

Introduced scale-dependent SVG styling and classification in `EarthMap.vue`:

- **Classification by Functional Class**:
  - `motorway`, `trunk`: Major freeways and dual carriageways.
  - `primary`, `secondary`, `tertiary`: Arterial and collector roads.
  - `residential`, `service`, `unclassified`: Local neighbourhood streets.
- **Dynamic CSS Filtering & Stroke Sizing**:
  At wide zoom scales ($k < 1.0$), minor residential and service roads fade out via opacity and reduced stroke widths, keeping major highways and arterial spines crisp. As the player zooms in ($k > 1.8$), full local connectivity, street geometry, and labels fade in smoothly.

### 2. Persistent Roads Driven Tracker

To avoid coupling with the transient GPS route, the exploration tracker is implemented as an independent persistent subsystem:

- **Tracking Engine in Lua**:
  `mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua` monitors vehicle traversal across mapped road segments. When the vehicle covers a road segment, its unique identifier and coordinates are recorded in an in-memory set and persisted to `<user-settings>/beamngEarth_driven.json`.
- **UI Exposure**:
  `settings.lua` and `roads.lua` expose `M.getDrivenSummary()` to the Vue frontend via `uiState()`.
- **Distinct Neon Visual Layer in `EarthMap.vue`**:
  ```html
  <g class="driven-network" v-if="drivenSegments && drivenSegments.length > 0">
    <polyline v-for="seg in drivenSegments" :points="seg.points" class="driven-line-glow" />
    <polyline v-for="seg in drivenSegments" :points="seg.points" class="driven-line" />
  </g>
  ```
  ```css
  .driven-line-glow {
    stroke: rgba(0, 212, 255, 0.35);
    stroke-width: 8px;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .driven-line {
    stroke: #00d4ff;
    stroke-width: 4px;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  ```
- **Session & GPS Independence**:
  Because this layer is read directly from `beamngEarth_driven.json` and rendered independently of `gps.routeDone`, it persists across sessions, world rebases, and route destination changes.

## Validation

- Verified SVG multi-scale rendering and reactive zoom updates in Vue 3.
- Validated JSON serialization, disk read/write, and rebase coordinate stability in `roads.lua`.
