"""Give each offline geotile build its own writable provider data on disk."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SOURCE = Path(os.environ.get("BNGE_GEO_TEST_SOURCE") or Path.home() / ".cache" / "beamng-earth")
# Provider inputs consumed by the Fells Point fixtures. Worker output starts empty in each copy.
PROVIDERS = ("dem", "mapdata", "mapdata-mirror", "roadz", "airports", "fra")


@contextmanager
def work_cache():
    disk = Path(os.environ.get("BNGE_GEO_TEST_TMP") or Path.home() / "bnge-tests")
    disk.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="geotile-", dir=disk) as tmp:
        for name in PROVIDERS:
            src = SOURCE / name
            if not src.is_dir():
                continue
            dst = Path(tmp) / name
            if src.is_symlink():
                raise ValueError(f"linked geotile input: {src}")
            if sys.platform.startswith("linux"):
                subprocess.run(["cp", "-R", "--reflink=auto", "--", src, dst], check=True)
            else:
                shutil.copytree(src, dst, symlinks=True)
            # cp -R preserves links. Refuse the private copy before a worker can follow one.
            if any(p.is_symlink() for p in dst.rglob("*")):
                raise ValueError(f"linked file inside geotile input: {src}")
        yield tmp
