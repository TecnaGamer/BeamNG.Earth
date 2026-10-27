# BeamNG.Earth

A 1:1 scale, streamed Earth for [BeamNG.drive](https://www.beamng.com/): drive anywhere on the
planet on real elevation data and satellite imagery, with distance-based level of detail and the
real curvature of the Earth.

Status: beta (see VERSION). Tested on BeamNG.drive 0.40.3 on Linux (Vulkan) and the Steam Deck. See
[docs/ROADMAP.md](docs/ROADMAP.md) for what is coming.

**Community:** join the [Discord server](https://discord.gg/tgf) and head to the #beamng-earth channel to talk about
the mod, report problems, and get an IP to join a BeamMP.Earth server (see [Multiplayer](#multiplayer-beammp)).

You can help support development by reporting [issues](https://github.com/TecnaGamer/BeamNG.Earth/issues), contributing code and sending pull requests, or [donating](https://ko-fi.com/tecnagamer). Thank You!

## Features (working today)
**The world**
- The whole planet at 1:1 scale on real elevation: national lidar where it exists (Mapterhorn: much of
  Europe, the US and more), Copernicus 30 m elsewhere with buildings and small tree stands filtered out of it.
- Aerial and satellite imagery on the ground: open state and national orthophotos (Maryland's 15 cm photos,
  a dozen more US states, NAIP across the US, Switzerland, France, Spain, the Netherlands, Germany's states,
  Austria, Poland, Czechia, Japan, parts of Australia and Canada, and more), Sentinel-2 cloudless everywhere else; shadows and
  colour seams taken out of the photos, the season's colours graded in, cliffs drawn as rock.
- A real view distance out to the horizon, with the Earth's curvature.
- Both poles: Antarctica's ice sheet up to the South Pole (2,835 m) from the Copernicus DEM, the Arctic's sea ice
  at the North Pole, the polar day and night of the real sky, and a minimap compass that points to true north.
- Ground materials from land cover (grass, dirt, rock, sand, snow, mud, lawns, paved squares and parking
  lots) with 3D grass and bushes.
- Trees and forests from a canopy height model, regional species (conifers, broadleaf, palms, desert
  shrubs), autumn colours in season. **Tree density** under World options offers Very Low, Low, Medium, High
  (the original density), Very High and Max. Changes apply gradually; higher presets fill out distant woods
  up to the available canopy data. Rocks, shrubs and road signs keep their normal density.
- Buildings from Overture / OpenStreetMap with regional styles (US siding and brick, Alpine chalets, Nordic
  wood, Mediterranean stucco, East Asian and Latin American looks), real heights, roofs, windows, shopfronts,
  porches, churches with steeples, glass towers, far-away buildings drawn as a skyline.
- Roads painted into the ground itself (no floating road surface), with lane markings, sidewalks and curbs, curb ramps, storm drains, cobblestone streets,
  traffic islands, roundabouts, rural motorways with shoulders and toll booths; the ground graded under them, with
  retaining walls where a road is cut into a hill (dry stone in the mountains and towns, concrete along major roads)
  and stone parapets along mountain roads' drops; street lights, utility poles with wires, traffic signals,
  stop signs and speed-limit signs (mph or km/h by country), traffic lights and sidewalks where OpenStreetMap maps them;
  railway lines as 3D track (ballast, sleepers and rails, rail bridges on piers, the ground cut and filled for the
  line) and tram tracks (street track where trams run on the road).
- Gas stations with pumps, canopies and forecourts in the region's style, where the stock refuel prompt fills the
  tank.
- Highway exit and advance signs, overhead destinations with route shields, merge warnings and Interstate mile
  markers. Toggle **Highway Signs** under World settings; coverage follows mapped sign data and the bundled mile
  references. See [docs/HIGHWAY_SIGNS.md](docs/HIGHWAY_SIGNS.md) for this custom migration.
- Bridges drawn as their real type (beam, truss, arch, cable-stayed, suspension) with real heights and spans, piers
  that keep clear of the roads under them and 6 m (20 ft) of headroom over roads where the ground allows; a divided
  road's two decks joined across a narrow median; tunnels through hills, with portals, lights and low arches where
  little is over them.
- Water: the sea with real depth (cars sink), lakes, and rivers kept beside the roads.
- Airports with graded, marked runways; racetracks (circuits, ovals and pit lanes, with their real banking);
  landmarks such as the pyramids of Giza.

**Time, weather and physics**
- **Real time**: the sun, moon and stars are where they really are for the place, the date and the clock
  (the real time zone); the clock runs in real time. Change the time in the pause menu and the mod hands it
  over to you (switch back to Real any time).
- **Real weather** at the player from Open-Meteo, updated as you travel: cloud cover and high cirrus, clouds
  drifting with the wind aloft, fog from the real visibility, ground wind with gusts that pushes the cars,
  rain and snow, the air temperature, and the sun dimming under overcast. It goes through the game's own
  weather system, so the Time & Weather menu shows the real values; a manual mode leaves it to you.
- **Seasons**: the photos' colours and the trees' leaves follow the time of year (autumn colours, bare
  trees in winter where the photos were taken leaf-off).
- **Real gravity**: the WGS84 gravity for your latitude and altitude plus the EGM2008 local anomalies, so
  gravity really is weaker on a mountain or at the equator.

**Driving and traffic**
- Drive anywhere: the physics ground follows the car at 100+ mph, the world origin moves with you
  (floating origin), and the world streams ahead of you; you never fall out of the world.
- Buildings, sidewalks, curbs and bridge decks are solid (lean collision meshes near the car keep the
  physics' collision rebuilds cheap).
- Fast teleports: right after a teleport, while you wait, the world server may borrow some of the game's
  CPU cores (at idle priority) to build the world faster (Linux; `--load-boost off` turns it off). It builds
  2 to 4 road pieces at once, by your computer's CPU threads and memory.
- Places you've been load much faster the next time: what was built around you (roads, buildings, the levelled
  ground, the distant land) is kept on disk, up to 2 GB (the oldest go first; the environment variable
  `BNE_REVISIT_MB` sets the size, `BNE_REVISIT=off` turns it off).
- **AI support**: the mod builds BeamNG's road graph (the AI's map) from the real roads around you, with
  lanes, one-way streets, speed limits (mapped, or the country's defaults), the country's driving side and
  road rules, and traffic signals at real junctions. The stock AI, traffic, police and the AI modes in the
  vehicle menu work on it.
- **Traffic**: stock BeamNG traffic and police can be switched on, and parked cars line the streets and fill
  parking lots (stock BeamNG parking).

**Multiplayer (BeamMP, in testing)**
- Play the real world together on a BeamMP server (every player runs the BeamNG.Earth app, which starts BeamMP with
  the game): BeamNG.Earth servers are their own **BeamNG.Earth** category in BeamMP's server browser, players who join are
  sent to the others, cars show in the right place and with their crash damage in everyone's world, and other players
  show on the world map (see "Multiplayer" below).

**Map and navigation**
- A level select with famous places and a **Random location** button (it starts you on a road).
- World map (**M**): pan and zoom the globe, satellite or street map (EOX) with a roads & labels overlay,
  borders and place names (the **Layers** button), search for addresses and places as you type (names in the
  local language in German-, French- and Italian-speaking countries), teleport anywhere. Works with a controller,
  laid out like the stock map.
- GPS routes to any destination (OSRM on OpenStreetMap), with the stock route arrows on the road and the route
  on the game's minimap (bottom left), which shows the roads around you. The first 20 km (12 miles) of the
  route's map data and elevation download ahead while you drive, with a progress bar on the map.
- **Autodrive**: the car drives itself along the GPS route (in lane, legal speed, minding traffic).
- Saved places and recent places; "Where you left off" in the level select puts you back where you last drove (it
  is saved as you drive, at teleports and when the game closes).

**Interface and tools**
- A slim status bar (street, town, country, and under it the loading progress of the ground, roads & buildings, trees and
  distant land after a teleport), the Earth coordinates above the minimap, a connection banner when something is
  wrong and the data credits on screen, as BeamNG UI apps in a ".Earth" app layout (based on Freeroam's;
  move or resize them in the UI apps editor), and a one-time welcome page about the project.
- Settings in the pause menu, keybinds, in-game reports (notes and performance recordings to share), and a
  wireframe view (Ctrl+Alt+W) that shows how the ground, the distant terrain and the roads are built.
- A diagnostic log to send when something is slow: the world server's `logs/server.log` (in
  `%LOCALAPPDATA%\beamng-earth` on Windows, `~/.cache/beamng-earth` on Linux) records your hardware and settings, every
  hitch and frame-rate drop with where it happened and what was loading, and a summary every minute
  ([docs/QUICKSTART.md](docs/QUICKSTART.md#something-wrong-tell-us)).
- Easy setup: one download per system (Windows .exe, Linux AppImage) that installs the mod and starts the server
  and the game; from the source, `install.py` installs everything and `start.py` keeps the mod in sync.
- Runs on a Steam Deck (aim: 30 fps on the Handheld graphics preset).
- Uses your graphics card for some of the world server's work (colour matching of the photos, compressing the distant
  textures) when it can, so the server needs less of your CPU; without a usable one the CPU does it as before.
- Offline play from the download cache (with a size cap and "clear cache").
- Updates: the mod tells you in game (and on its settings page, with a link) when a newer version is on GitHub;
  with the downloaded app (.exe / AppImage), **Update now** downloads it next to your copy, checks it against
  GitHub's checksum, and the next time you start BeamNG.Earth the new version starts (your old file and shortcut
  keep working).

## How it works
- A **companion server** (`server/`: Python, with its heavy work in a Rust module, `native/earthfast`) downloads
  elevation, imagery and map data, builds terrain, roads, buildings and the rest in a local flat frame around a
  moving origin, and streams them to the game over a local socket. The game's Lua cannot reach the internet, so the
  server is required.
- The **mod** (`mod/beamng_earth/`) loads the tiles as meshes, keeps an invisible physics terrain
  under the car, moves the world origin as you drive (floating origin), and blends level-of-detail
  changes so nothing pops.
- Design and measurements: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [docs/V1_4_SPEC.md](docs/V1_4_SPEC.md), research notes in [docs/research/](docs/research/).

## Install and play
**You need:** BeamNG.drive (tested with 0.40.x) on Steam, free disk space for the map download cache (it grows
as you visit places, up to 20 GB by default; settings page > "Download cache size" sets 5-50 GB), and an internet
connection (the map is downloaded while you play).

1. **Download** the file for your system from the [Releases](https://github.com/TecnaGamer/BeamNG.Earth/releases)
   page:
   - **Windows:** `BeamNG.Earth-<version>.exe`. Windows may warn about an unknown app: **More info > Run anyway**.
   - **Linux:** `BeamNG.Earth-<version>-x86_64.AppImage`. Make it executable (right-click > Properties >
     "Allow executing", or `chmod +x`).
2. **Run it** (with Steam running). A console window opens (like BeamMP's launcher); it copies the mod into the
   game, starts the world server and starts BeamNG.drive. Closing the game stops the server.
   - **Linux, native game:** started with the grass patch (`tools/gcpatch`) already applied, no Steam launch
     options needed.
   - **Linux, Windows build under Proton:** the AppImage sees the Proton setting and starts that build through
     Steam (the server runs natively). The `.exe` also works inside the game's Proton prefix (for example with
     protontricks' launcher).
   - **Linux, Steam as a Flatpak (or snap):** the mod goes into the game's folder inside the Flatpak
     (`~/.var/app/com.valvesoftware.Steam/...`) and the game starts through Steam (without the grass patch).
   - The Windows build shows BeamNG's launch options dialog (renderer, safe mode) as usual; `--gfx dx11` /
     `--gfx vk` / `--gfx dx12` skip it with that renderer.
3. **Play:** in the game, go to **Freeroam** and pick the **BeamNG.Earth** level. A welcome page explains the
   basics the first time. Press **M** for the world map: search a place or an address, right-click to teleport,
   left-click to set a GPS route, and **Autodrive** to let the car drive it.

**From the source instead** (any system with [Python 3.10+](https://www.python.org/downloads/)): download this
project (the green **Code** button > **Download ZIP**) and unzip it. Install once (and after each update):
double-click `run_server.bat` (Windows) or run `python3 install.py` (Linux). Then start the server with
`run_server.bat` / `./run_server.sh`, keep its window open, and start the game yourself. Or run
`python3 launcher.py`, which also starts the game like the downloads do.

**Updating:** the mod tells you in game when a newer version is on GitHub. Download the new file and run it (from
the source: unzip it over the old folder and run the installer again).

**Uninstalling:** delete the `beamng_earth` folder in `<BeamNG user folder>/current/mods/unpacked/` and the
downloaded file (or this folder); the map cache is `.cache/beamng-earth` in your home folder (`~/.cache/beamng-earth` on Linux,
`C:\Users\<you>\.cache\beamng-earth` on Windows).

**The map cache on another drive:** in the game, settings page > Download cache > **Cache folder**: **Change...** opens
your system's folder window, or type or paste a folder and press **Use**. Or start the app once with `--cache-dir PATH`
(e.g. a shortcut to `BeamNG.Earth-<version>.exe --cache-dir "D:\BeamNG.Earth cache"`). It is used from the next start
and remembered; `default` goes back. Move the old cache folder there first to keep what is already downloaded.

**BeamNG.drive installed somewhere else** (not in a Steam library the app finds, e.g. another launcher): the app asks
where it is the first time (Enter opens your system's folder window, or type or paste the folder) and remembers it;
`--game-dir PATH` sets it too, `--game-dir default` forgets it. A game folder outside Steam is started directly. On
Linux, BeamMP's launcher finds the game only in Steam's libraries, so BeamMP isn't started with such a copy.

More detail (traffic, the status bar, reporting problems, troubleshooting): [docs/QUICKSTART.md](docs/QUICKSTART.md).

**Compute sharing (optional, for tinkerers):** another computer on your network can build part of the distant
landscape for you. On it, from a copy of the same version's source: `python3 server/peerwork.py serve` (port 47620,
`--workers N` processes; it downloads its own map data). On the computer you play on, set `BNGE_PEERS` to the
helpers before starting BeamNG.Earth, for example `BNGE_PEERS=192.168.1.20:47620,192.168.1.21:47620`. Helpers
running other code are left out, and anything a helper fails on is built on your own computer. Only use helpers on a
network you trust: the connection is not encrypted.

### Developer setup
`python3 install.py` also works from a checkout (mod copy, `.venv`, then `python3 start.py`); by hand:
1. Python 3.10+ with the packages in `server/requirements.txt`. The server needs its Rust module: the checkout
   carries it built for Linux x86-64 (`server/earthfast.abi3.so`) and Windows (`server/earthfast.pyd`); after
   changing `native/earthfast`, or on another system, build it with `python3 tools/build_native.py` (Rust's cargo;
   `--windows` cross-builds the `.pyd` with cargo-zigbuild).
2. Copy (or sync) `mod/beamng_earth/` to
   `<BeamNG user folder>/current/mods/unpacked/beamng_earth/`.
3. Start the server: `python3 server/earthd.py`
4. Start BeamNG.drive and load the **BeamNG.Earth** level. The mod connects to the server and
   streams the world; the status bar at the top shows where you are and the loading, **M** opens the
   world map (search, teleport, GPS routes, Autodrive).

Player downloads: `python3 tools/build_app.py` builds this system's AppImage / .exe (PyInstaller; see its
docstring; for the .exe run `tools/build_bootloader.py` first: a bootloader built from source, fewer antivirus
false positives); GitHub Actions (`.github/workflows/release.yml`) builds both and publishes the release `v<VERSION>`
whenever `main` gets a `VERSION` without one. Release zip of the source: `python3 tools/package.py`.

Tests: `cd server && python3 -m unittest discover -s tests -t tests` and
`cd mod/tests && for f in test_*.lua; do luajit $f; done`.

## Multiplayer (BeamMP)

BeamNG.Earth runs on [BeamMP](https://beammp.com/) servers (in testing): drive the real world together.

**Joining a server**
1. Start the BeamNG.Earth app (the same version as the server's). It starts BeamMP with the game: the official BeamMP
   launcher, unchanged (BeamMP's code is AGPL-3.0, [github.com/BeamMP](https://github.com/BeamMP)). If BeamMP isn't
   installed, the app asks once: **Yes / No / Don't ask again / Have it already** (the last one asks where your
   launcher is: Enter opens your system's file window, or type or paste the path; `--beammp-launcher PATH` sets it
   too, and it is kept for the next starts). To change it later: settings page > Multiplayer > **BeamMP launcher**.
   - **Windows:** Yes downloads BeamMP's official launcher from BeamMP's GitHub releases (checked against the checksum
     BeamMP publishes; Windows Firewall then asks once whether it may use the network: allow it).
   - **Linux and Steam Deck:** BeamMP publishes no Linux download, so Yes builds its launcher on your computer from
     BeamMP's source, the newest version that passed BeamMP's own tests, in an Ubuntu container (podman or docker:
     SteamOS has podman; about a minute on a Steam Deck). A launcher you built yourself is used when found
     (a `BeamMP-Launcher` folder in your home folder or one folder down, e.g. `~/Games/BeamMP-Launcher`, or
     `~/.local/share`; or answer "Have it already", or set `BEAMMP_LAUNCHER`).
   - The setting **Start BeamMP with BeamNG.Earth** (Pause menu > Mods > BeamNG.Earth, Multiplayer) turns this off or
     back on; `--no-beammp` starts the game without BeamMP once.
2. In the game: main menu > More > BeamMP, log in (an account or a guest).
3. Pick the **BeamNG.Earth** category in BeamMP's server browser: the servers running BeamNG.Earth (search,
   filters, favorites and details as for the other servers), the official server at the top. They are listed only there, for players who have BeamNG.Earth. A private server: Direct Connect with its address (the
   [Discord server](https://discord.gg/tgf) has some). Every player needs the BeamNG.Earth app running: each builds the
   world on their own computer.
   (Starting the BeamMP launcher yourself first still works: the app then starts just the world server.)

Leaving a server turns BeamNG.Earth back on (joining one switches your other mods off).

Returning players resume their server-saved position, heading and vehicle. Saves from the modified 1.6.2 version
remain compatible; keep both checkpoint files when updating (see [server save instructions](beammp/README.md#saved-player-positions)).
Players without a checkpoint stay at their starting location unless the server enables joining near others.
Cars show in the right place in everyone's world; other players show on the
world map (**M**) as green arrows; players far away (another country) are hidden until you meet. Crash damage: a crash
you watch keeps the damage your own game worked out; a car that crashed out of your sight takes on its owner's damage
(broken parts and dents) once it comes within about 0.6 miles.

**Sharing the work** (turned off for now: each player builds their own world)**:** players near each other share one
world origin and split the building of the distant landscape (each piece is built once and sent to the others; faster computers take more), while a player far from
everyone builds their own. For that the other players must reach your computer on a TCP port, 47621 unless the server
picks another (the server tells your app which): on the same network it just works; over the internet, forward that
port in your router, or you simply build everything yourself. A port of your own: set the environment variable
`BNGE_SHARE_PORT` before starting the app.

**Hosting a server:** download `BeamNG.Earth-BeamMP-Server-<version>.zip` from the
[Releases](https://github.com/TecnaGamer/BeamNG.Earth/releases) page and follow the guide inside
(`BeamNG.Earth-README.md`). In short: unzip it into a BeamMP server's folder, set
`Map = "/levels/beamng_earth/info.json"` in `ServerConfig.toml`, forward the server's port (TCP and UDP) for players
over the internet, and start it (its log shows `[BeamNG.Earth] plugin loaded`). Which list it is on:
`Private = false` with your AuthKey puts it on the BeamNG.Earth list (the in-game category) and the plugin keeps it off
BeamMP's own public list, so players without BeamNG.Earth never see it; `Private = true`, or no AuthKey: on no list,
players use Direct Connect. The plugin lists the server once a minute with `curl` (Linux, and Windows 10 and later).
Update the server zip with every release: it carries the mod the server sends to players. The server's time and weather for everyone
(`G.TIME`: real or a fixed / running time such as `'20:30'`; `G.WEATHER`: real or clear, cloudy, rain, storm, snow,
fog) are set near the top of `Resources/Server/BeamNGEarth/main.lua`. The players' sharing port (47621) is
`G.SHARE_PORT` near the top of `Resources/Server/BeamNGEarth/main.lua`. The server needs no world data and
little CPU. From the source, `python3 tools/package.py --beammp` builds the same zip.

## Data sources and attribution
The repository contains only code; all map data is downloaded at runtime and cached locally.
- Imagery: state and national orthophoto services where they are open (Maryland, a dozen more US
  states, Switzerland, France, Spain, the Netherlands, Germany's states, Poland (GUGiK), Japan, parts of Australia and
  Canada, and more), USDA NAIP and USGS The National Map (public domain) in the US, and Sentinel-2
  cloudless by EOX IT Services GmbH everywhere (https://s2maps.eu, contains modified Copernicus Sentinel
  data; 2016 release CC BY 4.0, 2023 release CC BY-NC-SA 4.0, non-commercial).
- Elevation: Mapterhorn (see https://mapterhorn.com/attribution), AWS Terrain Tiles / Mapzen; the polar caps from
  85 deg on: Copernicus DEM GLO-30 (produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus
  Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA).
- Roads, buildings, water and places: Overture Maps / OpenStreetMap contributors (lane counts, widths and speed limits from OpenStreetMap via OSM US Layercake); trees from a
  canopy height model; airports from OurAirports.
Each source's terms apply to the downloaded data; the in-game window shows the attribution line.
The full list of data sources, services, libraries, tools and references, with licences and the
credit each one asks for, is in [CREDITS.md](CREDITS.md).

## License
Code: MIT, see [LICENSE](LICENSE). BeamNG.drive is a trademark of BeamNG GmbH; this is an unofficial
fan project and contains no BeamNG assets.
