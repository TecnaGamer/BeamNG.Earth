"""Give each offline geotile build its own writable provider data on disk, and take it away again.

A copy is several GB (the providers' data). TemporaryDirectory removes it only when its test ends normally: a worker the
test runner killed at its timeout, or a removal Windows refused while a child process still held a file, left it on disk
(47 GB of geotile-* copies under ~/bnge-tests, the disk full mid-run, 2026-11-27). So each copy names its owner process
(OWNER), every new one first removes the copies whose owner is gone (sweep), and its own removal is retried while Windows
releases the files."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


SOURCE = Path(os.environ.get("BNGE_GEO_TEST_SOURCE") or Path.home() / ".cache" / "beamng-earth")
# Provider inputs consumed by the Fells Point fixtures. Worker output starts empty in each copy.
PROVIDERS = ("dem", "mapdata", "mapdata-mirror", "roadz", "airports", "fra")
PREFIX = "geotile-"
OWNER = ".owner"            # the copy's owner process id, in the copy
ORPHAN_S = 3600.0           # s: a copy with no owner file this old is an orphan (a copy made before owners were named)
REMOVE_TRIES = 10           # removals tried, REMOVE_WAIT_S apart, while Windows holds a file
REMOVE_WAIT_S = 0.5


def _alive(pid):
    """Whether process pid runs (on Windows not by os.kill: signal 0 there terminates the process)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))       # (PROCESS_QUERY_LIMITED_INFORMATION)
        if not h:
            return ctypes.get_last_error() == 5             # (access denied: it runs, as someone else)
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259    # (STILL_ACTIVE)
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove(path):
    """Remove the tree at path, retried while files are held; whether it is gone."""
    for _ in range(REMOVE_TRIES):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(REMOVE_WAIT_S)
    return False


def sweep(disk):
    """Remove the copies under disk whose owner process is gone: [the paths removed]."""
    gone = []
    for p in Path(disk).glob(PREFIX + "*"):
        if not p.is_dir():
            continue
        try:
            pid = int((p / OWNER).read_text().strip())
            orphan = not _alive(pid)
        except (OSError, ValueError):
            try:
                orphan = time.time() - p.stat().st_mtime > ORPHAN_S
            except OSError:
                continue
        if orphan and _remove(p):
            gone.append(p)
    return gone


@contextmanager
def work_cache():
    disk = Path(os.environ.get("BNGE_GEO_TEST_TMP") or Path.home() / "bnge-tests")
    disk.mkdir(parents=True, exist_ok=True)
    sweep(disk)
    tmp = tempfile.mkdtemp(prefix=PREFIX, dir=disk)
    try:
        (Path(tmp) / OWNER).write_text(str(os.getpid()))
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
    finally:
        _remove(tmp)        # (held past the tries: the next work_cache's sweep takes it, its owner gone)
