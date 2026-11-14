# Map category search

The search box offers Race tracks, Rally / gravel, Off-road, Peaks and Forests in one click. More opens the other
reviewed categories, each with a small inline SVG icon. A chip requests the full search once; it does not trigger
an additional autocomplete request. The backend combines nearby places with notable worldwide suggestions, so
there is no separate scope toggle. Typed `race tracks near Paris` or `peaks in Switzerland` still works.

Results carry a Category places, Name match, Address or Saved / recent label. Each row includes its kind, miles
from the query origin (from the player for ordinary results), and country when the source supplies it. Countries
are not guessed, and no reverse-geocoding request is added. Optional structured fields (`result_type`,
`distance_mi`, `country`) travel through the existing Lua result message unchanged. Older server results still
show their description, coordinates and a player-relative distance when coordinates are available.

Hover, keyboard selection or controller focus shows an amber map marker. A destination outside the current view
appears at the edge with an Outside view label; previewing does not pan the map or change the GPS destination.
Keyboard Up/Down wraps through the list; Enter or click keeps the existing set-GPS action. Controller A activates
a focused result; X keeps the existing quick-teleport behavior, and Y keeps autodrive. The search hint states this
layout. Category buttons and results are native focusable buttons; no gamepad bindings have been reassigned.

Searching places and Searching wider states are distinct, including when earlier results remain visible. Empty,
offline/error and timeout states give a next step. Credits remain outside the scrollable results and stay visible
in all states. A new query immediately invalidates older callbacks, and map unmount cancels search debounce,
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

`node mod/tests/preview_mapsearch.mjs /tmp/beamng-search-preview` generates three local HTML fixtures (results,
searching wider and empty). They use the actual search template/styles and a small fixture-only template adapter,
with fictional nearby destinations and no network resources. Open the files in a browser to inspect them.
Chromium headless capture was attempted in the managed environment but could not start: Crashpad's `setsockopt`
was denied by the sandbox. No in-game run or screenshots were taken.
