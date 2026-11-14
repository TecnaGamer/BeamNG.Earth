# Map category search

The search box offers Race tracks, Rally / gravel, Off-road, Peaks and Forests in one click. More opens the other
reviewed categories, each with a small inline SVG icon. A chip requests the full search once; it does not trigger
an additional autocomplete request. The backend combines nearby places with notable worldwide suggestions, so
there is no separate scope toggle. Typed `race tracks near Paris` or `peaks in Switzerland` still works.

Results carry a Category places, Name match, Address or Saved / recent label. Each row includes its kind, miles
from the unrounded player position (or named origin if no player position is available), and country when the source supplies it. Countries
are not guessed, and no reverse-geocoding request is added. Optional structured fields (`result_type`,
`distance_mi`, `country`) travel through the existing Lua result message unchanged. Older server results still
show their description, coordinates and a player-relative distance when coordinates are available.

Hover, keyboard selection or controller focus shows an amber map marker. A destination outside the current view
appears at the edge with an Outside view label; previewing does not pan the map or change the GPS destination.
Keyboard Up/Down wraps through the list; Enter or click keeps the existing set-GPS action. Controller A activates
a focused result; the hint says “A sets GPS, then X teleports”. X keeps the existing quick-teleport behavior, and Y keeps autodrive. The search hint states this
layout. Category buttons and results are native focusable buttons; no gamepad bindings have been reassigned.

Searching places and Searching wider states are distinct, including when earlier results remain visible. Empty,
offline/error and timeout states give a next step. Chips and hints are shown only while the search field/palette has focus or the query has text.
An empty, unfocused search has no palette. Credits remain outside the scrollable results; existing result
attribution stays visible whenever results are visible, even without focus or text. A new query immediately invalidates older callbacks, and map unmount cancels search debounce,
result polling and hover-preload timers. Preview projection uses a computed property with no added frame loop.

Offline checks:

```sh
node mod/tests/test_mapsearch.mjs
node mod/tests/test_mapmath.mjs
node mod/tests/test_mapstations.mjs
node server/tests/test_mapmath.mjs
cd server/tests
python3 -m unittest test_geocode test_geocode_categories test_netfetch.PolicyTest test_netfetch.RolesTest test_determinism.NoPlatformMathsTest test_ui_lua_calls
```

`test_mapsearch.mjs` exercises presentation and the search handlers extracted from EarthMap.vue with mocked Lua,
Vue refs and timers. It covers single-request chips, navigation, preview selection, stale responses, empty/error
states, widening during autocomplete, GPS activation and cleanup. It also syntax-checks the complete setup script.
It does not replace an in-game controller or Vue integration test; the game's Vue toolchain is not in this checkout.

`node mod/tests/preview_mapsearch.mjs /tmp/beamng-search-preview` generates five local HTML fixtures (results,
searching wider, empty, idle and focused). They use the actual search template/styles and a small fixture-only template adapter,
with fictional nearby destinations and no network resources. Open the files in a browser to inspect them.
Chromium headless capture was attempted in the managed environment but could not start: Crashpad's `setsockopt`
was denied by the sandbox. No in-game run or screenshots were taken.

## Category source review (2026-11-14)

All OSM selectors now require `name`, and blank or missing names are dropped in parsing. Line/area categories use
15 km (about 9.3 mi); race tracks, peaks, airports and dams use 50 km (about 31 mi). Small `in` regions retain the
radius restriction as well as their approximate bounds. `out center tags 200` and `[timeout:5]` remain. Overpass
has its own 4-second HTTP timeout and still trips its breaker on failure; Wikidata's timeout remains 7 seconds.

Wikidata now follows `P31/P279*` with the existing notability threshold and LIMIT 24, on Enter only and cached.
Reviewed motorsport roots (links checked outside tests):

- [Q2338524](https://www.wikidata.org/wiki/Q2338524): motorsport racing track.
- [Q926439](https://www.wikidata.org/wiki/Q926439): street circuit.
- [Q1232319](https://www.wikidata.org/wiki/Q1232319): kart circuit.
- [Q357121](https://www.wikidata.org/wiki/Q357121): dragstrip.
- [Q3924399](https://www.wikidata.org/wiki/Q3924399): linked as the Wikidata item by
  [Special stage (rallying)](https://en.wikipedia.org/wiki/Special_stage_(rallying)). Its item page could not be
  fetched, so its current hierarchy and instance coverage remain uncertain. Only coordinate-bearing instances
  qualify; it is not an assumption that a rally event's headquarters identifies a drivable stage.

The broad [Q1777138 race track](https://www.wikidata.org/wiki/Q1777138) root is deliberately omitted: it covers
non-motor racing too. The general race-track category instead uses the four confirmed motorsport roots above;
karting, drag and rally each have their own root. All four motorsport category queries explicitly exclude
[Q1248784 airport](https://www.wikidata.org/wiki/Q1248784) and
[Q644371 international airport](https://www.wikidata.org/wiki/Q644371), including subclasses.

The remaining existing roots were checked against their item labels and kept: mountain
[Q8502](https://www.wikidata.org/wiki/Q8502), mountain pass
[Q133056](https://www.wikidata.org/wiki/Q133056), forest [Q4421](https://www.wikidata.org/wiki/Q4421), lake
[Q23397](https://www.wikidata.org/wiki/Q23397), beach [Q40080](https://www.wikidata.org/wiki/Q40080), desert
[Q8514](https://www.wikidata.org/wiki/Q8514), airport [Q1248784](https://www.wikidata.org/wiki/Q1248784), bridge
[Q12280](https://www.wikidata.org/wiki/Q12280), tunnel [Q44377](https://www.wikidata.org/wiki/Q44377), dam
[Q12323](https://www.wikidata.org/wiki/Q12323), canyon [Q150784](https://www.wikidata.org/wiki/Q150784).
Subclass traversal allows typed forests, bridges, tunnels, etc. Geographic families still do not guarantee a
road entrance, vehicle access, or that a tunnel is a road tunnel. No unverified Q-ids were added for off-road,
rock crawling, motocross or scenic drives; these remain OSM-only for category discovery.
