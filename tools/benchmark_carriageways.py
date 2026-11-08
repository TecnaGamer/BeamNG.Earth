#!/usr/bin/env python3
"""Repeatable offline solver workloads. This does not measure game FPS or real-world reconstruction accuracy.

python tools/benchmark_carriageways.py --output build/solver.json
python tools/benchmark_carriageways.py --server-root /path/to/baseline --output build/baseline.json
"""
import argparse
import json
import inspect
from pathlib import Path
import statistics
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--cases", nargs="*")
    args = parser.parse_args()
    if args.repeat < 1: parser.error("--repeat must be positive")
    sys.path[:0] = [str(args.server_root.resolve() / "server"), str(ROOT / "server/tests")]
    import carriageways as cw
    from carriageway_cases import cases, residuals
    supports_diagnostics = "diagnostics" in inspect.signature(cw.solve).parameters
    results = []
    workloads = cases()
    if args.cases and set(args.cases) - set(workloads): parser.error("unknown case")
    for name, (roads, couplings) in workloads.items():
        if args.cases and name not in args.cases: continue
        times = []
        diagnostics = {}
        options = {"diagnostics": diagnostics} if supports_diagnostics else {}
        for _ in range(args.repeat):
            start = time.perf_counter()
            profiles = cw.solve(roads, couplings, **options)
            times.append(time.perf_counter() - start)
        result = dict(case=name, chains=len(roads), samples=sum(len(c["t"]) for c in roads),
                      couplings=len(couplings), median_s=statistics.median(times), repeats=args.repeat,
                      timings_s=times,
                      residuals=residuals(roads, couplings, profiles),
                      finite=all(bool(np.isfinite(z).all()) for z in profiles))
        if diagnostics:
            result["diagnostics"] = diagnostics
        results.append(result)
        print(json.dumps(result), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"python": sys.version.split()[0], "workloads": "synthetic-v1", "results": results},
                                    indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__": main()
