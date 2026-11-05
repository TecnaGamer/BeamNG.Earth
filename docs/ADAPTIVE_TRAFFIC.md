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

## Validation and remaining work

The focused Python tests cover population interpolation, settlement lookup, graph metadata, road-class priority,
mapped-signal precedence and existing roundabout behavior. The Lua navgraph unit test covers physical clipping versus
the route halo. These source tests do not exercise BeamNG.drive's native traffic pool or spawn lifecycle.

This port does not yet include the reference's per-driver speeding profiles, post-respawn AI speed reapplication,
spawn-velocity impulse or its in-game velocity diagnostics. Those behaviors require a careful port against the current
vehicle lifecycle hooks and an in-game BeamNG test before they can be claimed as supported.
