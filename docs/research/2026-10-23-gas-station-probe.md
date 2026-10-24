# Gas stations: West Coast USA's fuel kits and the runtime pump probe (2026-10-23)

The user, 2026-10-23: "the refueling is the main part, we can already refuel in beam so I want to include it on this
impressive map mod." Read from BeamNG 0.40.3's `content/levels/west_coast_usa.zip` (offline), and the in-game probe
`probes/fuel_pump.lua` (results below once run).

## 1. How West Coast USA builds a station

`levels/west_coast_usa/main/MissionGroup/fuel_stations/fuel_station_<n>/`: one canopy `TSStatic`
(`art/shapes/buildings/s_fuel_structure_0x.dae`) and pump **prefabs** (`art/prefabs/p_fuel_pump_<year>_<brand>.prefab.json`
= two TSStatics at the prefab's origin: `s_fuel_pump_<year>.dae` + `s_fuel_pump_<year>_<brand>.dae`), all with the
canopy's yaw (fs_3's pumps: the canopy's yaw + 180°). Each pump has a `BeamNGGameplayArea` centred on it (scale
`2.2 0.6 2.75`, x along the pump row) and a `BeamNGPointOfInterest` ~1.5-2.5 m above it, listed as a pair in
`facilities/facilities.facilities.json` (`gasStations[].pumps`). There is no separate island shape: the canopies'
meshes include their islands (their boxes reach 1 m under the origin).

The canopy kits (pump positions in the canopy's frame, m; the canopy's model half sizes from its .dae positions):

| canopy | half x × y (m) | height | pumps (lx, ly) | pump dz | pump |
|---|---|---|---|---|---|
| `s_fuel_structure_01` | 8.11 × 11.11 | 6.1 | (-6.08,-6.07) (-2.76,1.37) (-0.86,-8.40) (0.46,8.60) (2.46,-0.95) (5.68,6.28) | 0.33 | 2015 Trilobite |
| `s_fuel_structure_02` | 7.30 × 10.50 | 6.4 | (-7.69,-4.47) (-3.16,2.18) (-1.46,-8.68) (1.31,8.82) (3.00,-1.97) (7.54,4.62) | -0.03 | 2000 Nodeoline |
| `s_fuel_structure_03` | 6.50 × 10.70 | 6.0 | (±2.9, -7.43 / 0.03 / 7.56), pumps turned 180° | 0.16 | 2015 Apex |
| `s_fuel_structure_05` | 6.61 × 8.61 | 5.8 | (-4.74,2.38) (0.05,-5.30) (0.29,5.50) (5.08,-2.16) | 0.05 | 2000 Tyranos |

`s_fuel_structure_06` is a 1 m slab roof (fs_4 puts it 4.8 m up over `_07` pillars) and `s_fuel_island_main` /
`s_fuel_main_05` are whole shop buildings: not used.

**Without a canopy** (fs_6, an Apex "commercial" station): two `p_fuel_pump_2015_apex` 5.6 m apart in a row, yellow
bollards (`/art/shapes/objects/bollard_yellow.dae`, scale 1.5 1.5 1) either side of each, areas `0.6 2.2 2.0`.

## 2. Materials

The shapes' materials come from three places:
- global art (always loaded): `clutter_commercial`, `industrial_trim`, `vertcolor_glow`, `barrierfence`
  (`/art/shapes/objects/`), `m_metal_trim_01`, `m_metal_wall_corrugate`, `m_plastic_trim`, `eca_genericsigns`,
  `bld_glass_transparent` (`/art/shapes/garage_and_dealership/`), `lod_vertcol`, `SOFT_COLLISION_GENERAL`,
  `slabs_large`, `architraves` (`/art/shapes/common/`);
- `/levels/west_coast_usa/art/shapes/buildings/main.materials.json`: `m_gasstation_tyrannos`, `brick_painted`,
  `concrete_precast1`, `highrise`;
- `/levels/west_coast_usa/art/shapes/objects/main.materials.json`: `corrugation`.

So the mod loads the two West Coast files (`gasstations.lua` `MATERIAL_FILES`), as `roads.lua` does for its borrowed
road materials.

## 3. In-game probe (`probes/fuel_pump.lua`)

Spawns the `s_fuel_structure_03` kit 25 m ahead of the player at runtime, each pump with an area `1.6 2.2 2.75`
(wider across the row than West Coast's 0.6, so a car need not touch the pump) and an icon 2.5 m up, and adds a stock
station POI through a temporary `onGetRawPoiListForLevel` extension.

Results (BeamNG 0.40.3, the BeamNG.Earth level, through the MCP, 2026-10-23; the stations as the mod spawns them, not
the probe file):

- The stock prompt ("Gas Station - <name> - Gasoline / Diesel - Refuel Vehicle") appears for a car stopped beside a
  pump in its `1.6 2.2 2.75` area: West Coast pumps (near Pittsburgh), East Coast's units (a Sheetz near Pittsburgh) and Italy's
  pumps (the Aral, Garmischer Str. 197, Munich). Its name is the record's (the Aral's said its operator's, "Wolfgang
  Tulach", until the name rule: the brand when the name does not mention it).
- Spawn cost, gasstations.update timed per frame: every station under 5 ms; the Buc-ee's in New Braunfels (132 pumps,
  88 pillars, two roof slabs, ~490 objects) 23.8 ms in one frame. Not spread over frames yet (a one-off on arrival).
  The batched collision rebuild after it is not in that figure.
- Shapes are preloaded at the session start (`gasstations.PREWARM`), so their first load is not mid-drive; East Coast's
  unit uses `eca_genericsigns`, defined in no materials file of East Coast's own zip, yet its roof-edge signs render.
- Not measured: the smallest reliable area, and the tank filling over time.

## 4. Other levels' sets (2026-10-23)

- Italy (`levels/italy/art/shapes/buildings/italy_petrolstation_*`): an awning half `awningSt` (12 m along its x,
  6.53 m to its -y, pillars built in; two turned 180 degrees about one origin make a 12 x 13 m section), pumps `pump_b`
  / `pump_c` (two under each half at x +-2.25, y -3.2), `brandsign`. Materials: Italy's
  `art/shapes/buildings/main.materials.json`.
- East Coast USA (`levels/east_coast_usa/art/shapes/buildings/eca_gastation_pumps.DAE`): one model of three islands
  (1.46 m wide at x = -5.215, 1.24, 7.74; along y over -9.2 .. -4.53; 0.2 m high), a dispenser on each at y = -7.46,
  and a 17.6 x 6.6 m roof 4.7 m up (x -7.53 .. 10.1, y -10.04 .. -3.44), origin at the ground (its collision mesh).
  East Coast's own station (`main/MissionGroup/Gas_Station`, `fuel_node_city` / `fuel_node_dealer`) puts one area per
  island at the dispenser, scale `2 0.5 1` across the island. Materials: East Coast's
  `art/shapes/buildings/main.materials.json`.
