# Australia: regional scenery

BeamNG.Earth's default look is North American, which reads wrongly in Australia. This adds an Australian look, worked out
region by region (first in Queensland) and then for the
other states and territories. Everything is generated from the map data at runtime; no third-party model or texture is bundled.

## What it adds

- **Buildings**: a house style for each state and territory (Colorbond and tile roofs, brick and weatherboard walls,
  verandahs, gable ends), including the high-set timber Queenslander on stumps.
- **Vegetation**: the trees, shrubs and ground cover of a place follow the NVIS Major Vegetation Group there (eucalypt
  woodland, mallee, scrub, mangrove, dry rainforest and so on, `server/data/veg_palettes.json`). Grass goes dry and
  tawny through the dry season (southern-hemisphere months), and the tropics and Australia keep their leaf colour all year.
- **Ground**: red and ochre soils in dry country, sugarcane fields on the Queensland coast.
- **Road markings** (AS 1742.2): white separation, lane and edge lines, no-overtaking barrier lines, and the continuous
  yellow no-stopping line on the approach to a signalised intersection.
- **Signs** (AS 1742): R4-1 speed signs with the posted steps and Australian default limits by road class, Give Way,
  traffic signals on mast arms, guide posts with red and white reflectors, yellow diamond curve and turn warnings,
  chevron alignment markers, the T-junction board, Wrong Way, and the gravel-road warning. The direction boards use
  green with white lettering and read the posted destinations of motorway and trunk link roads.
- **Streetscape**: nature strips (kerb and channel, grass verge, footpath), Colorbond and paling boundary fences, concrete
  utility poles, gardens in roundabouts, and roadside crash barriers where the road itself says one stands.
- **Street amenities**: payphones and post boxes from OpenStreetMap.

## Gated to Australia

Every rule is chosen by `climate.in_australia(lat, lon)` or the state lookup `climate.au_state`, or by the feature's own
country (`AU`). Outside Australia the output is unchanged: the existing digest tests (for example `test_nature_strips`,
`test_markings`, `test_speed_signs`, `test_buildings`) pin the other countries' output to what it was before. Protocol
additions (the Koeppen class and the grass's dry share in `anchor_ready`) are optional, so an older mod or server still works.

## Data sources and licences

| Data | Source | Licence |
|---|---|---|
| Vegetation (`server/data/au_mvg_v7.npz`) | NVIS Version 7.0 Major Vegetation Groups, Commonwealth of Australia (DCCEEW) | CC BY 4.0 |
| State and territory boundaries (`au_states.json.gz`) | Natural Earth 1:10m admin_1 | public domain |
| Place populations | Wikidata (CC0) and Overture | CC0 / as the places snapshot |
| Link-road destinations, payphones, post boxes | OpenStreetMap contributors | ODbL 1.0 |
| Lettering on the signs | Roadgeek 2014 Series EEM (`tools/fonts`, `Roadgeek-LICENSE.txt`) | see the licence file |

The full rows with the required credit lines are in `CREDITS.md`.

## Standards followed

Road signs and markings follow AS 1742 (Manual of uniform traffic control devices), Part 2 (signs and markings), Part 4 (speed
controls), as drawn by TfNSW, TMR (MRS45) and IPWEA. `docs/research/2026-10-28-road-markings-standards.md` records the
dimensions used. Left-hand traffic is assumed throughout.

## Tools and tests

`tools/build_au_*.py` build the sign and streetscape models and the data shards; each is reproducible from its public
source. Tests: `server/tests/test_au_roadsigns.py`, `test_au_streetscape.py`, `test_au_traffic.py`, `test_aurails.py`,
`test_nature_strips.py`, `test_streetamenities.py`, `test_nostopping.py` and the Australian cases in `test_markings.py`,
`test_speed_signs.py`, `test_buildings.py`, `test_surface.py` and `test_vegetation.py`.
