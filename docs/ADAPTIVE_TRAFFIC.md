# Adaptive traffic and junction priority

## Population target

`server/traffic.py` combines a smooth nearest-settlement curve with drivable-road length sampled inside a 750 m radius.
The settlement catalogue is the bundled Natural Earth populated-place layer in `maplayers.js`; its scalerank is only a
coarse settlement-size proxy. Road density, built-up share and road class contribute to the target, while motorway
mainlines and confirmed divided carriageways receive a bounded multiplier. When the settlement catalogue or player
coordinates are unavailable, the model falls back to the mapped road network.

The server attaches the resulting diagnostic profile to each navigation graph. Lua caps the target to the configured
or native traffic amount and adjusts BeamNG's native active ceiling by at most one vehicle every five seconds. Pool
selection, activation and retirement remain with BeamNG. Population changes pause while the navigation extension is
holding its traffic pool for a teleport or while a locally occupied traffic vehicle is protected.

## Navigation graph and junction controls

The graph keeps its original eight edge fields and appends legal speed, road class, divided-road and built-up metadata.
The server samples adaptive density around the player; this metadata is also available for future live re-sampling.

Large terrain blocks get up to 650 m of extra AI route graph coverage. The halo applies only to graph clipping; the
terrain box is not expanded. Physical signal heads and controllers are filtered to the terrain box, and small blocks
retain their original graph boundary to keep the route horizon outside the native spawn range. Traffic guard and
respawn checks continue to use the physical block.

At unsignalised junctions, lower-priority incoming road classes receive stop controls and the AI graph encodes matching
priority. Explicit mapped signals take precedence over inferred priority. Roundabout rules remain governed by the
existing ring detection and yield handling.

## Driver speed and spawn handling

Traffic vehicles receive a shuffled 20-profile speed deck (15 slightly above the posted limit, 4 near it, and 1
faster). The selected target uses appended legal-speed metadata from the road that best matches the vehicle's heading;
a perpendicular junction road cannot replace the limit of the road the vehicle follows. The vehicle receives explicit
car avoidance and speed-limit mode. Periodic updates reapply the target only when the mapped legal speed changes.

Fresh spawns and native pool respawns wait until the traffic record leaves its reset stage and reaches fade-in/active,
then allow a short delay for BeamNG's AI refresh to finish. A guarded one-shot spawn-speed impulse is skipped if the
vehicle is reversing, already near its target, lacks mapped speed metadata, or is close to a multi-arm junction. The
companion vehicle-Lua helper attempts to synchronize wheel tread rotation with body speed using balanced per-wheel
forces.

## Validation and remaining work

The focused Python tests cover population interpolation, settlement lookup, graph metadata, road-class priority,
mapped-signal precedence and existing roundabout behavior. Lua tests cover route clipping, speed-profile selection,
mapped-road speed choice, respawn gating and wheel-force balance.

Source tests do not exercise BeamNG.drive's native pool, vehicle VM, spawn/respawn cadence or measured speed response.
In-game testing remains necessary before the AI speed commands and one-shot body/wheel impulse can be considered
validated. Opt-in multi-sample spawn-velocity diagnostics from the reference are not included in this port.
