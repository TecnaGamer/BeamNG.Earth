# Regional infrastructure on 1.7.3-beta

The contribution can be merged with official v1.7.3-beta
(`8b0e9b04592fdc27c16a85aeaa055aa3dda58e10`) without a text conflict.
The feature scope and opt-in `--regional-poles europe` switch are unchanged.

## Windows validation fixes

The first Windows run on the merged base reported seven failing modules.
The attached logs identify three distinct causes:

- Six modules encounter `PermissionError: [WinError 32]` when removing temporary
  `revisit/where/*.tsv` files. Official 1.7.3's `revisit._note` retains their file
  descriptors in a process-wide dictionary. It now opens, appends one row and
  closes the descriptor in a `finally` block, under the existing process lock.
  Rows remain in the per-process O_APPEND sidecar and are not disabled. Tests
  cover successful/failed writes and 100 concurrent appends. The descriptor
  regression test fails against the original 1.7.3 implementation.
- The recessed-pole export test compares a newly generated LF COLLADA file
  against Git's Windows CRLF checkout as bytes. UTF-8 text comparison now ignores
  only universal newline differences. Geometry/material text and decoded PNG
  pixels remain checked; an actual COLLADA text modification still fails.
- The two `test_roadgeo.RealDataTest` integration cases see an existing map cache
  directory and assume it contains their Fells Point/Baltimore map and elevation
  data. These logs show missing offline Overture/DEM data there. They are not
  evidence of a geometry regression and cannot be validated from this cache.

No model assets, country data, native binaries or game Lua files are changed by
these Windows validation fixes.

## Reproducible targeted checks

On Windows, after `py -3.12 install.py`, run from the repository directory:

```powershell
$env:BNGE_ROADGEO_REAL = "0"
.\.venv\Scripts\python.exe .\tools\run_tests.py test_regional_poles test_countrybounds test_regional_furniture test_regional_revision test_europe_infrastructure test_delineator_density test_regional_geotiles test_furniture test_navgraph test_sidewalk_collision test_blockworker test_revisit test_roadgeo test_geotiles test_areas test_signready --par 2
Remove-Item Env:BNGE_ROADGEO_REAL
```

`BNGE_ROADGEO_REAL=0` is the existing test-only switch for those two integration
cases, not a server setting. They remain unchanged and should be run with the
switch enabled when the required Baltimore cache is available. Do not count
them as passed. The supplied correction helper sets it only in its test child
process, leaving the player's environment unchanged.

The corrected 1.7.3 merge was checked in Linux/Python 3.12 with the matching
official earthfast VERSION 116: 212 tests in 16 modules, 209 passed and 3 skipped,
no failures. Skips: the two real-data cases above and one optional LuaJIT
agreement case because LuaJIT was unavailable. Resource-closure and CRLF probes
also passed. These are targeted results, not the whole upstream suite.

Windows rerun and manual in-game validation of the corrected 1.7.3 version are
pending. These changes do not establish or fix the cause of the separately
reported Vulkan GPU crash.
