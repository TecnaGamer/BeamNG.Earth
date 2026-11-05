# Experimental Earth lane-following AI

The **Earth lane-following AI** is a separate, opt-in driving mode for the `beamng_earth` level. It does not replace BeamNG's native Traffic, Chase, or other AI modes. The setting is under **Settings → Traffic AI** and defaults to off. Its population setting is gradual and capped at 32 vehicles.

## Data and route lifecycle

The companion server queries the globally indexed OpenStreetMap Layercake highways dataset directly. The source contains world-wide road geometry and tags; its spatial row-group index lets the server fetch the nearby area without loading the full global dataset into memory. The server builds a local directed lane graph around the player, using mapped lane counts, one-way direction, driving side, road class, and speed limits. Only spawn poses and their forward route polylines are sent to the game. The graph is rebuilt as the player moves, independently of the streamed Overture road graph used by the existing traffic integration.

The geometry offsets each mapped road line into lane centres. At an intersection the route selector prefers the continuation with the smallest heading change and closest matching lane position, so this first version carries on straight. Cars are spawned around the player at a controlled cadence, drive their server-provided polyline with a standalone pure-pursuit controller, and use a bounded speed controller. Mapped `maxspeed` is used where available; otherwise a conservative road-class default is used. Driver profiles vary between cautious and near-limit speeds without exceeding the selected limit.

## Deliberate limits

- No obstacle or vehicle avoidance, following distance, turning, lane changing, or automatic destination routing.
- Roads marked private or motor-vehicle prohibited, plus driveways and parking aisles, are excluded.
- An off-route car or a car reaching the supplied route's end is stopped and despawned by the GE manager. Cars occupied or controlled by a local player are stopped and retained.
- The route source is global, but only a nearby graph is constructed and held for each active player. This avoids a full-world graph allocation and keeps requests spatially bounded.
- The first version places route points from the elevation model. Raised bridge decks, complex multilayer interchanges, and imperfect/incomplete OSM lane tags may therefore produce height or lane-placement errors.
- This mode has offline Python and Lua stub tests, but still needs in-game validation across road classes, rebases, bridges, junctions, vehicle switching, and multiplayer.

## Developer checks

```sh
python tools/run_tests.py test_earthai test_osmtags --par 1
python tools/run_lua_tests.py test_earth_lane_follower
```
