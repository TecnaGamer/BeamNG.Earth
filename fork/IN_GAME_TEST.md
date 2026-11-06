# Testing the QLD fork in game

## Status
- The fork's mod is installed at `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\mods\unpacked\beamng_earth`.
- Your previous (upstream 1.8.2) mod copy is backed up at `C:\Users\xavec\beamng_earth_upstream_mod_backup_1.8.2`.

## Start
1. Double-click **`run_server.bat`** in `C:\Users\xavec\beamng.earth`. It runs the fork's code from this repo; keep
   its window open.
2. Start BeamNG.drive normally (Steam), open **Freeroam**, and pick the **BeamNG.Earth** level.
3. **Don't start your old `BeamNG.Earth-….exe` launcher.** It reinstalls the upstream mod over the fork's. (The fork's
   `launcher.py` does the same with the fork mod, so `.venv\Scripts\python.exe launcher.py` also works.)

## Getting around
Press **M** for the world map. Type `latitude, longitude` into its search box to teleport.

| # | Where | Coordinates | Look for |
|---|---|---|---|
| 1 | Indooroopilly / Taringa suburbs | `-27.4990, 152.9750` | Colorbond + tile roofs, brick/weatherboard houses, gum trees, drier grass, fewer lawns |
| 2 | Paddington (Given Tce) | `-27.4598, 153.0003` | Same, inner suburb; palms among the gums |
| 3 | Mt Coot-tha lookout road | `-27.4760, 152.9580` | Bushland: should be eucalypts, not oaks |
| 4 | M1 Pacific Motorway, Eight Mile Plains | `-27.5830, 153.0960` | Edge lines; trees beside the motorway |
| 5 | Bruce Hwy south of Gympie | `-26.2060, 152.6690` | Rural markings: double solid centre on no-overtaking bits |
| 6 | Cairns, Edge Hill | `-16.9020, 145.7470` | Queensland houses; tall trees mostly rainforest |
| 7 | Longreach | `-23.4410, 144.2500` | Driest grass |
| 8 | London (control) | `51.5007, -0.1246` | Should look exactly like upstream |

## Console checks (`~` opens the console, Lua/GE)
- `beamngEarth_groundcover.status()` shows `dryShare`: ≈0.5 in Brisbane (0.4–0.6 by season), ≈0.65 at Longreach,
  nil in London.
- The log should not contain `no shapes for group eucalypt`.

## What's expected not to change yet
Signs (still Vienna-style speed discs; no green direction boards), signals (US style), fences (US wooden), kerbs,
gas station look, Queenslander houses on stumps. Those are Phase 2–3.

## Report back
For each spot, a screenshot (F12 in game, or Win+Shift+S) and a word or two: better / worse / too much
(e.g. "grass too dry", "roofs too dark"). Tuning values are one-line changes.

## Switch back to upstream
Run your old BeamNG.Earth `.exe` (it reinstalls its own mod), or delete the `unpacked\beamng_earth` folder and copy
the backup folder back in its place.
