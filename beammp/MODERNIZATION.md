# Multiplayer reliability and integration status

Audit baseline: `8b0e9b04` (1.7.3-beta), 2026-11-02.

## Implemented; local tests verified, game validation pending

The plugin's coordinate validation previously accepted NaN, which could poison
the anchors sent to every player. Positive latitude/longitude bounds now reject
non-finite values before changing the last good pose. Clients also validate
anchors and join destinations, including when connected to an older plugin.

Both server meeting-distance and client damage-proximity checks now wrap
longitude differences. At the equator, players at 179.995 and -179.995 degrees
are about 1,112 m apart; the old calculation returned 40,029,062 m. Nearby joiners
there no longer request an unnecessary teleport. The 12 m join offset also
wraps longitude back into the valid range.

`bngeAnchors` is a complete live-player snapshot. The client previously retained
missing players' origins until the entire session ended. It now expires absent
or invalid peers and their transform, position, distance and settling history.
A reused player ID starts with fresh conversion state. This does not change
BeamMP vehicle authority, introduce shared generation, or change protocol 2.

Regression tests first failed against the baseline, then passed after the fixes:

```sh
python -m pip install lupa
python tools/run_lua_tests.py test_beammp_plugin test_mpanchor test_mpsession test_beammp
python tools/run_lua_tests.py
```

The runner isolates each test in a subprocess with a 120-second default timeout.
It uses Lupa's Lua 5.3 for the server plugin and LuaJIT 2.1 for client modules.
These are offline unit/stub tests, not BeamNG/BeamMP runtime tests. Lupa is a
development dependency, not a game or packaged server dependency.

## Current boundaries

Every player runs the Python world companion plus the BeamNG Lua mod. BeamMP
synchronizes vehicles; this plugin relays geographic anchors, locations and
server settings. Client `mpanchor.lua` converts positions between floating
origins before BeamMP's handler. Shared world generation remains disabled.
Server discovery uses the existing Earth listing service or Direct Connect.
There is no party service, embedded voice client, or Steam authentication in
these changes.

The [BeamMP server API](https://docs.beammp.com/scripting/server/latest-server-reference/)
documents server event registration, client events and identifiers including
BeamMP account ID, IP and linked Discord. It does not document a Steam identity
field. A claimed Steam ID must not become a trusted party identity.

[Steam API initialization](https://partner.steamgames.com/doc/api/steam_api#SteamAPI_Init)
requires a running Steam client and appropriate AppID setup and ownership.
[Rich-presence invitations](https://partner.steamgames.com/doc/api/ISteamFriends#InviteUserToGame)
deliver a connection string through launch arguments or a callback. No supported
BeamNG mod bridge or authorization to integrate against its AppID was verified.
[Steam OpenID](https://partner.steamgames.com/doc/features/auth#website)
can link a third-party account through browser authentication; it does not by
itself implement friends, invitations or BeamMP session joining. The next party
implementation should use authenticated BeamMP membership and explicit invite
codes, with Steam linking optional and separate.

[Mumble Link](https://www.mumble.info/documentation/developer/positional-audio/link-plugin/)
passes positions, orientation, context and identity to an existing Mumble client.
It is not an embedded audio client. Context mismatch removes positioning; it
does not enforce party privacy. Channels and authenticated routing must enforce
access separately. An embedded solution still needs microphone/output selection,
PTT, Opus transport, credential association and reconnect tests. Its service must
fail independently of gameplay. [Mumble's license](https://github.com/mumble-voip/mumble/blob/master/LICENSE)
permits redistribution with retained notices; bundled dependencies require their
own review. The [protocol documentation](https://www.mumble.info/documentation/developer/)
links current protocol definitions; older narrative protocol pages explicitly
warn they describe 1.2.8. No audio latency or encrypted-session test has run.

## Required runtime acceptance

1. Two real clients join the same BeamMP server with this plugin. Confirm world
   alignment, spawn, vehicle damage and movement before and after a rebase.
2. Disconnect one client; after the next anchors snapshot its origin must no
   longer appear in `beamngEarth_core.mpStatus()`. Reconnect using a recycled
   player ID at another place, then move back into range. Confirm no old-position
   jump, false far-car history or physics instability.
3. Repeat after a pose outage longer than 15 seconds and after a map reload.
4. With reproducible test coordinates straddling the date line, confirm no
   forced meeting teleport within 3 km and damage synchronization within 2 km.
   Confirm the offset destination stays within valid longitude bounds.
5. Test delayed, jittered and interrupted connections while monitoring client
   logs for dropped packets and vehicle instability. The existing 1.5-second
   anchor-settling heuristic still needs measurement under those conditions.

No release should treat these offline checks as evidence of working Steam
parties, voice, real multiplayer recovery, or engine performance.
