# Distant traffic: returning camera views (#565)

Checkpoint before this experiment: `08574894`, also tagged locally as
`distant-traffic-approved-2026-11-01`. The user approved its density, distance fades, time curve and settings.

The renderer previously treated a camera budget change as a new vehicle birth: assignments started with zero
fade, and returning to a road waited for both a full candidate scan and the two-second fade. A minimal engine
fixture reproduced zero bright cars out of ten established cars on return, with route data still cached.

The change separates source/enabling age from camera assignment age. New data and enabling the feature retain
their fade; reusing established streams starts at their existing brightness. Native creation stays at two cars
per frame, while up to 64 existing pairs can be reassigned. Off-screen retiring assignments release their pair
without holding it through a two-second fade.

Four recent view choices retain at most 512 key/id/phase/score records each, without engine-object or geometry
references. Reuse is limited by position, orientation, FOV, aspect, density and a 30-second age. Missing routes
are skipped; rebase, teleport and reset invalidate the choices. A full cooperative scan still refreshes the view.
View vectors are copied so a later camera-vector update cannot mutate a cached view. The 512-car / 1024-plane
cap and the existing road/point limits are unchanged.

At the user's Japan/Fuji view, a controlled 180-degree turn with four seconds looking away reproduced the old
behavior: no returning eligible cars through 1.2 s; at 2 s, 74 cars with mean fade 0.169; at 3 s, 106 / 0.603;
at 4 s, 106 / 0.964. After the change, the first sample at 0.015 s had 50 bright cars; at 0.187 s, 117. All
return samples had fade 1. Camera position was restored exactly and rotation matched within floating precision.
The source connection, city and chosen time were preserved. No world/server reload was used for this experiment.

Module counters over 444 frames in each turn experiment gave 0.471 ms before and 0.406 ms after. Actor counts
were about 111 and 123; the stored pool sizes also differed after the renderer reload. This is limited CPU evidence
at that view, not a controlled universal speedup or GPU/FPS comparison. Scratch captures are under build/camera-turn.

Checks: renderer 1699, pure paths 145, core 282 and settings 285 assertions; LuaJIT compile and whitespace checks.
The slow-scan regression counts actual in-frustum bright objects, not lights behind the camera. Disabling recall
in an in-memory mutant makes that regression fail; the real implementation passes.

The user reports this is much better but occasional faster reappearance is still visible. They also reported
a native crash while removing physical traffic. That report requires separate lifecycle investigation; this
camera checkpoint does not claim the crash is fixed. The next slice updates to current dev and tests that path.
