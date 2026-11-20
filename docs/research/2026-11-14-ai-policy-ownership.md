# AI helper ownership

The stock AI owns steering, throttle and braking. These helpers change its
inputs; there is no global priority scheduler between vehicle extensions.

| Helper | Vehicles and owned input | Coordination and release |
| --- | --- | --- |
| `trafficBoundary` | Traffic: clipped-route planning and a temporary speed ceiling; optional crossing-turn yielding shares that ceiling. | Keeps the lower boundary/turn ceiling and respects the saved speed policy. Restores speed only while its last written speed and mode are still current. Normal map handoffs do not reset the AI. |
| `staticObstacles` / `trafficSignals` | Autodrive: one shared speed ceiling; obstacle passing can request one adjacent-lane shift. | Signals are checked before the obstacle trace and the lower signal/obstacle ceiling wins. The saved external/legal speed remains an upper bound. Release cannot overwrite a newer speed policy. Signals can run with obstacle sensing disabled. |
| `oncomingPass` | Opt-in Autodrive opposite-side passage: temporary waypoint/lane context. | Called by `staticObstacles`, not an independent speed writer. It uses the same ceiling, requests GPS handoffs, and cancels temporary lane context when disabled. It takes over obstacle passage while active; signal braking still applies. |
| `maneuverClearance` | Traffic/Autodrive: short controller freeze after reversing when crossing traffic conflicts. | Does not acquire an already frozen controller. A freeze prevents movement regardless of a speed ceiling. Releases on reset/cancellation or after 15 seconds, handing control back to stock AI. |
| `oncomingClearance` | Opt-in traffic/Autodrive: temporary scaling of `awarenessForceCoef` on narrow roads. | Does not write speed or lanes. Retains 25% of the saved lateral-avoidance coefficient; restores only while its last written value is still current. |
| GE `overtake` | Traffic: one relative same-direction lane shift after being held behind a slower vehicle. | `PLAYER=false` keeps it off the Autodrive car, whose obstacle-passing helpers own those requests. Its cooldown prevents repeated shifts during one pass. It does not own a speed ceiling. |

GPS starts/resumes the underlying Autodrive route. A fresh route establishes the
base policy; the obstacle controller then reapplies a still-needed ceiling.
Traffic and Autodrive helpers are separated by AI mode. Their release checks
protect newer external writes; they do not establish an ordering between
unrelated third-party helpers. User cancellation, reset and unload release owned
overrides. New helpers should use these ownership rules rather than assume their
extension's update order gives them priority.

Some rows describe optional helpers from other PRs in the traffic/Autodrive
stack; this document does not add those helpers as dependencies of boundary
protection.
