"""Isolated native-thread stress diagnostics for #687; never starts a world server.

python tools/stress_native.py --out build/native-stress --seconds 60 --workers 12 --cycles 3
Use a NEW output directory. --gpu explicitly includes the GPU BC1 path.
"""
import argparse
import concurrent.futures
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def supervise(command, folder, timeout, env=None):
    """Keep fatal stderr and fail on crashes, timeouts or a missing result."""
    folder.mkdir()
    with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env, cwd=ROOT)
        timed_out = False
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            code = child.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    result_file = folder / "result.json"
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = None
    ok = code == 0 and not timed_out and isinstance(result, dict) and result.get("ok") is True
    return {"ok": ok, "returncode": code, "timed_out": timed_out, "result": result}


def child_run(args):
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(max(1, args.seconds + 5), repeat=False)
    sys.setswitchinterval(.0001)
    sys.path.insert(0, str(ROOT / "server"))
    import numpy as np
    import fast
    fast.require(fast.MIN_VERSION, "native thread stress")
    ef = fast._ef
    folder = Path(args.child)
    # Each worker owns its inputs. No Python or NumPy mutation races are
    # introduced to manufacture a native failure.
    states = [{} for _ in range(args.workers)]
    states_lock = threading.Lock()
    stop_reporter = threading.Event()
    start = threading.Barrier(args.workers)
    gpu = ef.gpu_name() if args.gpu else None
    if args.gpu and gpu is None:
        raise RuntimeError("--gpu requested but the native module found no usable GPU")
    metadata = {"python": platform.python_version(), "platform": platform.platform(),
                "numpy": np.__version__, "native_version": ef.VERSION,
                "native_sha256": hashlib.sha256(Path(ef.__file__).read_bytes()).hexdigest(),
                "workers": args.workers, "seconds": args.seconds, "gpu": gpu}
    save(folder / "metadata.json", metadata)

    def progress():
        while not stop_reporter.wait(1):
            with states_lock:
                snapshot = [dict(s) for s in states]
            save(folder / "progress.json", snapshot)

    def worker(index):
        # Analytic planar fields, a byte-exact raster round trip, an identity
        # frame transform, and deterministic BC1 output from a fixed image.
        y, x = np.mgrid[:16, :16].astype(np.float64)
        a, b = np.ascontiguousarray(2*x + 3*y), np.ascontiguousarray(x - 4*y)
        px = np.linspace(-1, 16, 4096)
        py = px[::-1].copy()
        raster = np.arange(32768, dtype=np.float32) / 8
        points = np.column_stack((px, py, px*2))
        image = np.arange(64*64*3, dtype=np.uint8).reshape(64, 64, 3)
        expected_bc1 = bytes(ef.bc1_encode(image))
        expected_gpu = ef.bc1_encode_gpu([image])[0] if args.gpu else None
        counts = {"grid": 0, "raster": 0, "frame": 0, "bc1": 0}
        if args.gpu:
            counts["gpu_bc1"] = 0
        thread = hex(threading.get_ident())

        def mark(name):
            with states_lock:
                states[index] = {"thread": thread, "active": name, "counts": dict(counts)}

        start.wait(timeout=30)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            mark("grid_bilinear2")
            av, bv = ef.grid_bilinear2(a, b, 0., 0., 1., px, py)
            np.testing.assert_allclose(av, 2*px + 3*py, atol=1e-12, rtol=1e-12)
            np.testing.assert_allclose(bv, px - 4*py, atol=1e-12, rtol=1e-12)
            counts["grid"] += 1
            mark("f32_pack / f32_unpack")
            unpacked = ef.f32_unpack(ef.f32_pack(raster))
            if unpacked.tobytes() != raster.tobytes():
                raise AssertionError("native raster round trip changed bytes")
            counts["raster"] += 1
            mark("geo_world_map")
            if ef.geo_world_map(48., 2., 48., 2., points).tobytes() != points.tobytes():
                raise AssertionError("native identity frame changed points")
            counts["frame"] += 1
            mark("bc1_encode")
            if bytes(ef.bc1_encode(image)) != expected_bc1:
                raise AssertionError("CPU BC1 output changed under concurrency")
            counts["bc1"] += 1
            if args.gpu:
                mark("bc1_encode_gpu")
                if ef.bc1_encode_gpu([image])[0] != expected_gpu:
                    raise AssertionError("GPU BC1 output changed under concurrency")
                counts["gpu_bc1"] += 1
        mark("finished")
        return counts

    reporter = threading.Thread(target=progress, daemon=True)
    reporter.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            counts = list(pool.map(worker, range(args.workers)))
        if any(not all(c.values()) for c in counts):
            raise RuntimeError("a worker completed no checked workload")
        save(folder / "result.json", {"ok": True, "metadata": metadata, "counts": counts})
    finally:
        stop_reporter.set()
        reporter.join(timeout=5)
        with states_lock:
            snapshot = [dict(s) for s in states]
        save(folder / "progress.json", snapshot)
        faulthandler.cancel_dump_traceback_later()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not (0 < args.seconds <= 86400 and 1 <= args.workers <= 128 and 1 <= args.cycles <= 100):
        parser.error("seconds must be 0..86400, workers 1..128, cycles 1..100")
    if args.child:
        child_run(args)
        return 0
    if args.out is None:
        parser.error("--out must name a new output directory")
    # Refuse an existing path, including symlinks. Never touch the game cache.
    args.out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, BNGE_GPU="1" if args.gpu else "0", PYTHONFAULTHANDLER="1")
    results = []
    save(args.out / "report.json", {"ok": False, "complete": False, "cycles": []})
    for cycle in range(args.cycles):
        folder = args.out.resolve() / f"cycle-{cycle+1}"
        command = [sys.executable, str(Path(__file__).resolve()), "--child", str(folder),
                   "--seconds", str(args.seconds), "--workers", str(args.workers)]
        if args.gpu:
            command.append("--gpu")
        result = supervise(command, folder, args.seconds + 45, env)
        results.append(result)
        save(args.out / "report.json", {"ok": len(results) == args.cycles and all(r["ok"] for r in results),
             "complete": len(results) == args.cycles, "cycles": results})
        print(f"cycle {cycle+1}: {'OK' if result['ok'] else 'FAILED'}; logs: {folder}", flush=True)
        if not result["ok"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
