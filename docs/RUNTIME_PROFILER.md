# Live performance profiler

This PR includes upstream dev `11cc55572fd5b828f52a0cf0edf0cf449cba1ac0`
(2 November 2026) and the latest continuous, resizable and compact-view work.
The profiler remains optional and off by default. World-generation features are
reviewed separately; the generic pool hooks also cover upstream's photo process.

Open **Pause menu → BeamNG.Earth → Interface → Detailed performance profiler**.
The option defaults to off. Enabling it starts continuous collection and opens a
resizable window while the Earth level is loaded. Drag its edges or corner to
resize; charts and worker timelines follow the available width, and frame lanes
grow with the window height. The window remembers its size and position.
Closing it or choosing **Compact view** leaves a small status window while
collection continues. **Open profiler** returns to the full view without resetting
history. **Stop profiling** or turning the setting off stops collection and saves
the disabled setting.
Use matching mod and server sources. Older servers still allow game-frame timings;
the window explains when worker telemetry is unavailable.

Collection has no time limit. Frame history rolls over at its capacity instead of
ending the capture. Live frame feedback refreshes every 250 ms in Essential and
100 ms in Full; the server feed retains its one-second / half-second cadence.
The window shows the elapsed capture time and total frames sampled separately
from the bounded recent history. Collapsing the window keeps collection running.
**Freeze display** holds the displayed frames, worker and I/O data while all
collection continues; **Resume display** immediately catches up to recent data.
**Save snapshot** exports the displayed data, including the held snapshot when
frozen. Changing collection mode, level or server session starts a fresh history.

Choose **Essential** (the default) or **Full** beside the toggle or inside the
window. Switching modes starts a fresh capture. Essential keeps frame/FPS history,
CPU/GPU totals, traffic counters, average service latency, failures and disk space.
It skips detailed engine CPU/GPU API queries, Earth scopes, worker/stage tracing,
latency distributions, cache-read timers and OS process I/O sampling. Its frame
history is capped at 180 frames, versus 600 in Full; server updates arrive once
per second instead of twice. These checks are disabled during collection, not
merely hidden. No performance improvement is claimed without a native benchmark.

Navigate between **Frames**, **Workers**, and **Network & Disk**. Full adds:

- Frame wall time, FPS, p95/p99, worst frame and history of the last 600 frames.
- Stacked columns for every captured frame: engine CPU phases, GPU passes and
  Earth Lua scopes in separate lanes. Hover to read costs, click to select a frame,
  or jump straight to the worst frame. Choose 180/300/600 visible frames, fit spikes
  or use the detail scale, and compare against 60/120 FPS budgets.
- Focused observations showing expensive phases, peaks, slow-frame occurrences,
  intentional pacing, missing timing data and possible GPU pressure. Workload
  observations distinguish a long worker queue from long execution.
- Mean and maximum time for the mod's measured game stages.
- A ten-second timeline for each observed process/thread, with execution in blue
  and queue wait in the narrow amber track underneath.
- Running jobs, their sampled current stage, and queued jobs.
- Count, average, p95, queue wait and thread CPU time per pool over the last five
  seconds (at most 120 completed jobs), plus incomplete records and failures.
- Generation steps and an expandable breakdown of the most recent 20 jobs.
- **Freeze display** and **Save snapshot**. Export writes
  `/settings/beamngEarth-profiler.json` in the game's user directory.

## Network and storage

Network & Disk shows the local game/world-server application round trip, bytes
sent/received, socket errors and expired RTT probes. External service rows group
observed HTTP/HTTPS and helper TCP operations by scheme, hostname and port. Each
row shows average time to response headers, average complete-request/session
time, activity, bytes, transport failures, HTTP errors and rate limits. Filter by
active/problem/idle services and sort by latency, traffic or name. Full adds recent
latency graphs, p95 and jitter (standard deviation); stale worker reports cannot
leave a service permanently active. Summary graphs show network and cache rates.

Measurements are passive: enabling the profiler creates no extra external pings
or downloads. Services appear when used during the capture, including imagery,
elevation, metadata, updates and connected helper operations. Previously opened
idle connections are not enumerated. Response latency includes connection setup
and service processing; it is not an ICMP ping. Session duration also includes body
transfer and caller work within the download session. Application RTT includes
game/server scheduling; expired probes do not establish packet loss. Sent HTTP
bytes are an estimate of attempted application requests, not TCP/TLS traffic.
Redirect-hop timings and unrelated game/Steam/BeamMP connections are not measured.
Endpoint records never contain URL paths, query strings, credentials or headers.

Disk displays cache and generated-world volume capacity/free space, low-space
feedback, existing cache inventory/quota, instrumented cache-read bytes and output
notifications. Full adds cache-read operation latency and per-server/worker OS I/O
rates, sampled at most once per second. On Windows,
[GetProcessIoCounters](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getprocessiocounters)
covers all process I/O handles. Those rates are explicitly labeled; they do not
measure physical disk bandwidth or hardware latency. Cache output notifications
count reported produced-file sizes rather than physical writes. Capacity is
sampled every five seconds without recursively scanning the filesystem.

Each process retains at most 128 service records, evicting inactive records first,
and at most 64 recent latency samples per service in Full. The collector retains
at most 128 reporting processes; history graphs retain 120 rate observations.
Missing counters remain unavailable rather than appearing as zero.

## Reading the timings

CPU phase times come from `Engine.Debug.getLastPerformanceMetrics()`. GPU pass
times come from `Engine.Debug.getGpuTimings()`, verified against the running
Direct3D12 game: it returns `{passName = milliseconds}`. CPU work, GPU total and
GPU wait counters use the game's `$fps::cpuTime`, `$fps::gpuTime` and
`$fps::waitForGPU` registry values. Memory counters are never treated as timings.
Unsupported values are missing, not zero. GPU pass panels disappear if the API or
renderer cannot supply them.

GPU queries are asynchronous. CPU and GPU charts share **observation order**;
the API does not provide the exact CPU-frame identity of the completed GPU query.
They are separate parallel lanes, never added together. Earth scopes are nested
within CPU Lua work and are shown separately as well. Unattributed time is labeled;
reported durations that exceed an interval are retained and flagged, not rescaled.
The graph hides only sub-pixel rectangles; raw durations remain available in the
selected-frame list, legend and export.

Wall execution time includes I/O, locks and time waiting for other workers. Queue
time ends when the function starts in its worker, so process startup and task
serialization are included. CPU time comes from `time.thread_time()` in the actual
executing thread; work delegated to other threads or processes has separate rows.
Parallel jobs and inclusive nested steps overlap. Their times must not be added
as though they were sequential pieces of one frame.

Stage scopes cover elevation sampling, imagery rendering and assembly, terrain
generation, grading, surface data and paint, building plans/site selection/mesh
assembly, road meshes, geometry assembly/serialization and native shape encoding.
Explicit server thread executors and respawning process pools are instrumented,
including thread executors inside process jobs. Remote helper computation is
represented by the local peer I/O job's wait; its remote CPU time is not measured.
Event-loop/default-executor work, maintenance threads and uninstrumented function
bodies are not individually timed. Game frame wall time includes engine, physics,
rendering and system work beyond the mod's stage timers.

Workload rows are driven by observed activity, with a five-second window; there
are no mandatory building-generator panels or imports. Missing optional systems
never create placeholder rows. Their older recorded timings remain in the bounded
raw export, while their inactive live rows disappear.

This is diagnostic instrumentation and has measurement overhead. No detailed jobs
or frames are collected before opting in. Turning it off stops new root-job
tracing; already traced jobs can finish emitting bounded metadata, which is
discarded after the capture session changes. The manager starts on first use and
remains idle until server shutdown. The diagnostic sampler sleeps without jobs.

## Transport and bounds

`hello.runtimeProfiler = 2` advertises modes, I/O and RTT support. The mod requests
`{type: 'runtime_profiler', enabled: true, mode: 'essential'|'full', probe: integer}`
every 1000 ms in Essential or 500 ms in Full while enabled in the Earth level.
The server immediately acknowledges probes with `runtime_profiler_pong` and
publishes `{type: 'runtime_profiler', profile: ...}` once per second in Essential
or twice per second in Full. A version-1 server supports Full worker telemetry
only; Essential leaves that tracing off. With multiple subscribers, the server
uses Full if any subscriber requests it. Essential clients receive reduced data,
and the window explains when another client keeps Full collection active.
Subscriptions expire after three seconds without renewal; collection stops when
no subscribers remain. Snapshot queue RPCs run outside the event loop, and updates
are skipped for clients with more than 256 KiB already buffered for writing.

Spawned jobs retain ordinary Future results, exceptions, cancellation and worker
admission rules. A bounded manager queue carries telemetry; full queues drop
metadata instead of waiting for queue space. History retains 512 in-flight records,
120 completed jobs and 64 named scopes per job. Current stages are sampled every
100 ms per interpreter. Missing end events, crashes and cancelled futures become
incomplete records rather than permanently busy workers. Orphaned nested records
with no telemetry for three seconds are marked unknown. Incomplete records are
excluded from execution-time statistics.

To instrument another semantic step, import `step` from `runtimeprofiler` and use
`@step('Descriptive stage name')`. Decorators preserve public function names so
spawned-process pickling continues to work. Prefer useful phase boundaries over
per-vertex timers.

## Validation

Run `python -B tools/check_runtime_profiler.py` with the server dependencies and
the existing optional LuaJIT-backed `lupa` runtime available. This covers threads,
spawned processes, nested worker threads, queue waits, original results/errors,
cancellation, process crashes, limits, subscription expiry/backpressure, protocol
agreement, settings persistence and stubbed game-loop integration.

The isolated profiler check at dev `50e41cac` ran 180 Python cases (174 passed,
6 optional skips) and passed 814 Lua assertions. The later combined check at dev
`11cc5557` ran 1,804 Python cases across 136 selected modules (1,794 passed,
10 optional skips), with no failures or errors. Matching CLI startup and both
profiler modes passed in legacy and v2 configurations. These runs overlap and
should not be added together as unique coverage.

On Windows, two worker tests encounter temporary-file cleanup errors from open
revisit-cache sidecars, also reproduced on untouched upstream at `50e41cac`.
All five worker tests pass with a test-only cleanup adapter that closes those
handles before deleting temporary folders; production code is unchanged.

Native BeamNG acceptance is still required: verify window sizing/readability,
live generation activity during a drive and teleport, closing/reopening, export,
and profiling overhead with the setting on and off. Offline checks do not establish
renderer correctness or an FPS improvement. The existing Ctrl+Shift+Y report
capture remains available separately.

## Independent upstream build

The profiler branch targets original upstream `TecnaGamer/BeamNG.Earth` dev
`11cc5557` (fetched 2 November 2026). It contains neither `server/buildgen` nor
`server/sitegen`. Its patch adds profiler files, timing decorators, thread/process
and I/O instrumentation, opt-in settings and tests. The world-generation branch
builds on it separately. The performance branch is independent of both.
