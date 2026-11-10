"""Convert PR #505's final archives into upstream's regionaldata layout, outside git.

This preserves the final checked French direction routes without putting the large
ZIPs back into the app/repository. The result can also be published to the data
host with the existing regional-data publication workflow.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import build_regional_data

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "eb73b0f7f02b1ddc42664d5da7ffcd095469daa2"
ARCHIVES = ("country_bounds.zip", "quebec_bounds.zip", "europe_places.zip",
            "fr_local_directions.zip", "fr_priority_directions-01.zip",
            "fr_priority_directions-02.zip")
OUT = ROOT / "build" / "regional-pr505-eb73b0f7"


def prepare(out=OUT):
    out = Path(out).resolve()
    stamp = out / "PR505_SOURCE.json"
    if (out / "manifest.json").is_file() and stamp.is_file():
        if json.loads(stamp.read_text())["source"] == SOURCE:
            print("Regional data already prepared:", out, flush=True)
            return out
    if out.exists():
        raise RuntimeError(f"Output already exists without the expected source marker: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pr505-data-", dir=out.parent) as tmp:
        src, converted = Path(tmp) / "archives", Path(tmp) / "converted"
        src.mkdir()
        for name in ARCHIVES:
            print("Reading original PR archive:", name, flush=True)
            with (src / name).open("wb") as fh:
                subprocess.run(["git", "show", f"{SOURCE}:server/data/{name}"],
                               cwd=ROOT, stdout=fh, check=True)
        build_regional_data.main(str(src), str(converted))
        (converted / "PR505_SOURCE.json").write_text(json.dumps({"source": SOURCE}) + "\n")
        shutil.move(str(converted), str(out))
    print("Regional data ready:", out, flush=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, default=OUT)
    args = ap.parse_args()
    prepare(args.output)
