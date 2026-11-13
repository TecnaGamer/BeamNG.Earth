# BeamNG.Earth: quick start

Drive anywhere on the real Earth in BeamNG.drive. This page gets you from the download to driving
in a few minutes. No programming needed.

## What you need
- **BeamNG.drive** (tested with 0.39.x), started at least once.
- **Python 3.10 or newer** from <https://www.python.org/downloads/>. On Windows, tick
  **"Add python.exe to PATH"** on the first page of the Python installer. **64-bit Python 3.12**
  is recommended for source installs on Windows, matching the packaged release builds. Newer
  Python versions may not yet have compatible prebuilt packages for all dependencies.
- **About 2 GB of free disk space** for the map cache (it grows as you explore and is capped).
- **An internet connection**: the map (height data, aerial photos, roads, buildings) is downloaded
  while you play.

## Install (3 steps)
1. **Unzip** `BeamNG.Earth-<version>.zip` anywhere you like (for example your Documents folder).
2. **Run the installer.** Open the unzipped folder and:
   - Windows: double-click `run_server.bat`. The first time, it runs the installer for you. (Or
     open a terminal in the folder and type `py install.py`.)
   - Linux: open a terminal in the folder and run `python3 install.py`.

   The installer finds your BeamNG.drive user folder, copies the mod into it, and downloads the
   server's Python packages (a few minutes). If it finds more than one BeamNG folder it asks which
   one your game uses. Running it again later is safe: it only updates what changed.
3. **Play.**
   - Start the world server: `run_server.bat` (Windows) or `./run_server.sh` (Linux). Keep its
     window open while you play; close it when you are done.
   - Start BeamNG.drive, go to **Freeroam** and pick the **BeamNG.Earth** level. The first time, a short
     welcome page explains the basics (it is in the settings page too: Pause menu > Mods > BeamNG.Earth).

## Going places
- Press **M** (the game's map key) or open **Map** in the pause menu for the **world map**:
  satellite imagery, your car's position and heading, and your saved places. Drag to pan, scroll
  to zoom. The search box looks up landmarks and places as you type, in any language (Wikipedia,
  Wikidata, and Photon by komoot where it answers: what you type is sent to them, and your position
  to Photon). Enter searches streets and house numbers too (OpenStreetMap's Nominatim: what you type
  and an area around your position), then Enter again takes the first line. It also answers
  "highest bridge in Switzerland", and takes your saved places and a typed "latitude, longitude". M or Esc closes it.
- **Left-click** the map (or a saved place, or a search result) to plan a GPS route there: the whole
  road route is planned online (OSRM on OpenStreetMap; your start, destination and any waypoints are sent to it)
  and drawn on the map with the distance and driving time left; the game's own route arrows follow
  it on the road. Leave the route and it is planned again. **Clear** stops it, **Teleport** jumps there,
  **Autodrive** lets the car drive the route by itself (in its lane, at the speed limit, minding traffic;
  press it again to drive yourself).
- **Right-click** the map for **Teleport here**, **Set GPS destination** or **Save this place**.
- The map's **Places** panel (on the right; "Places ›" hides it) lets you save where you are, type
  (or paste from Google Maps) a "latitude, longitude" and press **Go**, go to, rename or delete your
  saved places, and jump back to recent ones. Click a place's name to see it on the map.
- The first visit to a new area takes a little while: the map is downloaded and built while you
  watch. Going back later is much faster (it is cached).

## The status bar
A slim bar at the top of the screen shows the street you are on, the town, state and country, the loading
progress after a teleport. The street comes from the map data on your computer; the town,
state and country come from Overture Maps' boundaries and places data, downloaded once per region (a few MB) and kept
with the map data: your position is not sent to any lookup service. Turn either off in the settings page
(**Status bar**, **Place names**).

## Traffic
Traffic and parked cars follow your game settings, like on any other map: pick them in the
level's start options (Disabled / Parked only / Normal / Police), or turn them on or off while
driving with the game's **radial menu** (the traffic entry). The amounts are the game's own traffic
settings (Options > Gameplay > Traffic).

## Something wrong? Tell us
In the game, press **Ctrl+Shift+E** where it happens: the mod takes a screenshot and notes the place,
the frame rate and your hardware and graphics settings. Pick the kind (**Map issue**, **Performance
issue**, **Bug**, **Idea / suggestion**), type a short title and what you saw, and Save. For stutters
or low fps, press **Ctrl+Shift+Y** instead while it happens: the **performance profiler** records 30 s
of frame times, then opens the same window.

**Send to the developers** sends the report privately to the BeamNG.Earth developers (not published): its window shows
exactly what goes, lets you leave out the screenshot, the exact location and the logs, and gives you a report id to
quote on Discord. Nothing is sent before you click Send there ([TELEMETRY.md](../TELEMETRY.md#reports-you-send)).
Each report is a folder (the window shows where) with `issue.md`, a ready-to-post GitHub issue you
can edit first. The **Open GitHub issue page** button in the game opens the new-issue page on
<https://github.com/TecnaGamer/BeamNG.Earth/issues> with your text; **Open folder** shows the report folder, so you
can drag `screenshot.jpg` into the issue. If the page opens without the text, copy issue.md's text into it.
(From a source copy, `python3 tools/reports.py issue` does the same with an edit step first; `python3
tools/reports.py` lists the reports.)

**The log file.** For freezes, stutters or anything slow, the world server's log is the most useful thing to send
(Discord or the GitHub issue): `%LOCALAPPDATA%\beamng-earth\logs\server.log` on Windows,
`~/.cache/beamng-earth/logs/server.log` on Linux and the Steam Deck. Send it right after it happens: it starts over each
time the BeamNG.Earth app starts. Besides how long each piece of ground took to build, it has your computer and
graphics settings (`game: machine:` lines) and the game's own performance lines:
- `game: perf hitch`: a frame of 0.1 s or more, with where you were (latitude, longitude), your speed, which parts of
  the mod and of the game took the time, and what the mod was loading;
- `game: perf drop` / `perf back`: the frame rate below about two thirds of your usual for 5 s or more, and when it
  came back;
- `game: perf minute`: every minute, the average frame rate, the 1 % low and the worst frame with its cause.

## Troubleshooting
- **DuckDB fails to install on Windows**: the installer uses a prebuilt DuckDB package instead of
  compiling it. If pip reports **"No matching distribution found"** for DuckDB, install **64-bit
  Python 3.12**, close the world server, and rename this project's `.venv` folder to an unused
  backup name (for example `.venv-backup`). Then open a terminal in the project folder and run
  `py -3.12 install.py`. Installing another Python and rerunning the installer alone does not
  change the interpreter in an existing `.venv`. Alternatively, use the standalone Windows
  `.exe` from the [Releases page](https://github.com/TecnaGamer/BeamNG.Earth/releases), which
  includes Python and the server's packages.
- **"Looking for the world server" / "server not connected" banner** in the game: the server is not
  running. Start `run_server.bat` / `./run_server.sh` and wait a few seconds; the game reconnects
  by itself. If the server window shows an error, run the installer again.
- **Windows Firewall asks about Python**: the server only talks to the game on your own computer
  (local port 47613). You can allow it for private networks only, or cancel; the game still connects.
- **The first load is slow, or the ground looks blurry at first**: it is downloading. This gets
  faster as the cache fills up.
- **The level is not in the list**: the mod is not in the folder the game uses. Run the installer
  again and pick the right folder (in the game, Options > Other shows the user folder), or run
  `python install.py --user-dir "<that folder>"`.
- **After an update**: unzip the new version and start its `run_server.bat` / `./run_server.sh`: it copies the new
  mod into the game and updates the server's packages by itself (`--keep-mod` leaves the game's copy alone).

## Credits
The map comes from free and open data sources (USGS, Sentinel-2 by EOX, Mapterhorn, OpenStreetMap
and Overture contributors, and more). The full list and their licences are in
[CREDITS.md](../CREDITS.md); the game also shows the credits on screen.
BeamNG.Earth is an unofficial fan project, not made or endorsed by BeamNG GmbH.
