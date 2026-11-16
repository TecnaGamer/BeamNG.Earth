# Initial ground loading: recovery and validation

## Demonstrated failures

The real Lua core consumer, with engine/transport boundaries stubbed, discarded
its pending request before importing the first terrain. If that import returned
false, no request remained to retry and the automatic start was already marked
done. The original consumer failed the transient-import regression; the corrected
consumer keeps the prepared message until import succeeds.

Three connected-server regressions also failed on the original implementation:
exceptions while creating the output folder, waiting for land cover, or finishing
the ready message escaped without `anchor_error`. They use fresh temporary folders
and injected failures, not live providers. The outer preparation boundary now
returns the matching request ID, error and stage for each failure. Cancellation
releases the foreground hold and stops the progress heartbeat.

These demonstrate failure paths relevant to #770 and #818; they do not establish
the causes of those reports. The pre-listen startup phases and launcher diagnostics
help investigate #765; they do not claim to fix its unreproduced Ubuntu startup
failure. A slow cold build alone is not evidence of a deadlock.

## Bounded recovery

The first load and explicit map teleports share a 30-minute deadline across
generation retries. Reported server failures have ten attempts with exponential
backoff. A failed engine import has three local attempts, separated by 2 and 4
seconds, reusing the same files. An exception during commit stops immediately:
retrying an unknown partial commit would be unsafe. No anchor is acknowledged
before a successful import. Replaced, duplicate and late replies cannot consume
the retry budget or commit the abandoned location.

A deadline produces a persistent loading-bar error and a game notification, with
instructions to save logs and select the location again. The notification also
works when the loading bar is disabled. There is no automatic new generation job
after a silent timeout, since an executor may still be running the original one.
The changes do not cancel executor threads or redesign rebase/follow scheduling.

`anchor_progress` is additive to protocol 4, sent initially and every two seconds,
with a request ID, stage and elapsed seconds. It is not a percentage or a reason
to extend the deadline. Old clients ignore it; old servers still work with the
new client's existing ready/error replies and local deadline.

## Automated checks

```text
python tools/run_tests.py test_initial_loading test_launcher_lifecycle test_launcher_linux test_connection_cleanup test_protocol_agreement test_server --par 3 --timeout 600
python tools/run_lua_tests.py
```

The new Python module covers eight cases: three setup/finalization errors, slow
build progress, cancellation cleanup, and three startup diagnostic cases. The Lua
fixture drives the actual core consumer with a virtual clock: transient and
permanent imports, bounded generation retries, stale replies, manual recovery,
heartbeats that never complete, and an error surviving the loading bar's normal
expiry. Its 30 assertions include duplicate delivery, recovery while paused and
exceptions during import. These fixtures do not load BeamNG.

On Windows/Python 3.10, all 93 Lua modules passed. The foundation Python modules
passed 37/39: the two failing modules were `test_cache` (two symlink tests lacking
the required Windows privilege, WinError 1314) and `test_process_lifecycle` (three `guard.json` fixture
readiness timeouts). Both sets of failures were reproduced on an unmodified
`e2ff61f5` checkout on the same machine. They remain visible failures, not skips or
a green quality gate. Node map-math tests, Rust server-list tests and both player
and BeamMP package builds also passed.

A Windows source-launcher smoke check used new user/cache directories, recommended
providers, `--offline`, `--port 0` and `--map-tiles off`. The owned child reached its
post-bind marker, negotiated protocol 4, sent initial progress, returned a matching
terrain-stage error for missing offline DEM data, then stopped and released its
port. No game was started. This checks cold startup and error delivery, not online
download throughput or successful engine import.

## Game acceptance still required

1. Start the packaged candidate with empty user/cache folders and a working network;
   load one location and then a second uncached location. Check displayed phases,
   first placement, collision, and successful file import.
2. Repeat with cached data, offline missing data, and an interrupted connection.
   Check bounded retries, terminal guidance and recovery by selecting a location.
3. Test a slow machine and a deliberately unavailable terrain file. Check import
   backoff, no duplicate commit, and recovery while paused or without a vehicle.
4. Reproduce the Ubuntu pre-listen failure separately and retain the last startup
   step, exit code and traceback. Do not infer its cause from a cleanup warning.

No BeamNG/BeamMP gameplay, Linux source-process startup or native crash resolution
is certified by the automated checks above. No native ABI or binary changes.
