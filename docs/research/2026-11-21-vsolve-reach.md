# The solve's reach, for tracks (2026-11-21)

`2026-11-18-vsolve-default-on.md` left Silistra's step open. Silistra's track ends 1.6 m off the next track's centre
line. The solve joins an end only within 1 m of a line (`vsolve.JOIN_M`), so it held the track at its profile, 3.4 m
over the other track, which the dip under the bridge takes down (drivability 1.5 with the passes, 5.8 with the solve).
The passes' snap joins an end within the road's half width plus 3 m (`grading.JUNCTION_REACH_M`). `vsolve.joins` can
do the same (`JOIN_REACH_M`, `BNE_VSOLVE_REACH`), but it stayed off: it gave Tokyo 4 seams over 30 cm.

## For every road

Measured again on this branch (dev 560, the next cells' roads in each solve: `_vsolve_context`), with
`BNE_VSOLVE_REACH=on` for every road:

- Tokyo has no seam over 30 cm any more. A limit from the cell's edges (no reach within 50 or 150 m of them) changes
  nothing there.
- But the other places get worse: Lauterbrunnen gets a 0.56 m seam (none without), Amsterdam a 0.17 m one (0.015),
  Lagos and Tokyo more corridor steps (248 → 277, 819 → 901), and the drivability total goes from 1281.5 to 1296.7:
  Silistra −4.4, but Vienna's Rautenweg +7.3, Südtiroler Platz +5.1, Boston's Bowker +2.7, Riva +2.6 and others.

## For tracks alone

`vsolve.REACH_CLASSES` (`BNE_VSOLVE_REACH_CLASSES`, default `track`): only the end of a track reaches. The road it
reaches can be any class.

- test_drivability: 1277.0 (1281.5): Silistra 5.8 → 1.4, every other site unchanged.
- grade_metrics on the 8 places: every metric identical to the solve without the reach.

So the reach is on by default for tracks. `BNE_VSOLVE_REACH=off` turns it off;
`BNE_VSOLVE_REACH_CLASSES=track,service` widens it.
