# Mapped street-lamp assets and lighting

Updated against upstream `main` at `e1c72abf` for **BeamNG.Earth 1.9.1-beta**. Existing French lanterns retain their lighting profile alongside ordinary and high-mast lights.

Mapped OSM `highway=street_lamp` nodes now use these stock assets globally wherever infrastructure data is available:

| `lamp_mount` | Stock asset | Authored scale reference |
| --- | --- | --- |
| `high_mast` | Driver Experience `buildings/s_light_pole_high_mast` | 18.03 m |
| `straight_mast` | Driver Experience `buildings/s_light_pole` | 12.25 m |
| `double_arm` | West Coast USA `objects/pole_light_double` | 13.76 m including buried foundation |

Other mount values retain the existing ordinary streetlight. Only active OSM lamp nodes qualify; disused and underground features remain excluded. Nearby drivable road geometry determines rotation: single arms aim inward toward the closest road segment, while double arms run perpendicular to that segment. ETK straight poles account for their authored -Y arm direction; ordinary/WCA poles use +X. Footpaths are excluded and explicitly mapped road/lamp layers must agree. Numeric OSM `direction` is a fallback when no eligible road is found within 80 m. The pole and its light heads rotate together. Ground sampling and pole-based tile ownership are retained, including when an arm extends into a neighboring tile. Mapped lamps are no longer limited to Wyoming. Roads with mapped poles suppress guessed lamps along that road; existing regional fallback rules remain on roads without mapped points.

Mapped poles now emit the same lens/glow rows used by the existing night-light system. Straight/ordinary lamps have one light, double-arm poles have two oppositely aimed lights, and high-mast poles use a broad downward floodlight profile. Lighting turns off by day; far glow is night-only. Nearby illumination uses the existing bounded, quality-controlled spotlight pool rather than creating an unlimited light for every distant pole. Asset geometry and material references are reused from the installed game; no stock files are copied or modified.

Both the thread fallback and road worker enable mapped lamps globally. Existing regional fallback spacing is retained for unmapped roads. No server restart or cache purge was performed. The new client groups/night-light profile and server code must both be loaded to see the change; in-game appearance and nighttime brightness still need user verification.

Earlier validation on the focused upstream-based PR tree: 10 world-prop tests, 39 furniture tests, 108 road tests, 6 blockworker tests, 13 highway-sign tests, 14 service-sign tests, 6 national-coverage tests, and 155 Lua assertions across the tree/night-light modules pass. Checks cover asset selection, authored scale, terrain-relative light heights, both double-arm heads, tile ownership, inactive-node exclusion, day/night switching, light-profile restoration, both sides of a road, curved segments, footpath exclusion, layer matching, median orientation and mapped-lamp preference over guessed placement.

## Laramie exit 310 orientation check

`probes/lamps-exit310.osm.json.gz` contains an isolated OpenStreetMap query around both exit 310 ramps, including 20 lamp nodes and nearby roads (OSM timestamp November 8, 2026). The sample has 12 straight-mast lamps, 7 bent-mast lamps and one unspecified mount; none has a `direction` tag. Previously all received zero yaw, leaving their arms facing a fixed compass direction instead of the ramps.

The road-based audit now finds inward-facing arms for all 20 nodes. Reproduce with:

```powershell
.venv/Scripts/python.exe tools/check_lamp_orientation.py probes/lamps-exit310.osm.json.gz --output probes/lamps-exit310-orientation.json
```

This is an offline geometry check, not an in-game screenshot or a guarantee that an authored arm reaches across every mapped shoulder. Pole positions remain at their mapped coordinates. The running server was not restarted and no active caches were cleared. Normal source-code cache revision changes handle rebuilt tiles on the next launch; the user has verified the Laramie result in-game; other locations have not been individually verified.

## 1.9.1 selection efficiency

Nearest-light selection retains only the requested spotlight-pool count in a max heap. It scans the existing lens/provider positions, allocates at most the pool count in candidate records, and sorts only that retained set. Radius and view-cone filtering, nearest-first ordering, quality limits, shared-pool reuse, and daytime disabling remain in place. A zero-light request skips candidate/provider work.

A synthetic LuaJIT 2.1 test selecting 128 lights from 20,000 lenses reduced median selection time from **11.891 ms to 1.689 ms** over seven samples. This excludes engine lighting/rendering costs and is not an FPS benchmark. Regressions cover dense-area nearest ordering, bounded candidate sorting, zero-light requests, and reuse of the same pool slot for lantern, ordinary, and high-mast profiles.
