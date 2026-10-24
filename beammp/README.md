# BeamNG.Earth on a BeamMP server

This zip turns a BeamMP server into a BeamNG.Earth server: the real world, streamed around every player. Players join
with the normal BeamMP launcher.

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
4. **Open the port** if players come over the internet: forward the server's port, **TCP and UDP**, in your router to
   the server's computer (and allow it in its firewall). Players on the same network need nothing.
5. **Start the server.** Its log should show `[BeamNG.Earth] plugin loaded`.
6. **Players connect** from BeamMP's server browser (main menu, More, BeamMP): the **BeamNG.Earth** tab for listed
   servers, or Direct Connect with your address and port.

To update, unzip the new version's server zip over the old one and restart the server. The server needs no world
data and little CPU: the world is built on each player's computer.

## Players

- **Every player needs the BeamNG.Earth app** (https://github.com/TecnaGamer/BeamNG.Earth/releases) running, the same
  version as the server's. Start the BeamNG.Earth app and the BeamMP launcher in either order (the app starts the game,
  or just its world server if the game is already running), then join. Without the app, a window asks to install it:
  the world can't load without it.
- A player who joins is sent to where the others are, and each player's car shows in the right place in everyone's
  world, with its dents. Other players show on the world map (M) as arrows.
- (Turned off for now: each player builds their own world, and the setting below does nothing yet.) Players near
  each other share one world origin and split the work of building the world: each piece of the distant
  landscape is built once and sent to the others. For that, the other players must reach each player's computer on
  **TCP port 47621** (on one network it just works; over the internet, each player forwards it, or else simply builds
  everything themselves). To use another port on your server, edit the line `G.SHARE_PORT = 47621` near the top of
  `Resources/Server/BeamNGEarth/main.lua` and restart the server: every player who joins gets it. A player can still
  pick their own with the environment variable `BNGE_SHARE_PORT`.
- Players far apart (another country) each build their own world and don't see each other's cars until they meet.
