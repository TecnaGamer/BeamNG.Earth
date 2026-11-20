# Bulgarian roads

Branch `bulgaria-roads`, off `dev`. Everything here applies to country BG only; other countries' output is unchanged
(their tests and the bake-format golden say so).

## What this covers

| Feature | What the game shows | Rule | Where in the code |
|---|---|---|---|
| Legal speed defaults | The AI drives, and the speed signs post, 50 km/h in a populated place, 90 outside one and 140 on a motorway where no limit is mapped | Закон за движението по пътищата чл. 21 (1), category B, as amended by ДВ 64/2025 | `navgraph.LEGAL_KMH`, `road_mph` |
| В26 speed signs | The Vienna disc (white, red ring, black figures), showing the AI's limit, defaults included | Наредба № РД-02-21-1 от 2023, чл. 66 and annex 3 | `speedsigns.STYLE_BULGARIA` |
| Road markings | 0.10 m lines (0.15 on motorways); broken 3 / 6 m on two-lane roads and town streets, 3 / 9 between lanes, 4 / 12 on motorways; a double solid line (M2) on roads of more than three lanes; solid edge lines 0.15 m, 0.25 m on motorways and roads of more than three lanes | Наредба № 2 от 17.01.2001 (to ДВ 32/2019) | `markings._BG`, `Style.double_multi`, `Style.edge_wide_lanes` |
| Sofia's yellow paving | The glazed yellow bricks (жълти павета) of Tsar Osvoboditel and the streets round it, on the mesh and in the ground photo | OpenStreetMap's colour tags (67 ways) | `bgpaving.py`, `server/bg_yellow_paving.json`, `surface.photo_kind` |
| С7 bend boards | White boards with a red chevron on the outside of sharp rural bends, two back to back on two-way roads | Наредба № РД-02-21-1 чл. 201 and чл. 24 (1) 3 (radius 200 m or less, turning 30° or more), spacing from annex 23 в | `bgsigns.py` |
| Д11 / Д12 populated-place signs | The place's name in black on white with a black border where a road enters built-up land, with the Latin transliteration on republican roads; the same with a red diagonal where it leaves | Наредба № РД-02-21-1 чл. 105, 119, 120; Закон за транслитерацията | `bgsigns.py`, `server/bg_sign_glyphs.json`, `roadside/bg_letters_o` |

## Real-data checks

Built offline from the cached map data:
- Shipka pass: 497 С7 posts over about 25 km².
- Shipka's town edge: 8 Д11 / Д12 boards reading "Шипка" / "Shipka".
- The whole chain through `signstream.build_tile` runs on a real tile.

## Interaction with dev's features

- **Town signs** (Settings > World > Town signs, `townentry.py`): when they are on, they draw Bulgaria's town boards and
  `bgsigns` leaves out its own Д11 / Д12 (no doubled boards). With the switch off, Bulgaria gets the Д11 / Д12 above.
- **Roads painted into the ground photo** (`ROADS_IN_GROUND`, the default): the yellow paving is a sixth paint kind.
  The native paint needs paved asphalt as the fifth kind (`paintlist.rs` `PAVED_KIND`) and the last kind over a pixel
  wins, so yellow paving comes after asphalt: where a yellow boulevard meets an asphalt street, the yellow runs on
  through the junction. Putting asphalt on top would need a change to the native painter.
- **Textures**: the letter atlas ships as a lossless source and is made into a DDS at install, as every mod texture now is.

## Known limits

- **No 120 km/h expressway default.** Nothing in the data tells a Bulgarian expressway (скоростен път) from a trunk
  road: the OpenStreetMap tag source has no `motorroad` column, and Bulgarian mappers use `highway=trunk` for every
  dual carriageway (5 objects in the country carry `motorroad=yes`, none of them an expressway). 93 % of Bulgaria's
  trunk ways have a mapped `maxspeed`, which wins over the default.
- **Sign sizes** are the common ones (0.6 m С7 boards, 0.15 m letters); БДС 1517, which sets them, is not public.
- **С7** uses only the geometric test of чл. 24 (1); the sight-distance and slowdown tests need data the map lacks.
- **Д11 / Д12** need the place-name cells already on disk (the status bar fetches them round the player).
- **Yellow paving** follows the map: a street whose colour is not tagged stays grey cobbles. Rebuild the list with
  `tools/build_bg_yellow_paving.py` when the map changes.
- Not yet driven in game after the merge with `dev`.

## Rebuilding the data

    python3 tools/build_bg_yellow_paving.py     # server/bg_yellow_paving.json from the Overpass API
    python3 tools/build_bg_sign_glyphs.py       # server/bg_sign_glyphs.json and the letter atlas PNG
    python3 tools/mod_textures_dds.py mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside/bg_letters_o.data.png
