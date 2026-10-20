"""Run the offline urban regressions and neighboring generator suites.

Run from the repository root. No game or download server is started.
"""
import argparse
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "tests"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))

SUITES = [
    "test_heightmap", "test_roads", "test_surface", "test_roadmesh", "test_buildings",
    "test_navgraph", "test_vegetation", "test_seatiles", "test_seacarve",
    "test_european_traffic", "test_building_fidelity", "test_urban_frontages",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true", help="require and test the native kernels")
    parser.add_argument("--report", type=Path, default=ROOT / "dist" / "urban-validation.json")
    args = parser.parse_args()
    if not args.native:
        # The repository includes native binaries. Simulate their absence before
        # any server module imports them, including geodesy and image helpers.
        sys.modules["earthfast"] = None
    import fast

    if args.native and not fast.AVAILABLE:
        parser.error("native kernels unavailable; run python tools/build_native.py first")
    names = SUITES + (["test_fast"] if args.native else [])
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    report = {
        "native": fast.AVAILABLE,
        "tests": result.testsRun,
        "failures": [case.id() for case, _ in result.failures],
        "errors": [case.id() for case, _ in result.errors],
        "skipped": [{"test": case.id(), "reason": reason} for case, reason in result.skipped],
        "scope": "offline generator tests; no gameplay, rendering or FPS validation",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    sys.exit(main())
