#!/usr/bin/env python3
"""Reproduce the cache-fill/lock-acquisition race without contacting providers.

python tools/benchmark_range_cache.py --baseline-ref baseline/modernization-20261102

The producer completes its HTTP read immediately before a second worker acquires
the same block locks. This deliberately forces a race; it is not an estimate of
its frequency during gameplay or of real-world loading speed.
"""
import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import netfetch
import rangeread


class LocalTransport:
    def __init__(self, data, latency):
        self.data, self.latency = data, latency
        self.requests = self.bytes = 0

    def miss_alive(self, *args):
        return False

    def get(self, url, *, kind, headers):
        start, last = map(int, headers["Range"][6:].split("-"))
        end = min(last + 1, len(self.data))
        time.sleep(self.latency)
        body = self.data[start:end]
        self.requests += 1
        self.bytes += len(body)
        return netfetch.Response(206, body, {"content-range": f"bytes {start}-{end - 1}/{len(self.data)}"})


def measure(module, repeats, latency):
    data = bytes(range(256)) * (16 * 1024)    # 4 MiB, sixteen default blocks
    elapsed, requests, transferred = [], [], []
    for _ in range(repeats):
        module.clear_memory()
        with tempfile.TemporaryDirectory(prefix="earth-range-bench-") as folder:
            transport = LocalTransport(data, latency)
            reader = module.RemoteFile("https://example.invalid/data", len(data), folder, transport)
            producer = module.RemoteFile("https://example.invalid/data", len(data), folder, transport)
            take = module.xproc.take
            pending = True

            def race(path, kind):
                nonlocal pending
                if pending:
                    pending = False
                    assert producer.read(0, len(data)) == data
                return take(path, kind)

            t0 = time.perf_counter()
            with mock.patch.object(module.xproc, "take", side_effect=race):
                assert reader.read(0, len(data)) == data
            elapsed.append((time.perf_counter() - t0) * 1000)
            requests.append(transport.requests)
            transferred.append(transport.bytes)
            assert not list(Path(folder).glob("*.lock"))
    module.clear_memory()
    return {"median_ms": round(statistics.median(elapsed), 3),
            "requests_per_iteration": statistics.mean(requests),
            "bytes_per_iteration": statistics.mean(transferred), "samples_ms": [round(x, 3) for x in elapsed]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--latency-ms", type=float, default=30)
    args = parser.parse_args()
    if args.repeats < 1 or args.latency_ms < 0:
        parser.error("repeats must be positive and latency must be nonnegative")
    result = {"scenario": "forced cache-fill race; synthetic transport, no external network",
              "python": platform.python_version(), "platform": platform.platform(),
              "processor": platform.processor(), "repeats": args.repeats, "latency_ms": args.latency_ms}
    if args.baseline_ref:
        source = subprocess.check_output(["git", "show", args.baseline_ref + ":server/rangeread.py"],
                                         cwd=ROOT, text=True, encoding="utf-8")
        baseline = types.ModuleType("baseline_rangeread")
        exec(compile(source, "baseline_rangeread.py", "exec"), baseline.__dict__)
        result["baseline_ref"] = args.baseline_ref
        result["baseline"] = measure(baseline, args.repeats, args.latency_ms / 1000)
    result["current"] = measure(rangeread, args.repeats, args.latency_ms / 1000)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
