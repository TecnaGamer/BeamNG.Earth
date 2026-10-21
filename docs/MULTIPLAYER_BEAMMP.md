# BeamMP co-op (close-formation)

Addresses the long-requested BeamMP integration (#4) for the specific case of players who want to
drive together in roughly the same place: a convoy, the same street, meeting at an address.

## The problem

BeamMP assumes a static level with one shared coordinate frame, and synchronises vehicle positions
as plain world-space vectors. BeamNG.Earth gives every client its own floating origin
(`rebase.lua`), rebased independently whenever that client's own player crosses the rebase distance
from their own current anchor. Two clients' raw Lua-space coordinates therefore generally do not
mean the same real-world point, even moments after both join — BeamMP's own prediction/smoothing/
teleport-correction has no way to know that, and places a remote player's car wherever their last
packet's numbers say, regardless of which anchor they were expressed in.

## What this adds

- `mod/beamng_earth/lua/ge/extensions/beamngEarth/mpanchor.lua` (client, new file). Announces the
  player's own anchor (lat, lon) over a custom BeamMP event whenever it changes (hooked into
  `core.lua`'s `commitTeleport` / `finishRebase`, five lines total there), and transforms every
  incoming remote-vehicle position packet from its owner's last-announced anchor into the local one
  *before* BeamMP's own prediction/smoothing/teleport-correction logic runs — none of that needs
  touching. The math is the existing `geodesy.Anchor:transformTo` rigid transform, the same one
  `rebase.lua` already uses for a client's own frame changes; nothing new there either.
- `beammp/BeamEarthAnchors/main.lua` (server, new file, ~40 lines). A BeamMP server-side Lua plugin:
  relays each announced anchor to every other player, and replays the current roster to a player who
  just joined. Install by copying the whole `BeamEarthAnchors/` folder into the server's
  `Resources/Server/`.
- `beamngEarth_core.mpStatus()` console helper: installed state, the player's own anchor, and every
  peer anchor heard about so far — for diagnosing a stale client (BeamMP only re-downloads
  `Resources/Client/*.zip` on (re)connect, not live into an already-joined session, so an old cached
  copy of the mod predating this patch is the most likely reason two players don't sync).
- No changes to BeamMP itself, no change to its wire format, no central "authority" client — fully
  symmetric, works for any number of players.

## What this does not fix

Only one physics `TerrainBlock` can exist at a time ("Multiple TerrainBlocks: Not supported by
physics ... vehicles sink through to ~z=0.4", confirmed by the engine probes in
`docs/ARCHITECTURE.md`). Every player still only has solid ground within their own ~1 km (512²
block) to ~2 km (1024² block) collidable radius, centred on themselves. This change makes players'
positions *correct relative to each other* wherever they are; it does not give a second player
distant from the first any ground to stand on. Practical range today: close-formation co-op, not
"see each other across a city" — the natural next step once this lands would be a larger `--block`
(the engine accepts up to 8192² per the same probes).

## Validated

Tested with two real BeamNG.drive clients (two machines, two Steam accounts) on a private BeamMP
server: joining, spawning, driving independently, meeting at a chosen address — vehicles correctly
placed relative to each other throughout.

## Open questions / things a reviewer may want to check

- `rvel` (angular velocity) is rotated as a plain vector under the frame's `Transform:rotate`
  (correct for a vector/pseudovector under a proper rotation) — matches the math, confirmed by the
  live test above, but worth a second look given it's not something `rebase.lua` itself needed to
  handle (it moves local vehicles directly rather than correcting network packets).
- Right after a remote peer's rebase, there is a network-round-trip window (their `bngEarthAnchor`
  announce vs. their position packets) during which the previous transform is used. Expected to be a
  sub-second, bounded glitch absorbed by BeamMP's own position smoother like any other correction it
  already handles; not stress-tested under packet loss / high ping.
- A remote player running an unpatched BeamNG.Earth never announces an anchor, so their vehicle is
  simply never corrected (same experience as today) — graceful degradation, not tested explicitly.
