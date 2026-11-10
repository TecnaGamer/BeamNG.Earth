# Adaptive traffic and junction priority

From ProxyFirewall202's PR #728 ("Adaptive Traffic Density and AI Safety Improvements") and the three commits on
PR #773's head that did not come in with its merge (22ba0aee, c15f1ecf, 5e9988bf), ported onto dev on 2026-11-10.
Both parts are off by default: neither has been checked in game yet.

## Population target (setting "Adaptive traffic amount", off by default)

`server/traffic.py` combines a smooth nearest-settlement curve with drivable-road length sampled inside a 750 m radius.
The settlement catalogue is the bundled Natural Earth populated-place layer in `maplayers.js` (`server/settlements.py`,
no network); its scalerank is only a coarse settlement-size proxy. Road density, built-up share and road class
contribute to the target, while motorway mainlines and confirmed divided carriageways (a matched median) double it.
Tracks and busways do not count as traffic roads. When the settlement catalogue or player coordinates are unavailable,
the model falls back to the mapped road network.

`navgraph.build` attaches the profile to every graph as `trafficProfile` (one vectorised numpy pass over the roads near
the graph's centre, a few ms per graph, built every 400 m in the navgraph worker thread). The edges are unchanged.

In the game (`navgraph.lua`, `M.ADAPTIVE`, Settings > World > Adaptive traffic amount) the stock traffic's active
amount (`gameplay_traffic.setActiveAmount`) moves toward the profile's target by one car at most every 5 s, never above
the player's own amount (the ceiling found before it was touched, or the cars spawned). After a teleport the new
place's amount is set at once instead of the old one. It waits while the pool is held for a teleport and while an
occupied traffic car is protected; an amount changed by someone else (the radial menu, the game's UI) becomes the
player's amount, a ceiling of 0 (traffic toggled off) is left alone, and switching the setting off puts the player's
amount back. The check runs once a second. `beamngEarth_core.status().trafficAdaptive` shows the state.

## Junction priority (BNE_PRIORITY_STOPS=on, off by default)

At unsignalised junctions the roads are ranked trunk > primary > secondary > tertiary > unclassified > residential >
living street. The road through the junction is its two highest-ranked arms (so a road whose class changes there does
not stop); every lower arm coming in gets a stop sign, a stop line and an AI stop, in the country too, and the AI
graph's approach edges carry the same order in their drivability (the stock AI gives way on the lower one; the road
through comes in 2 m/s slower). Mapped signals take precedence; with no infrastructure data the hierarchy wins over
guessed signals. Sweden keeps its own rules, Germany's junction kernel its junctions. Junctions with a link or a
motorway (ramps and merges), and arms of service roads, driveways and tracks, are left as before.

One fix from the PR is on for everyone: no stop sign stands on a one-way arm that only leaves a junction.

## Left out, and why

- **Per-driver speed deck** (15 slightly over the limit, 4 near it, 1 faster): dev's `speeding.lua` (2026-11-09) gives
  every traffic car a habit from its country's profile through the same `ai.setSpeed`; two systems would fight.
- **Spawn-speed impulse after respawns**: in dev since PR #773's merge (`navgraph.lua` launch, with a 0.4 s delay
  instead of 0.12 s after a respawn once read 63,000 mph).
- **Freeway lane preference at spawn** (`ai.laneChange` once by speed profile): the AI's later plan nodes come back in
  its own lane (as `overtake.lua` found), so a one-time shift does not hold; dev's `overtake.lua` passes slower cars.
- **650 m route halo for large blocks and the 1800 m server window**: the same as `HALO_M` / `FAST_HALO_M`, which are
  off for their measured frame cost (87.9 / 84.5 fps against 92.8 / 88.2 without, 2026-11-06); the signal clipping to
  the physical block only matters with a halo.
- **Appended edge metadata** (legal speed, class, divided, built-up): only the speed deck and the lane preference read
  it; it would add ~25 bytes to every edge of every graph the game decodes.

## Validation

`server/tests`: test_traffic, test_settlements, test_navgraph (the profile, priority stops in the graph),
test_european_traffic (PriorityStops), test_furniture. `mod/tests/test_navgraph.lua`: the amount's steps against a
stubbed `gameplay_traffic` (cap, toggled off, changed by the player, back off). Not yet in game: the stock pool's
response to `setActiveAmount` while driving, and how the priority stops look and drive.
