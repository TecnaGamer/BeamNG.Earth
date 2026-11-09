# Add-ons

Add-ons are mods and tools other people make for BeamNG.Earth. The in-game **Add-ons** settings page lists a short,
curated set of them, shows each one's README and changelog from its own GitHub page, and installs, updates, turns off
and on and removes the ones that are game mods. Other mods talk to BeamNG.Earth through its modding API
(`beamngEarth_api`); programs beside the game read the player's position from the companion feed.

The first two add-ons:

- **Worldwide Trucking Career: Dangerous Contracts** (MadMerick01/BeamNG-Worldwide-Trucking-Career-Dangerous-Contracts):
  a game mod (a zip in the mods folder), released as `worldwide-trucking-dangerous-contracts.zip`, tags `build-NNN`,
  several builds a day.
- **BeamNG.Earth Live GPS** (Leopri2008/BeamNG.Earth-LiveGPS): a Python program beside the game plus an Android app,
  not a game mod. The page shows it with its README, changelog and download buttons.

Both licences forbid redistribution: BeamNG.Earth never bundles or mirrors their files. The player's own BeamNG.Earth
app downloads each from its author's GitHub releases.

## How it works

```
Add-ons page (EarthAddons.vue, earthAddonsStore.js)
   | bngApi.engineLua: beamngEarth_settings.uiAddon*  (the game's UI browser cannot fetch anything itself)
   v
addons.lua (the mod)  --- addons_list / addon_detail / addon_install / addon_applied --->  addons.py (the server)
                      <-- addons / addon_detail / addon_status / addon_apply -----------       |
   |                                                                                          | api.github.com:
   | core_modmanager (the game's own mod manager):                                            |   releases, README
   |   new: FS:renameFile(<file>.bnge-tmp -> <file>), its file watcher mounts it              | github.com:
   |   update: updateZipMod(<file>, <file>.bnge-tmp)                                          |   the release file
   |   on / off / remove: activateMod / deactivateMod / deleteMod                             v
   v                                                                              <user>/mods/<file>.bnge-tmp
<user>/mods/<file>.zip                                                            (checked: size, SHA-256, a zip)
```

### The registry

`server/data/addons.json`, bundled with the app (format 1). Only its repos can be installed: there is no "paste any
URL". Since 1.9.0 the list players see is the online copy, `addons.json` in the BeamNG.Earth-data repo
(`addons.REGISTRY_URL`, read through raw.githubusercontent.com), so add-ons are added and taken off without a release;
an edit there reaches players within a few hours. The bundled list stands in when the online one can't be read or has
no valid entry (an empty `addons` list takes every add-on off). Treat an edit there like a release: only repos whose
authors agreed and whose files were checked. Per add-on:

| field | meaning |
|---|---|
| `id` | short name, `[a-z0-9-]` |
| `name`, `author`, `summary`, `licence`, `note` | what the page shows (`note`: optional) |
| `repo` | `owner/name` on GitHub: the only place its files and text come from |
| `kind` | `beamng-mod` (a zip for the mods folder), `companion` (a program beside the game), `link` (text and downloads only) |
| `asset` | a regular expression for the release file's name |
| `modFile` | (`beamng-mod`) the zip's name in the mods folder |
| `oldFiles` | (`beamng-mod`) older names of the same mod, removed when it is installed |
| `uiApps` | (`beamng-mod`) its UI apps, named in the "installed" message |
| `links` | extra downloads `[{label, asset}]` (Live GPS's APK and script) |
| `trust` | `latest` (any release of the author's) or `reviewed` (only files listed in `reviewed`) |
| `reviewed` | `[{tag, sha256}]`: files BeamNG.Earth has looked through (a badge on the page) |
| `feedOrigins` | local web pages of the add-on that may read the companion feed (`http://127.0.0.1:<port>`) |
| `minEarth` | the oldest BeamNG.Earth version it works with |

### GitHub, frugally

Without a token GitHub allows 60 API requests an hour per address. The page asks one request per add-on for its
releases (`/releases?per_page=8`) when the list opens, and one more for its README when the player opens it. Each
answer is kept on disk (`<cache>/addons/`) for an hour, then asked again with `If-None-Match` (a 304 is the same
answer); when GitHub does not answer, the copy on disk is shown. The requests go through netfetch's api.github.com
policy in a lane of their own (`addons`), so they never use up the update check's daily budget. **Check for updates**
asks again at once.

### The text: README and release notes

GitHub Markdown is turned into HTML on the server by the Rust kernel `md_html` (`native/earthfast/src/mdhtml.rs`,
pulldown-cmark): every tag in the output is written by us from the parser's events and every piece of text is
escaped, so raw HTML in a README (`<img>`, `<details>`, `<script>`, event attributes) never reaches the page; a lone
`<br>` stays a line break. Links have no `href`: each is `<a class="md-link" data-i="k">`, `k` an index into the list
of absolute https URLs sent with it; `javascript:`, `data:`, `http:`, `mailto:` and in-page links stay plain text.
Relative links resolve against the README's place on GitHub. Images are not loaded: each is a link "[Image: alt]".
Release notes are cut at their first `<details>` line (Trucking folds every earlier build's notes into one; the page
lists the releases one by one instead).

A click on a link asks the server to open it in the browser (`open_page`); the server opens only a URL that was in
an add-on's text or is a page of a registry repo, besides BeamNG.Earth's own links (`addons.Addons.openable`,
`updates.open_page(allow=...)`).

### Installing a game mod

Replacing a mod's files under a running game broke its UI apps (2026-10-24), so the server never touches the mods
folder's real files:

1. **Install / Update** the first time for an add-on shows a notice: who made it, that BeamNG.Earth did not make it
   (and whether it reviewed that version), that it runs like any mod with the same access, where it comes from, and
   which older copies are removed. The agreement is kept per add-on and author (`/settings/beamngEarth_addons.json`);
   a new author in the registry asks again. The server refuses an install without it too.
2. The server picks the newest release that is not a pre-release and has the file (with `trust: reviewed`: a reviewed
   file only), downloads it to `<mods>/<modFile>.bnge-part`, checks its size and the SHA-256 digest GitHub publishes
   for the asset, that it is a readable zip with no absolute or `..` names and the game's folders at its top, then
   renames it to `<modFile>.bnge-tmp`. The game's mod manager only looks at `*.zip`: it ignores both names.
3. The mod hands it over when `core_modmanager.isReady()` (at most 60 s): a new add-on is renamed into place with
   `FS:renameFile` and the game's own file watcher mounts and turns it on (if it has not after 3 s, the mod asks
   `core_modmanager.workOffChangedMod`); an update goes through `core_modmanager.updateZipMod(file, file.bnge-tmp)`,
   the call the game's own download command uses. A mod that was turned off is turned off again. A file in the folder
   that the mod manager does not know (copied by hand while the game ran) is replaced directly instead, because
   `updateZipMod` fails on those.
4. The mod tells the server (`addon_applied`), which logs it and sends the list again. **Turn off**, **Turn on** and
   **Remove** are `deactivateMod`, `activateMod` and `deleteMod`, as on the game's own mods page.

Never while a BeamMP server is joined (BeamMP turns mods off and on itself). After an update the game has the new
files at once, but the add-on's Lua code that was already loaded keeps running until BeamNG.drive starts again; the
page says so. Leftover `.bnge-part` / `.bnge-tmp` files older than a day are removed when the server starts.

What is installed is found from the file itself: its SHA-256 compared with the release files' digests gives the
version ("unknown" for a copy installed by hand that matches none of the last releases).

### Trust model (the user's decision)

The code supports two policies per add-on, and the badge:

- **A, latest** (`trust: latest`, the default now): the author's newest release, shown as third-party and not
  reviewed. Trucking ships several builds a day; this keeps up with them.
- **B, reviewed only** (`trust: reviewed`): only files whose SHA-256 is in `reviewed`. Safe, but every update waits
  for a review and a registry change.
- **C, latest with a badge**: `trust: latest` plus `reviewed` entries: the newest release installs, a reviewed one
  shows "Reviewed by BeamNG.Earth" (on the card and in the notice).
- **D**: where the page lives: in game only for now; the launcher window could show the same list later.

## For add-on authors

To be listed: a public GitHub repo with releases; the file to install attached to each release under a stable name
(GitHub publishes a SHA-256 digest for every release asset; the page refuses a file without one); for a game mod, the
game's folders (`lua/`, `ui/`, `scripts/`, `vehicles/` ...) at the zip's top, not inside a folder. Release notes in
the release body (Markdown); a README with the description. Tell us your old file names if you renamed the zip.

Later (phase 4) an add-on can describe itself with `beamng-earth-addon.json` at its repo's root (kind, asset name,
mod file, old names, UI apps, compatible BeamNG.Earth versions, a companion's command and port); the registry entry
then only names the repo and whether it is listed.

## Modding API v1: `beamngEarth_api`

A GE Lua extension (`lua/ge/extensions/beamngEarth/api.lua`) for other mods, instead of reading BeamNG.Earth's
internals (`beamngEarth_core._state()`, `require('beamngEarth/gps')` ...), which change between versions. Names and
fields change only with a new major version; additions bring a new minor version and a `has()` name.

```lua
local api = rawget(_G, 'beamngEarth_api')     -- nil: BeamNG.Earth not installed, or older than the API
if api and api.version() == 1 then
  if api.ready() then
    local p = api.pose()
    print(p.lat, p.lon, p.heading, p.speed)
  end
end
```

| function | returns |
|---|---|
| `version()` | `major, minor` (1, 2) |
| `has(name)` | `'pose'`, `'place'`, `'teleport'`, `'gps'`, `'notify'`, `'events'`, `'options'` (1.1), `'gpsRoute'` (1.2) |
| `earthVersion()` | the app's version (`"1.8.2-beta"`) once connected, else nil |
| `connected()` | the BeamNG.Earth app connected |
| `inWorld()` | the BeamNG.Earth level loaded |
| `ready()` | the world loaded round the player, no teleport under way (a rebase does not count) |
| `pose()` | `{lat, lon, alt, heading, speed, ve, vn, vu, camera, x, y, z, anchorLat, anchorLon}` or nil |
| `position()` | `lat, lon` or nil |
| `place()` | `{street, place, region, country}` (each optional) as the status bar shows it, or nil |
| `teleport(lat, lon, name)` | true once taken (as the map's Go), else `false, why` |
| `gpsSet(lat, lon, name)`, `gpsClear()`, `gps()` | the GPS destination: `gps()` is `{lat, lon, name, remaining, eta, planning, waypoints, legs}` or nil |
| `gpsSetRoute(points)` | 1.2: the GPS through waypoints, `points` `{{lat, lon, name?}, ...}`, the last one the destination, at most 11; one route through them all, each waypoint passed in order (within 40 m). `gps().waypoints` is the ones still ahead, `gps().legs` the planned route's `{dist m, dur s}` per leg. True, or `false, why` |
| `notify(text, seconds)` | a short message on screen |
| `on(event, fn)`, `off(handle)` | listeners for the events below |

`pose()`: lat / lon in WGS84 degrees; `alt` m over the WGS84 ellipsoid; `heading` degrees clockwise from true north
(the car's direction; nil with no car); `speed` m/s; `ve`, `vn`, `vu` m/s east, north, up (true north, not the
world's frame: they differ by up to a degree or so far from the world's origin); `camera` true when there is no car
and the free camera stands in; `x, y, z` the game's coordinates (metres east, north, up of the origin
`anchorLat, anchorLon`, which moves as the player travels: never keep game coordinates across a rebase).

Events, as game hooks (any extension defining the function gets it) and as `api.on` listeners:

| hook | `on` name | info |
|---|---|---|
| `onBeamNGEarthWorldReady` | `worldReady` | `{lat, lon}`: after the level loads and after each teleport |
| `onBeamNGEarthTeleported` | `teleported` | `{lat, lon}`: a teleport finished, the player is set down |
| `onBeamNGEarthRouteArrived` | `routeArrived` | `{lat, lon, name}`: the GPS destination reached |
| `onBeamNGEarthWaypointReached` | `waypointReached` | `{lat, lon, name, left}`: a GPS waypoint passed (1.2), `left` still ahead |

Next for v1.x (proposals): `session` events (BeamMP join / leave), the player's own settings that matter to
add-ons (units), an add-on panel inside the Add-ons page, and road information at the player (the road's name and
class from BeamNG.Earth's map data).

## Companion feed

`server/companionfeed.py`, on by default for this computer only (`--companion-feed local`; `lan` also answers the
local network, `off` turns it off; port `--companion-port`, 47615). Read-only, GET only:

| path | answer |
|---|---|
| `/v1/info` | `{"api": 1, "app": "<version>"}` |
| `/v1/pose` | `{"ok", "lat", "lon", "speed", "ve", "vn", "bearing", "anchorId", "t", "age"}` or `{"ok": false}` |
| `/v1/stream` | server-sent events: one `data: <pose>` per pose (about two a second), a keep-alive comment every 15 s |

`bearing` is the velocity's direction (null below 0.5 m/s); `ve`, `vn` are in the world's frame. Only requests that
name this computer in their Host header are answered in `local` mode (a web site whose name points at 127.0.0.1
cannot read it), and web pages of other origins can read the answers only when a registry entry lists their origin
(`feedOrigins`). It replaces Live GPS's relay: no `-bngeport` launch option, nothing between the game and the world
server.

## Proposals for the first two authors (drafts for the user to pass on)

**MadMerick01 (Trucking):**

1. Use `beamngEarth_api` in `earthAdapter.lua` when it is there (version 1), and keep the current sources as the
   fallback for older Earth versions. A local copy of the adapter changed this way (about 60 lines: `sampleApi`, and
   `placeNames` / `setDestination` / `teleport` through `api.place` / `api.gpsSet` / `api.teleport`) passes a test
   against the real `api.lua`; `tools/check_earth_versions.py` would then only need to check `api.version()`.
2. `onBeamNGEarthRouteArrived` and `onBeamNGEarthTeleported` could replace polling for arrival and for the end of a
   teleport ("earth_starting").
3. Keep the asset name `worldwide-trucking-dangerous-contracts.zip` stable (the page finds it by name) and tell us if
   it changes; the old names `earth-freight-sim.zip` / `earth_freight.zip` are removed on install, as the README asks.
4. Release notes: the folded `<details>` part is cut by the page (each build is shown on its own); a short first
   section per build reads best there.
5. The Street View page opens a local `127.0.0.1` address through a file in the settings folder and
   `Engine.Platform.openFile`, because `openWebBrowser` refuses it. That works; if they want, the companion server's
   `open_page` could open add-on pages too (it opens only listed addresses).

**Leopri2008 (Live GPS):**

1. Read the position from the companion feed (`http://127.0.0.1:47615/v1/stream` or `/v1/pose`) instead of the
   relay: no `-bngeport 47614` launch option, and the script is no longer in the game's connection to the world
   server (when the relay stops, the world stops loading today). The pose has the same fields it reads now (`lat`,
   `lon`, `speed`, the frame velocity `ve`/`vn`, `anchorId`).
2. A packaged program in the releases (a Windows .exe, a Linux AppImage), so players without Python can use it and
   the Add-ons page can start and stop it later (phase 3).
3. Security of its own HTTP server: it listens on every address (0.0.0.0:47700) with `Access-Control-Allow-Origin: *`,
   and `POST /reset`, `/fog/edit`, `/fog/reset` take no token: any web page open in the player's browser can reset
   their trip and fog of the world (a cross-site request), and anyone on the network can read the position. A
   Host / Origin check like the feed's and a token for the POSTs would close that. Nothing malicious was found in
   v1.3.
4. Asset names: the script's name changes per release (`bne_live_coords.v1.3.py`, `bne_live_coords-v1.2.py`); one
   stable name (or a stable pattern) makes it easy to offer the right file.

## Status and phases

- Phase 1 (done in code, not yet tried in game): registry, GitHub fetch and cache, list and detail pages with the
  README and changelog.
- Phase 2 (done in code, not yet tried in game): install / update / turn off / on / remove for game mods, the notice,
  the checks.
- Phase 0 (API v1): `beamngEarth_api` and the companion feed implemented with tests; the proposals above to send.
- Phase 3: companions (Live GPS) started and stopped from the page once it ships a packaged program; `link` add-ons.
- Phase 4: `beamng-earth-addon.json` manifests in the add-ons' repos; the registry online in the BeamNG.Earth-data repo (done in 1.9.0).

## Changes apply without restarting the game

The game's mod manager runs a mod's `modScript.lua` files when it mounts or turns on a mod, but an extension that is
already loaded stays loaded: turning a mod off or removing it unloads nothing, and after an update `extensions.load`
of the old (still loaded) extension does nothing. So with every hand-over the server sends the mod's code as it reads
it from the zip (`addonrun.mod_code`: its own GE extensions `lua/ge/extensions/a/b.lua` -> `a_b`, their module names,
whether it has `ui/` files), and the mod (`addons.lua`):

- before an **update**, **Turn off** or **Remove**: unloads those of its extensions that are loaded and forgets its
  required modules (`package.loaded`), so the new version's code is what runs (or none);
- on **Install**, **Update** and **Turn on**: the mod manager runs its modScripts, which load the new code;
- when it has UI files: reloads the UI a moment later (`reloadUI`), so its apps appear or go.

BeamNG.Earth's own extensions are never unloaded. A mod's files that replace the game's own (`lua/ge/extensions/core/...`:
found from the game's folder, `BNGEARTH_GAME_DIR`) are not unloaded live: the page asks for a restart for those, and
for vehicle Lua says it applies when the vehicle is spawned again.

## Programs beside the game (companions)

A `companion` entry with `run` options is installed and run from the page:

| run field | meaning |
|---|---|
| `args` | arguments (`--feed`) |
| `page` | its own web page on this computer (`http://127.0.0.1:<port>/...`): the **Open its page** button |
| `port` | the port it serves |
| `lan` | it serves the player's Wi-Fi too (a phone): the page shows the address to type there |

Install copies the release's Python file (checked against GitHub's SHA-256) into `<data>/addons/<id>/`. **Start** runs
it with BeamNG.Earth's own Python (`BeamNG.Earth --run-addon FILE ARGS`; the dev tree: `python3 launcher.py
--run-addon ...`), so players need no Python, with the companion feed's address in `BNE_FEED`; it ends with the server,
and its output goes to `run.log` beside it. **Starts with BeamNG.Earth** starts it when the server starts. A companion
entry without `run` is downloads only.

## The local list (try an add-on first)

`<data>/addons/local.json` (Linux: `~/.cache/beamng-earth/addons/local.json`; Windows:
`%LOCALAPPDATA%\beamng-earth\addons\local.json`), the registry's format, each entry with `local: {file, tag}`
(an absolute path on this computer). Shown with a **Local** badge; an entry with a registry id takes that one's place
(a changed copy tried before its author releases it); its file is installed with its own SHA-256, and GitHub's releases
still make its changelog. Only that file can add local entries.

Example: Live GPS with its `--feed` mode (reads the position from the companion feed instead of the relay, so no
`-bngeport` launch option):

```json
{"format": 1, "addons": [{"id": "live-gps", "name": "BeamNG.Earth Live GPS", "author": "Leopri2008",
  "repo": "Leopri2008/BeamNG.Earth-LiveGPS", "kind": "companion", "summary": "...", "licence": "...",
  "asset": "^bne_live_coords.*\\.py$",
  "local": {"file": "/path/to/bne_live_coords.py", "tag": "v1.3-feed"},
  "run": {"args": ["--feed"], "page": "http://127.0.0.1:47700/map", "port": 47700, "lan": true}}]}
```

## Options (an add-on's own settings)

An add-on declares its options and the page's **Options** tab shows them. The declaration comes from its list entry
(`options`), else from a `beamngEarth_addon.json` the author ships (a game mod: at its zip's top; a repository
added from GitHub: at its root):

```json
{"options": [
  {"key": "port", "label": "Port", "type": "int", "default": 47700, "min": 1024, "max": 65535, "arg": "--port",
   "help": "The port its live map and the phone app use."},
  {"key": "units", "label": "Units", "type": "choice", "choices": ["km/h", "mph"], "env": "BNE_UNITS"},
  {"key": "fog", "label": "Fog of the world", "type": "bool", "default": true, "arg": "--fog"}
],
 "run": {"args": ["--feed"], "page": "http://127.0.0.1:{port}/map", "lan": true}}
```

| field | meaning |
|---|---|
| `key` | `[a-z][A-Za-z0-9_]*`, at most 16 options |
| `type` | `int` (with `min` / `max`), `bool`, `text` (200 characters), `choice` (with `choices`) |
| `label`, `help` | what the tab shows |
| `default` | its value until the player changes it (checked like any value) |
| `arg` | a program beside the game gets `arg value` (a bool: `arg` alone when on) |
| `env` | ... and / or this environment variable (names starting with `BNE_` only) |

The values are kept in `<data>/addons/<id>/options.json`; each saved value is checked against its option. A running
program is restarted with them when they are saved; its `run.page` may name them (`{port}`), so **Open its page** and
the Wi-Fi address follow. A game mod reads them with `beamngEarth_api.addonOptions(id)` (`{key = value}`, the
defaults until saved; API 1.1, `has('options')`) and hears of a change through `onBeamNGEarthAddonOptions(info)`
(`info.id`, `info.values`) or `api.on('addonOptions', fn)`.
