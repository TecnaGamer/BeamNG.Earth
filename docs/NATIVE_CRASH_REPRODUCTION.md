# Native thread crash investigation (#687)

The report contains `PyEval_SaveThread` with no current thread state after a
long Windows drive. The full crashing-thread stack and extension list were
lost. The launcher now retains two previous logs; that part is already fixed.
There is no established native call site or reproduction of the fatal error.

`tools/stress_native.py` supplies an isolated, bounded workload and durable
crash evidence. It does not start BeamNG, connect to a server, use downloaded
map data or read/write the player's cache. It does not change runtime code.

After building the required native module:

```text
python tools/stress_native.py --out build/native-stress-cpu --seconds 60 --workers 12 --cycles 3
python tools/stress_native.py --out build/native-stress-gpu --seconds 60 --workers 12 --cycles 3 --gpu
python -m unittest discover -s server/tests -p test_native_stress.py
```

Use a new output directory for each run. Existing paths are refused.
CPU mode explicitly disables the GPU. `--gpu` requests the GPU BC1 path and
fails if no GPU is available; it is not a silent CPU fallback.

Each cycle starts a fresh Python process and runs independent worker-owned
inputs through grid interpolation, raster compression/decompression, a frame
transform and CPU BC1 encoding. GPU mode adds GPU BC1 encoding. Analytic grid
values, round-trip bytes, identity coordinates and repeated encoding bytes
are checked after every call. This checks concurrency consistency; it does
not replace each kernel's independent correctness tests.

The output contains:

- `report.json`: completion status, exit codes, timeout flags and checked-call counts;
- per-cycle `stderr.log`: Python fatal-error dumps for all threads, or ordinary tracebacks;
- `metadata.json`: Python/NumPy/native versions, the loaded binary's SHA-256 and GPU identity;
- `progress.json`: a bounded once-per-second snapshot of thread IDs, current calls and completed counts;
- `result.json`: written only after every worker finishes and has checked every workload.

The parent enforces a deadline with initialization/cleanup allowance and
kills/reaps its own child on timeout or interruption. A fatal exit, timeout,
missing/corrupt result or incomplete cycle set cannot pass. It does not
terminate unrelated processes. Failed runs retain their diagnostic files.

A successful stress run means only that this workload completed on the
reported binary and machine. It does not establish that #687 is fixed, cover
every native kernel, or replace the original long-drive/teleport test.
When the original crash happens again, attach `server.log.1` before another
two launches rotate it away, together with this tool's report if it reproduces.

Local validation on Windows x64 / Python 3.10.11 / NumPy 2.2.3, native 158
built in Release with MSVC 14.44: three fresh CPU processes (12 workers,
30 seconds each) completed 38,476 checked rounds in total. Two fresh GPU
processes (four workers, 10 seconds each) completed 17,431 checked rounds
with a GTX 1660 SUPER using Vulkan. Every round exercised all selected
workloads. Six supervisor regressions passed, including fatal exit, missing
and corrupt results, timeout and preservation of existing output.
The original fatal error was not reproduced. These are reliability checks,
not gameplay or performance measurements.
