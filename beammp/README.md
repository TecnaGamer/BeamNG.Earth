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

To update, stop the server, unzip the new version's server zip over the old one, then start it again.
Keep the existing player saves (see below). The server needs no world
data and little CPU: the world is built on each player's computer.

## Players

- **Every player needs the BeamNG.Earth app** (https://github.com/TecnaGamer/BeamNG.Earth/releases) running, the same
  version as the server's. Start the BeamNG.Earth app and the BeamMP launcher in either order (the app starts the game,
  or just its world server if the game is already running), then join. Without the app, a window asks to install it:
  the world can't load without it.
- Returning players automatically spawn in their last saved vehicle at their saved position and heading on this
  server, including guest players using the same game profile and server address. Players without a checkpoint stay
  at their normal starting location; they are not automatically teleported to other players. Each player's car shows
  in the right place in everyone's world, with its dents. Other players show on the world map (M) as arrows.
- (Turned off for now: each player builds their own world, and the setting below does nothing yet.) Players near
  each other share one world origin and split the work of building the world: each piece of the distant
  landscape is built once and sent to the others. For that, the other players must reach each player's computer on
  **TCP port 47621** (on one network it just works; over the internet, each player forwards it, or else simply builds
  everything themselves). To use another port on your server, edit the line `G.SHARE_PORT = 47621` near the top of
  `Resources/Server/BeamNGEarth/main.lua` and restart the server: every player who joins gets it. A player can still
  pick their own with the environment variable `BNGE_SHARE_PORT`.
- Players far apart (another country) each build their own world and don't see each other's cars until they meet.

## Saved player positions

The server records stable driving positions from the clients' two-second updates, writes them every **10 seconds**,
and flushes the latest received checkpoints on disconnect and server shutdown. Each checkpoint includes position,
road/deck height, heading, and the active owned vehicle's model, parts, tuning, paints, and license plate. Vehicle
configurations come from [BeamMP's server vehicle data](https://docs.beammp.com/scripting/server/latest-server-reference/#mpgetplayervehiclesplayer_id-number-table).
Damage, fuel levels, and attached trailers are not restored.

When a player reconnects, including after a server restart, the client loads the checkpoint's terrain and road
collision, spawns the saved vehicle through BeamMP's normal vehicle spawning path, and enters it facing the saved
heading. It waits for server ownership before completing the restore. A position-only checkpoint from an older
version, or an unavailable vehicle mod, uses the game's default vehicle. Normal vehicle limits still apply; if an
automatic spawn is rejected, manually spawning an owned vehicle completes the position restore. Temporary loading
positions, free cameras, walking avatars, and other players' vehicles do not replace a driving checkpoint.

Saves use the authenticated account's permanent `beammp` identifier, independent of display names or temporary
player IDs ([BeamMP API](https://docs.beammp.com/scripting/server/latest-server-reference/#mpgetplayeridentifiersplayer_id-number-table)).
Guest sessions use a random server-issued token, retained in the guest's game profile. Their positions and vehicles
are stored in the same server checkpoint files under separate `guest:` keys. Guest names, temporary player IDs, and
IP addresses are not used as player identity, so guests on the same connection do not share saves. Authenticated
account saves always take precedence when logged in. Each server keeps its own saves, separate from singleplayer
last-position saving.

The client keeps only its per-server guest tokens in `settings/beamngEarthMPGuests1.json` and
`settings/beamngEarthMPGuests2.json` in the BeamNG user folder. Keep these files to resume as a guest; deleting them,
using another game profile, or joining through a different server address creates a new guest identity. These tokens
identify guest saves, so do not share them. The modified 1.6.2 guest tokens remain compatible with this version.

Edit `G.SAVE_POSITIONS` and `G.SAVE_SECONDS` near the top of `Resources/Server/BeamNGEarth/main.lua`, then restart,
to enable/disable this feature or change its save interval. The plugin automatically creates a `data` folder in the
server directory, beside `ServerConfig.toml`, and reads/writes `data/bnge_player_positions1.json` and
`data/bnge_player_positions2.json`. Keep both when migrating the server. Alternating files preserve the previous
complete save if a write is interrupted. They are outside `Resources/Server` to avoid plugin reloads when saving.

Existing saves beside `ServerConfig.toml` migrate automatically into `data` if that folder has no valid checkpoint
file. The original root files remain as backups; after migration, valid files in `data` take precedence.

`G.SAVE_GUESTS = true` enables guest checkpoints. `G.JOIN_NEAR_PLAYERS = false` disables the old automatic teleport
to the longest-connected player. Setting it to `true` restores that behavior for players without a checkpoint, but
saved-location restores take priority. The console logs `checkpoint active (guest, <vehicle>)` after the first accepted
guest driving pose and `resuming at ...` when that checkpoint is selected on reconnect.

Update **both** the server plugin and `Resources/Client/beamng_earth.zip`, then restart the server and have players
fully close BeamNG and reopen it before reconnecting. Guest identity and vehicle spawning require the updated client.
The server ZIP includes a startup script that disables the older local mod copy and reloads cached BeamNG.Earth Lua
modules before the level loads. This matters because the local app loads its mod at the main menu, and simply mounting
a newer ZIP does not replace already loaded Lua modules. Use the matching custom 1.6.3 app so its world server also supplies the migrated highway signs.

Check the server console for `client runtime 4; guest-save support ready`, followed by
`checkpoint active (guest, <vehicle>)` after driving. If a guest cannot save, the console now distinguishes an outdated
client runtime, an unconfirmed guest identity, loading/free camera, and a vehicle configuration awaiting server acceptance.
Keep the existing `data` files. A guest who never produced a checkpoint must drive after updating before testing a
rejoin. BeamMP saves are controlled by the server settings above.

## Updating from the modified 1.6.2 version

Stop the server before replacing its plugin and client ZIP. Keep `ServerConfig.toml` and both
`data/bnge_player_positions*.json` files beside it; the release ZIP contains no player saves. If moving to a new
server folder, copy both save files into its `data` folder. If your older saves are still in the server root, keep
both there for automatic import, or copy both into `data`. Files already in `data` take precedence.

The version-1 JSON format, account IDs, guest tokens, vehicle configurations, and save sequence numbers are unchanged.
No coordinate conversion of existing files is needed: checkpoints use latitude, longitude and geographic height,
and the client converts them into 1.6.3's snapped world origin when restoring. Keep the same server address and
BeamNG user profile for guest restores. Reapply any custom plugin settings, including the save interval, after updating.
