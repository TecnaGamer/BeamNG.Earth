#!/usr/bin/env python3
"""Build the world server's Rust kernels (native/earthfast) and put the module next to the server:
server/earthfast.abi3.so (Linux) or server/earthfast.pyd (Windows). One build works for every Python from 3.10
(the stable ABI). Needs Rust (https://rustup.rs). The server requires this module at fast.MIN_VERSION or newer.

    python3 tools/build_native.py              # this platform
    python3 tools/build_native.py --windows    # server/earthfast.pyd from Linux: cargo-zigbuild, zig on PATH,
                                               # rustup target add x86_64-pc-windows-gnu
"""
import glob


def install(src, dst):
    """Copy src to dst through a new file renamed into place: a server running with the old module keeps its
    mapping (copying over a loaded .so rewrote it under the running server: a segfault, 2026-10-15)."""
    tmp = dst + ".new"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRATE = os.path.join(ROOT, "native", "earthfast")


WIN_TARGET = "x86_64-pc-windows-gnu"
LINUX_TARGET = "x86_64-unknown-linux-gnu"
LINUX_GLIBC = "2.35"           # the oldest glibc the Linux module runs on (the AppImage's own limit too)


def windows():
    """Cross-build the Windows module. pyo3 makes python3.dll's import library as python3.dll.a; zig's linker
    looks for libpython3.a, so the first build fails at the link, gets a copy and builds again."""
    cmd = ["cargo", "zigbuild", "--release", "--target", WIN_TARGET]
    if subprocess.run(cmd, cwd=CRATE).returncode != 0:
        libs = glob.glob(os.path.join(CRATE, "target", WIN_TARGET, "release", "build", "pyo3-ffi-*", "out", "lib"))
        for d in libs:
            if os.path.isfile(os.path.join(d, "python3.dll.a")):
                shutil.copyfile(os.path.join(d, "python3.dll.a"), os.path.join(d, "libpython3.a"))
        subprocess.run(cmd, cwd=CRATE, check=True)
    dst = os.path.join(ROOT, "server", "earthfast.pyd")
    install(os.path.join(CRATE, "target", WIN_TARGET, "release", "earthfast.dll"), dst)
    print("built", dst)


def main():
    if "--windows" in sys.argv[1:]:
        windows()
        return
    if sys.platform.startswith("linux") and shutil.which("cargo-zigbuild") and shutil.which("zig"):
        # against glibc LINUX_GLIBC (cargo-zigbuild), not this machine's: built on a rolling distro, the module asked
        # for glibc 2.43 / 2.44 and failed to load on a Steam Deck (2.43 without the 2.44 symbols) and would on every
        # older system (Ubuntu 22.04: 2.35), 2026-10-22
        subprocess.run(["cargo", "zigbuild", "--release", "--target", f"{LINUX_TARGET}.{LINUX_GLIBC}"], cwd=CRATE,
                       check=True)
        install(os.path.join(CRATE, "target", LINUX_TARGET, "release", "libearthfast.so"),
                os.path.join(ROOT, "server", "earthfast.abi3.so"))
        print("built", os.path.join(ROOT, "server", "earthfast.abi3.so"), f"(glibc {LINUX_GLIBC})")
        return
    subprocess.run(["cargo", "build", "--release"], cwd=CRATE, check=True)
    rel = os.path.join(CRATE, "target", "release")
    if sys.platform == "win32":
        src, dst = os.path.join(rel, "earthfast.dll"), os.path.join(ROOT, "server", "earthfast.pyd")
    elif sys.platform == "darwin":
        src, dst = os.path.join(rel, "libearthfast.dylib"), os.path.join(ROOT, "server", "earthfast.abi3.so")
    else:
        src, dst = os.path.join(rel, "libearthfast.so"), os.path.join(ROOT, "server", "earthfast.abi3.so")
    install(src, dst)
    print("built", dst)


if __name__ == "__main__":
    main()
