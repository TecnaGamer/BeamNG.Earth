# Australian regional scenery

Adds an Australian look to BeamNG.Earth: state building styles, NVIS-based vegetation, Australian road signs and markings
(AS 1742), streetscape, and payphones and post boxes. Everything is gated to Australia; other countries' output is unchanged.
See `docs/AUSTRALIA.md`.

## What is included

- **Buildings**: a house style per state and territory (Colorbond and tile roofs, brick and weatherboard, verandahs), including the high-set Queenslander.
- **Vegetation**: trees, shrubs and ground cover from the NVIS v7.0 Major Vegetation Groups (`server/data/au_mvg_v7.npz`, `veg_palettes.json`), dry-season grass, red soils in dry country, sugarcane fields.
- **Road markings and limits**: AS 1742.2 lines, R4-1 speed signs, Australian default limits by road class, continuous yellow no-stopping lines at signalised intersections.
- **Signs**: Give Way, traffic signals, guide posts, yellow diamond curve and turn warnings, chevron alignment markers, T-junction boards, Wrong Way, gravel-road signs; direction boards in the Australian style with the posted destinations of link roads.
- **Streetscape**: nature strips, Colorbond and paling fences, kerbs, utility poles, roundabout gardens, roadside crash barriers.
- **Street amenities**: payphones and post boxes from OpenStreetMap.
- Build tools (`tools/build_au_*.py`, `build_dests_au.py`, `build_places_au.py`), their data shards, models and textures, and `CREDITS.md` rows.

## Gating

Every rule is chosen by `climate.in_australia` / `climate.au_state` or the feature's own country (`AU`). Elsewhere nothing changes.
The digest tests pin other countries' output to what it was before: `test_nature_strips` (digests of the pre-change code),
`test_markings`, `test_speed_signs`, `test_buildings` (other regions' roofs and gables unchanged), `test_kantstolpar` (Sweden unchanged),
`test_surface`, `test_vegetation`. The `anchor_ready` additions (Koeppen class, the grass's dry share) are optional, so an older
mod or server still works.

## Performance

The added code costs about 0.006 s per tile in the fuel stage. What looks slow on a first visit to a place is upstream's
station detection and cold cells, not this change.

## Testing

- Python: `test_au_roadsigns`, `test_streetamenities`, `test_nature_strips`, `test_markings` (85 tests, OK), plus `test_updates`, `test_netfetch`, `test_reports_tool`, `test_launcher_update` (92 tests, OK).
- Lua: `tools/run_lua_tests.py`, 111 modules passing.
- In-game: driven across Queensland, from Brisbane suburbs to rural highways, on 1.9.x dev.

## Screenshots

<!-- placeholders: fill in before opening the PR -->
- Queenslander street: 
- Outback highway: 
- Coastal curves (chevron markers): 
- Tropical cane country: 
- Suburban nature strips and fences: 
- City payphone / post box: 

## Credits

NVIS Version 7.0 (Commonwealth of Australia, DCCEEW, CC BY 4.0); Natural Earth (public domain); Wikidata (CC0); OpenStreetMap
contributors (ODbL); Roadgeek 2014 font (licence in `tools/fonts`). Rows are in `CREDITS.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
