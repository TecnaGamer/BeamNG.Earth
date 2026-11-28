#!/usr/bin/env python3
"""Build the live weather sounds' synth (native/weathersynth, feature `audio`) for the app bundle:
build/weathersynth/weathersynth (Linux, glibc 2.35 or older, libasound.so.2 linked dynamically) or
build/weathersynth/weathersynth.exe (Windows). tools/build_app.py bundles it where server/liveaudio.py finds it (next
to the frozen app's modules, sys._MEIPASS).

    python3 tools/build_weathersynth.py            # this platform
    python3 tools/build_weathersynth.py --check    # also check the result (the GLIBC versions it needs on Linux)

The crates come from crates.io at the exact versions and checksums in native/weathersynth/Cargo.lock (cargo fetch
--locked for this target only: cpal's other-platform dependencies are never downloaded). A machine whose glibc is newer
than 2.35 builds through cargo-zigbuild (zig on PATH) like tools/build_native.py; the CI runner (ubuntu-22.04) is 2.35
itself. Linux needs alsa-lib's development files (libasound2-dev / alsa-lib).
"""
import argparse
import os
import platform
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRATE = os.path.join(ROOT, "native", "weathersynth")
OUT = os.path.join(ROOT, "build", "weathersynth")
IS_WIN = os.name == "nt"
LINUX_TARGET = "x86_64-unknown-linux-gnu"
WIN_TARGET = "x86_64-pc-windows-msvc"
LINUX_GLIBC = (2, 35)


def glibc():
    v = platform.libc_ver()[1]
    m = re.match(r"(\d+)\.(\d+)", v or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def run(cmd, env=None):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=CRATE, env=env)


def max_glibc(path):
    out = subprocess.run(["objdump", "-T", path], check=True, capture_output=True, text=True).stdout
    vs = {tuple(int(x) for x in v.split(".")) for v in re.findall(r"GLIBC_(\d+\.\d+(?:\.\d+)?)", out)}
    return max(vs) if vs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="check the built binary (Linux: GLIBC <= 2.35, libasound.so.2)")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    env = dict(os.environ)
    if IS_WIN:
        target, exe = WIN_TARGET, "weathersynth.exe"
        run(["cargo", "fetch", "--locked", "--target", target])
        run(["cargo", "build", "--release", "--locked", "--features", "audio", "--target", target])
        built = os.path.join(CRATE, "target", target, "release", exe)
    else:
        target, exe = LINUX_TARGET, "weathersynth"
        run(["cargo", "fetch", "--locked", "--target", target])
        g = glibc()
        if g is not None and g <= LINUX_GLIBC:
            run(["cargo", "build", "--release", "--locked", "--features", "audio", "--target", target])
        else:
            if not shutil.which("cargo-zigbuild") or not shutil.which("zig"):
                sys.exit(f"glibc {g} is newer than {LINUX_GLIBC}: cargo-zigbuild and zig are needed")
            # (the build machine's own libasound may need a newer glibc than the 2.35 stubs: the binary does not)
            env["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"] = "-C link-arg=-Wl,--allow-shlib-undefined"
            run(["cargo", "zigbuild", "--release", "--locked", "--features", "audio",
                 "--target", f"{target}.{LINUX_GLIBC[0]}.{LINUX_GLIBC[1]}"], env=env)
        built = os.path.join(CRATE, "target", target, "release", exe)
    dst = os.path.join(OUT, exe)
    tmp = dst + ".new"
    shutil.copy2(built, tmp)
    os.replace(tmp, dst)
    if not IS_WIN:
        os.chmod(dst, 0o755)
    print(f"built {dst} ({os.path.getsize(dst) / 1e6:.1f} MB)")
    if a.check and not IS_WIN:
        g = max_glibc(dst)
        if g is None or g > LINUX_GLIBC:
            sys.exit(f"{dst} needs GLIBC_{'.'.join(map(str, g or ()))}: over {LINUX_GLIBC}")
        needed = subprocess.run(["readelf", "-d", dst], check=True, capture_output=True, text=True).stdout
        if "libasound.so.2" not in needed:
            sys.exit(f"{dst} does not link libasound.so.2 dynamically")
        print(f"checked: GLIBC_{'.'.join(map(str, g))} at most, libasound.so.2 dynamic")


if __name__ == "__main__":
    main()
