# Dutch Groenpol vehicle traffic signals

This change extends the Dutch RVV signs PR with vehicle traffic signals from
the user-confirmed Groenpol.fbx (SHA256
0c87eef5eea270f91807cabdaf9f37df3ebec64d115e6d99e6ecc28100effe77).
The submitted derived models retain their provenance; no third-party substitute
models are used. Full branch installation includes the required models, textures,
Lua modules and offline context. No user-specific launcher is required.

OSM-confirmed car signals use the existing native controllers. Lane counts and
direction metadata select one primary head per incoming lane where a complete
safe layout fits. Through lanes use round lenses. Urban lenses are 200 mm;
rural, mixed-zone junctions and motorway exits use 300 mm. Dutch phases go
directly from red to green, then yellow and red. No bicycle/pedestrian phases
are invented. Foreign signal behavior is retained.

The construction uses separate masts, original brackets and independently aimed
heads. Cantilevers carry at most three overhead lane heads per arm. Wider
layouts use procedural portals or verified mirrored cantilevers/3+1 alternatives.
Shared posts retain separate controllers and reference-count their hardware.
Safe left feet prefer a median. Remote portal feet may use a nearby standing
repeater instead of a duplicate attached low head. Fitting backboards remain;
only an actually obstructed backboard is removed. Low head yaw is limited to
10 degrees and upper head yaw to 15 degrees from the incoming road axis.

Foot checks use actual drawn road/walk geometry and measured terrain. The
optional local Venlo BGT context recognizes sidewalks batched into road skins
only when their object IDs match the drawn BGT source. Outside known polygons
the native checks remain. This does not ship the BGT server road adapter or its
height work; those will join this same PR in a later update. BGT polygons alone
do not supply heights, stop lines, controller phases or lane topology.

Offline context packs contain WKD/OSM lane and zone context plus the bounded
Venlo BGT polygons. There are no gameplay PDOK or Overpass calls. The large
NDW/George sign inventory is still supplied separately with --nl-signs-store.
The server must be restarted after changing its sign-store configuration.

Validation includes LuaJIT placement, shared-object lifetime, 200/300 mm sockets,
independent red/green control, lane counts, mirrored structures, road clearance,
bounded heading and backboard geometry. Visual acceptance remains incomplete:
the exact Tegelseweg-Danoweg mast placement still needs an on-location check.
No nationwide completeness or performance claim is made.
