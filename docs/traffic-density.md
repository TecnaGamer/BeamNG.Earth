# Adaptive traffic density

BeamNG.Earth adjusts the active traffic population to the mapped character of the roads around the player. This is an estimate of how busy an area should feel, not an exact real-time count of vehicles on each road. It does not need a new third-party API account or API key.

## How it works

The server samples drivable road length within 750 m of the graph centre, classifies road types, and uses the available ESA WorldCover built-up classification to distinguish developed areas. It produces one of five bands:

| Band | Intended setting | Target multiplier |
|---|---|---:|
| City | Dense urban streets and central business districts | 1.00 |
| Suburban | Built-up neighbourhoods and outer urban roads | 0.78 |
| Town | Smaller built-up centres | 0.58 |
| Rural | Connected country roads and regional areas | 0.30 |
| Remote | Sparse roads / a lone highway through remote land | 0.12 |

The multipliers are relative to the player's chosen traffic amount. In automatic amount mode only, city centres may receive up to a 25% increase over the resolved baseline, capped at 12 active vehicles when the baseline is below 12. An explicit amount selected by the player is never exceeded. Counts are rounded and at least one active vehicle is retained when traffic is enabled. BeamNG still decides the individual vehicle routes, spawn points, AI behaviour and recycling; the profile changes the active population rather than forcing an exact count onto each road segment.

WorldCover may be unavailable while a map cell is first downloading. In that case, dense road networks still classify as urban/suburban using road density; sparse profiles remain estimates until map coverage is available.

## Confirming it is working

Enable traffic in the Freeroam traffic options, start BeamNG.Earth, then watch the BeamNG console/log. The mod reports the baseline when adaptive traffic starts, and a new line when the local traffic band or target changes. For example:

```text
adaptive traffic: enabled (baseline 8, automatic: gameplay_traffic.getTrafficVars); profiles update with the local road graph
adaptive traffic: city | target 10 active vehicles (baseline 8, automatic amount) | road density 10.32 km/km^2; local roads 7.18 km/km^2; built-up roads 86%; major roads 24% | source=mapped-roads+available-WorldCover
```

The exact measurements depend on the location. As you travel out of town, the band should move toward `suburban`, `town`, `rural`, then `remote`, and the active-vehicle target should usually fall. The log only emits a new profile line when the band/target changes, so it does not spam the console every frame. A warning saying `no trafficProfile received from server` means the server build and mod files are mismatched or the server could not supply the new profile. A warning that the baseline fallback was used means BeamNG's resolved traffic setting could not be read; the feature will still operate with a conservative baseline of six.

For a fuller diagnostic snapshot, enter `beamngEarth_core.status()` in the BeamNG console. The returned status now includes `navgraph.trafficDensity` (the profile currently applied), `navgraph.trafficBaseline`, and `navgraph.incomingTrafficProfile` (the latest server estimate). This is useful if the log lines scroll by too quickly.

## Limitations

This first version is geographically adaptive, not a live traffic service. It will not model a specific accident, rush-hour event, road closure or exact vehicle count. Exact current traffic counts are not broadly available for all roads worldwide, and adding such a provider would require a separate data-source decision, possible API credentials and an assessment of fees and coverage.
