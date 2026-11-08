# U.S. service-sign coverage setup

The national coverage pipeline is configured and its background build has been started. **Nationwide data is still being indexed/built; it is not yet complete or verified in-game.** The user has confirmed the original Wyoming sample works in-game. That sample remains the fallback while regional cells are published.

## Scope

Coverage targets mapped motorways and explicitly controlled-access trunks (`motorroad=yes` or `expressway=yes`) throughout the 50 states and DC, using one United States OSM extract. It does not make signs appear on every road, invent missing businesses, establish paid program participation, or extend U.S. conventions internationally. Existing conservative access rules and state policy defaults still apply. Ordinary trunks can be used as local access roads without being treated as freeways.

The bulk source is [Geofabrik's United States OSM extract](https://download.geofabrik.de/north-america/us.html), approximately 11.4 GB when setup began. Using the national extract avoids gaps from processing individual state boundaries. Data remains subject to [OpenStreetMap attribution and ODbL](https://www.openstreetmap.org/copyright). The builder's disk-backed location cache uses [pyosmium's documented FileProcessor location storage](https://docs.osmcode.org/pyosmium/latest/reference/File-Processing/).

## Running job and progress

The separate Python data job runs with below-normal priority in Windows. It does not restart/control the BeamNG server, clear live caches, or perform routing in gameplay.

```powershell
.venv/Scripts/python.exe tools/service_sign_coverage_status.py
```

This prints phase, process ID, downloaded bytes and, when available, completed/total cells and directed exit counts. Download transfer details and estimates are in `build/service-signs-us/build-error.log`; processing progress is in `build/service-signs-us/build.log`. Transfer estimates fluctuate. Downloading and nationwide indexing/routing are substantial background tasks; setup does not mean the data has already finished.

To resume after a shutdown or failure:

```powershell
tools/start_us_service_signs.bat
```

The launcher checks for an already-running builder and the builder also holds an OS lock. The batch file uses a process-local PowerShell execution-policy override for this repository's launcher; it does not change machine/user policy. Alternatively run `.venv/Scripts/python.exe -u tools/build_us_service_signs.py` in a terminal.

## Build stages

1. Download/resume the national PBF and verify it against a pinned source checksum. A rotated or corrupt download fails closed instead of publishing partial data.
2. Import relevant roads, service POIs, junctions, barriers, restrictions and military areas into SQLite/RTree. Node locations use disk-backed storage in a separate import process, which releases Windows file mappings before routing starts.
3. Build independent half-degree cells with a geographic halo derived from the largest policy routing limit plus 2.5 km for approaches. Whole source features are retained. Routes can cross cell/state boundaries; ownership follows the exit's cell, not the business's cell.
4. Atomically publish completed gzip JSON cells and a checksum manifest under `server/data/service_signs_us/`. Wyoming is prioritized during cell routing. A cell is published even when it contains zero eligible associations, so an authoritative empty result can retire stale fallback records.
5. Mark the manifest `complete=true` only after every candidate cell completes. In-progress builds and completed cells can be resumed without routing completed cells again. New source/code/policy snapshots retain previous published cells until replacements are ready.

The bulk download, SQLite database, location files and logs stay under the ignored `build/service-signs-us/` directory. Generated national cells are also ignored by Git. They are separate from active game/server caches. Import stops with resumable files retained if available disk space falls below 15 GiB. Initial indexing may need tens of gigabytes beyond the PBF.

The default work directory pins one source snapshot so interrupted downloads can resume safely. A future fresh-source refresh should use a new work directory while keeping the same output, for example:

```powershell
.venv/Scripts/python.exe tools/build_us_service_signs.py --work build/service-signs-us-refresh --output server/data/service_signs_us
```

Custom work directories write their own `status.json` and logs. The convenience status command/launcher monitor the default work directory.

## Runtime integration

`server/servicecoverage.py` reads local manifest/cell files only. It verifies checksums and paths, bounds cell decompression, caches at most 16 decoded cells within a 128 MiB estimated memory budget, and tracks at most 64 requested cells. It never downloads OSM data or constructs a road graph. Changes outside requested regions do not continually invalidate the player's signs.

`servicesigns.Source` merges the national cells with the bundled Wyoming fallback. Completed national cells take precedence; missing/corrupt cells keep fallback signs available. The existing meshes, shared font atlas, sign visibility and streaming are retained. No new protocol or Lua component is needed.

The currently running server was not restarted or hot-patched. **The updated loader becomes active at the next normal launch of this source.** After that, completed national cells become available through ordinary sign jobs and the existing approximately five-second local revision checks. There is no need for a destructive cache purge.

## Validation

- Six new national-coverage tests pass, including a real binary PBF import, road-routing halos across a cell boundary, deterministic resume, controlled-trunk rules, fallback replacement/retirement, corrupt-cell fallback, path safety, and region-specific revision behavior.
- All 14 original service-sign tests pass.
- All 13 upstream highway-sign tests pass.
- The focused upstream-based sign suite reports 176 tests, 175 passing, with the font-atlas reproducibility failure also reproduced on untouched upstream. See `docs/service-signs.md`.

The national dataset's final cell/exit counts are not known until the build finishes. Nationwide in-game appearance has not been claimed. Conservative OSM connectivity can omit services, and inferred business identification is still distinct from verified roadside signs or paid-program participation.

## Files

- `tools/build_us_service_signs.py`: download, disk import, resumable regional routing and publication.
- `tools/start_us_service_signs.ps1`, `.bat`: independent hidden background launcher.
- `tools/service_sign_coverage_status.py`: progress display.
- `tools/service_signs.requirements.txt`: offline builder dependencies.
- `server/servicecoverage.py`: bounded local cell reader.
- `server/servicesigns.py`: national source integration and trunk placement.
- `server/servicesigns_index.py`: controlled-trunk versus local-trunk routing rules.
- `server/tests/test_service_coverage.py`: isolated tests.
- `.gitignore`: excludes generated national cells and existing build outputs.

No BeamNG process, existing road/bridge generator, terrain feature, sign material, or active cache was modified by the build job.
