# BeamNG.Earth on a BeamMP server

This zip turns a BeamMP server into a BeamNG.Earth server: the real world, streamed around every player. Players join
with the normal BeamMP launcher.

> ## Help: the TGForever Discord, not BeamMP
> **Join the TGForever Discord, [discord.gg/tgf](https://discord.gg/tgf), to get help setting this up, ask
> questions, report bugs, chat with other players or help develop the mod.**
>
> BeamNG.Earth isn't like a normal BeamNG mod: it needs its own app running next to the game on every player's
> computer. If something isn't working, it's almost certainly a BeamNG.Earth problem, not a BeamMP one, so please
> **don't contact the BeamMP team, ask in BeamMP's channels or open BeamMP support tickets** when you run into trouble
> getting BeamNG.Earth to work with BeamMP.
>
> **Your server is hidden from players who don't run BeamNG.Earth.** BeamMP can't install BeamNG.Earth for players who
> join: it needs its own app (a launcher and a world server that builds the world on each player's computer). So
> BeamMP's own server list never shows a BeamNG.Earth server. Players with the BeamNG.Earth app find it in the
> BeamNG.Earth tab of BeamMP's server browser, or join by Direct Connect.

## Quick server setup

1. **Get the BeamMP server** (the latest release from https://github.com/BeamMP/BeamMP-Server/releases; Windows:
   `BeamMP-Server.exe`, Linux: the build for your distribution). Put it in its own folder and run it once: it writes
   `ServerConfig.toml` and a `Resources` folder, then stops or waits. Close it.
2. **Unzip this file into that folder.** Its `Resources` folder merges with the server's:
   - `Resources/Client/beamng_earth.zip`: the BeamNG.Earth level and mod (BeamMP sends it to every player who joins);
   - `Resources/Server/BeamNGEarth/main.lua`: the server plugin (it tells the players where everyone is).
3. **Edit `ServerConfig.toml`:**

   ```toml
   Map = "/levels/beamng_earth/info.json"
   Port = 30814          # any port you like; players connect to it
   Name = "My BeamNG.Earth server"
   MaxCars = 3
   Private = false       # false (with your AuthKey): on the BeamNG.Earth server list; true: direct connect only
   AuthKey = "..."       # your own key from https://keymaster.beammp.com (the server won't start with it empty)
   ```

   Never share your AuthKey (the plugin only checks that one is set; it never sends it anywhere).

   **Which list your server is on:**
   - `Private = false` with your AuthKey: on the **BeamNG.Earth** server list, the "BeamNG.Earth" tab in BeamMP's
     server browser, which only players with BeamNG.Earth see. The plugin keeps it off BeamMP's own public list while
     the server runs (it switches BeamMP's Private setting on; your file isn't changed), so players without
     BeamNG.Earth never see it.
   - `Private = true`, or no AuthKey: on no list. Players join with Direct Connect and your address.
   - To keep a public server off the BeamNG.Earth list too, set `G.LISTED = false` near the top of
     `Resources/Server/BeamNGEarth/main.lua`.

   The server lists itself once a minute with `curl` (built into Linux and Windows 10 and later).

   **Time and weather** (near the top of `Resources/Server/BeamNGEarth/main.lua`, then restart the server):
   - `G.TIME = 'real'` (each player's real local time where they are) or a fixed local time such as `'20:30'`, with
     `G.TIME_SPEED` 0 (it stays), 1 (it runs at real speed) or more (60: an hour of the day every real minute). Every
     player sees the same time.
   - `G.WEATHER = 'real'` (the live weather where each player is) or `'clear'`, `'cloudy'`, `'rain'`, `'storm'`,
     `'snow'`, `'fog'` for everyone.
   - A player who set Sky or Weather to Manual in BeamNG.Earth's settings keeps their own.
4. **Open the port** if players come over the internet: forward the server's port, **TCP and UDP**, in your router to
   the server's computer (and allow it in its firewall). Players on the same network need nothing.
5. **Start the server.** Its log should show `[BeamNG.Earth] plugin loaded`.
6. **Players connect** from BeamMP's server browser (main menu, More, BeamMP): the **BeamNG.Earth** tab for listed
   servers, or Direct Connect with your address and port.

To update, unzip the new version's server zip over the old one and restart the server. The server needs no world
data and little CPU: the world is built on each player's computer.

## Docker

Each release is also a container image: `ghcr.io/tecnagamer/beamng.earth-server:<version>` (`latest`: the newest
beta; `next`: the newest preview; `dev`: the newest development build), the Linux host bundle (BeamMP's own
unmodified server program and this zip) with `curl` for the server list. The server and its players run the same
BeamNG.Earth version, so pin the version's tag and change it with each release. `docker/compose.yaml` in the source is
a starting point:

```sh
docker run -d --name beamng-earth -p 30814:30814/tcp -p 30814:30814/udp -v beamng-earth:/data \
  -e BEAMMP_AUTH_KEY=... -e BEAMMP_NAME="My BeamNG.Earth server" -e BEAMMP_PRIVATE=false \
  ghcr.io/tecnagamer/beamng.earth-server:<version>
```

- **`/data`** keeps the server's `ServerConfig.toml` (the host bundle's on first start: `Private = true`, no AuthKey)
  and what the plugin keeps between sessions: the shared road plans (`store`), baked packs of whole areas (put `.bnge`
  files in `bake`) and the share helper's `spool`. The server runs as uid 1000: a folder of your own mounted there
  must be writable by it (`chown -R 1000:1000`).
- **BeamMP's own `BEAMMP_*` settings** win over the file and are written into it at each start: `BEAMMP_AUTH_KEY`
  (required), `BEAMMP_NAME`, `BEAMMP_DESCRIPTION`, `BEAMMP_TAGS`, `BEAMMP_PORT`, `BEAMMP_PRIVATE`,
  `BEAMMP_MAX_PLAYERS`, `BEAMMP_MAX_CARS`, `BEAMMP_MAP`, `BEAMMP_DEBUG` (see BeamMP's documentation). What none sets:
  edit `/data/ServerConfig.toml` and restart. A port other than 30814: set `BEAMMP_PORT` and publish that same port
  (`-p 30899:30899/tcp -p 30899:30899/udp`), as the server tells the list its own.
- **Time, weather and listing** for everyone (the settings at the top of `main.lua`): `BNGE_TIME` (`real` or
  `HH:MM`), `BNGE_TIME_SPEED` (0, 1, 60, ...), `BNGE_WEATHER` (`real`, `clear`, `cloudy`, `rain`, `storm`, `snow`,
  `fog`), `BNGE_LISTED` (`false` keeps a public server off the BeamNG.Earth list too). A value it doesn't know stops
  the start with a message saying which.

Build it yourself from the source's root: `docker build -f docker/Dockerfile -t beamng-earth-server .`

## Players

- **Every player needs the BeamNG.Earth app** (https://github.com/TecnaGamer/BeamNG.Earth/releases) running, the same
  version as the server's. Start the BeamNG.Earth app and the BeamMP launcher in either order (the app starts the game,
  or just its world server if the game is already running), then join. Without the app, a window asks to install it:
  the world can't load without it.
- A player who joins is sent to where the others are, and each player's car shows in the right place in everyone's
  world, with its dents. Other players show on the world map (M) as arrows.
- Each player's app builds the world around them on their own computer, and this server shares the slowest part of
  it (in testing): the road levelling plans of each map area that a player builds come to the server, which keeps
  them in `Resources/Server/BeamNGEarth/store` and sends them to every player who comes there later, this session or
  the next. Nothing to set up or forward: it goes over the BeamMP connection. Near the top of
  `Resources/Server/BeamNGEarth/main.lua`: `G.SHARE_STORE_MB` (the store's size, 2048 MB; the pieces used longest ago
  go first) and `G.SHARE_OUT_KBPS` (how fast the server sends them, 1024 KB/s for all players together).
- Baked packs: `.bnge` files of whole areas put in `Resources/Server/BeamNGEarth/bake/` are served the same way to
  players with the matching BeamNG.Earth version.
- Share helper (in testing): the server's own machine can build the areas round the players before they get there.
  Run BeamNG.Earth's world server next to the BeamMP server, from BeamNG.Earth's source (the same version as the
  players):
  `python3 server/sharehelper.py --share-dir <this server's folder>/Resources/Server/BeamNGEarth --cache <a folder
  for its map data> --procs 4` (`--procs`: how many of the machine's cores it uses). It needs internet access and
  a few GB of disk for each area.
<!-- v1 shared world building: commented out 2026-10-27 (off since 2026-10-23; not coming back for a while).
- (Turned off for now: each player builds their own world, and the setting below does nothing yet.) Players near
  each other share one world origin and split the work of building the world: each piece of the distant
  landscape is built once and sent to the others. For that, the other players must reach each player's computer on
  **TCP port 47621** (on one network it just works; over the internet, each player forwards it, or else simply builds
  everything themselves). To use another port on your server, edit the line `G.SHARE_PORT = 47621` near the top of
  `Resources/Server/BeamNGEarth/main.lua` and restart the server: every player who joins gets it. A player can still
  pick their own with the environment variable `BNGE_SHARE_PORT`.
-->
- Players far apart (another country) each build their own world and don't see each other's cars until they meet.
