"""lanecheck.py's build worker: tools/widecorpus/build.py of another checkout (a git worktree: one code state), run
on any OS (build.py imports the Unix-only pwd module for its command line only).

    python lanecheck_build.py TREE PLACE_JSON CACHE OUT RADIUS OFFLINE(0|1)
"""
import json
from pathlib import Path
import sys
import types

if __name__ == "__main__":
    tree = Path(sys.argv[1]).resolve()
    here = Path(__file__).resolve().parent
    # (this checkout's tools stay out of the way: build.py and the server come from TREE)
    sys.path = [p for p in sys.path if p and Path(p).resolve() != here]
    sys.path[:0] = [str(tree / "tools" / "widecorpus"), str(tree / "server"), str(tree / "tools")]
    sys.modules.setdefault("pwd", types.ModuleType("pwd"))
    import build
    if Path(build.__file__).resolve().parent != tree / "tools" / "widecorpus":
        raise SystemExit(f"build.py came from {build.__file__}, not {tree}")
    build.build(json.loads(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), float(sys.argv[5]), sys.argv[6] == "1")
