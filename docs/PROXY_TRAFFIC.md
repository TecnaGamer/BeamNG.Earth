# 3D proxy traffic

The opt-in **3D proxy traffic** setting adds moving, non-physics traffic on streamed road lanes. It uses the existing
`traffic_visuals` route stream, so it needs the companion server, Roads, and BeamNG traffic to be on. It works during
the day; distant headlight and taillight sprites are used at night and at longer proxy distances.

Near the player, low-cost cars move on the streamed navgraph: they choose connected lanes at junctions, use the
country's left- or right-hand driving side, follow the car ahead with safe spacing, yield at minor roads, queue at
stop signs, and obey traffic-signal states. Beyond the local graph window, route-sampled proxies follow mapped roads.
Those route proxies also remain eligible inside the near-field radius as a fallback when the connected flow has no
usable road links, so an empty or rebuilding graph does not leave the area around the player empty.

When **3D proxy traffic** is enabled, its **3D proxy car count** setting controls the target from 10 to 1,000,
independently of Distant traffic quality. With Adaptive traffic amount off, proxies target the chosen count across
the nearby road network, including motorways. When Adaptive traffic amount is enabled, location-based road density
scales that target down in less populated areas. The chosen count is the upper cap.

Real-car handoffs use 6 prewarmed donors: at most 4 are active at once and two stay dormant as spares. The nearest
eligible car on the player's current road has first priority; predicted pass distance breaks ties. Closely spaced
graph segments count as the same road when their lanes align. With Adaptive traffic amount enabled, a nearby car on
another road may use a remaining slot. Donors are selected by vehicle dimensions and repainted to match the proxy.
If no suitable donor is free, the low-cost car remains visible. When a physics car leaves the local area, it returns
to its matching graph lane with its speed and paint only if that lane has room. If a safe lane is unavailable, a
distant donor can be retired out of view to make room for an approaching proxy. Ordinary stock moving traffic is
suppressed while proxy traffic is enabled.

For runtime counts, inspect `beamngEarth_core._trafficvisuals.status()`. It reports proxy counts, flow counts,
`promotedVehicles`, `activeStockTraffic`, and reserved donor counts.
