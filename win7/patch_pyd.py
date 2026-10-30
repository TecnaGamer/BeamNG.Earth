"""Windows 7 / Vista: make Rust-built extension modules (server/earthfast.pyd) loadable.

Those modules import functions that only exist on newer Windows:
    bcryptprimitives.dll              ProcessPrng                                   (Windows 10)
    api-ms-win-core-synch-l1-2-0.dll  WaitOnAddress, WakeByAddressSingle/All        (Windows 8)
    api-ms-win-core-winrt-error-*.dll Get/SetRestrictedErrorInfo, RoOriginateError* (Windows 8)
This script rewrites the DLL names in the .pyd's import table so they point at stand-in DLLs shipped in win7/:
    efw7shim.dll  (source: efw7shim.c)   ProcessPrng, WaitOnAddress, WakeByAddressSingle/All
    efw7err.dll   (source: error.c, by EAZY BLACK, AGPL-3.0-or-later; see error.def)   the winrt error functions
It is idempotent and safe to run before every start; a release update that brings a fresh .pyd is simply
patched again. The original is kept as <name>.orig. On Windows it then checks that every DLL and function
the module imports can really be resolved, and prints a WARNING line for each one that cannot (send me those).

    python win7/patch_pyd.py            patch every .pyd of the BeamNG.Earth folder (not python/)
    python win7/patch_pyd.py FILE...    patch the given files
    python win7/patch_pyd.py DIR...     patch every .exe / .dll / .pyd found under the given folders
                                        (used by win7/patch_rust.bat for a Rust toolchain: rustc.exe, cargo.exe,
                                        rustc_driver-*.dll ...). Folders are only reported when something changed.
"""
import os
import re
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHIMS = ("efw7shim.dll", "efw7err.dll")

# (regex on the lower-case import name) -> stand-in DLL
RULES = [
    (re.compile(r"^bcryptprimitives\.dll$"), "efw7shim.dll"),
    (re.compile(r"^api-ms-win-core-synch-l1-2-\d\.dll$"), "efw7shim.dll"),
    (re.compile(r"^api-ms-(win-)?core-winrt-error-l\d-\d-\d\.dll$"), "efw7err.dll"),
]


def shim_for(name):
    low = name.lower()
    for rx, shim in RULES:
        if rx.match(low):
            return shim
    return None


def rva_to_off(sections, rva):
    for va, vsize, raw, rsize in sections:
        if va <= rva < va + max(vsize, rsize):
            return rva - va + raw
    return None


def parse(data):
    """-> (is64, data_directory_offset, sections, import_descriptor_offset) or an error string."""
    if data[:2] != b"MZ":
        return "not a PE file"
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return "not a PE file"
    nsec, = struct.unpack_from("<H", data, pe + 6)
    optsize, = struct.unpack_from("<H", data, pe + 20)
    opt = pe + 24
    magic, = struct.unpack_from("<H", data, opt)
    is64 = magic == 0x20B
    dd = opt + (112 if is64 else 96)                     # data directories
    imp_rva, _ = struct.unpack_from("<II", data, dd + 8)
    if not imp_rva:
        return "no imports"
    sec = opt + optsize
    sections = []
    for i in range(nsec):
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, sec + 40 * i + 8)
        sections.append((va, vsize, raw, rsize))
    return is64, dd, sections, rva_to_off(sections, imp_rva)


def cstr(data, off):
    return bytes(data[off:data.index(b"\0", off)]).decode("ascii", "replace")


def read_imports(data):
    """-> [(dll name, [function names / '#ordinal'])] of a PE image."""
    info = parse(data)
    if isinstance(info, str):
        return []
    is64, _dd, sections, off = info
    out = []
    while off is not None:
        oft, _ts, _fc, name_rva, ft = struct.unpack_from("<IIIII", data, off)
        if not name_rva and not oft:
            break
        funcs = []
        t = rva_to_off(sections, oft or ft)
        step = 8 if is64 else 4
        flag = 1 << (63 if is64 else 31)
        while t is not None:
            v, = struct.unpack_from("<Q" if is64 else "<I", data, t)
            if not v:
                break
            if v & flag:
                funcs.append("#%d" % (v & 0xFFFF))
            else:
                funcs.append(cstr(data, rva_to_off(sections, v & 0x7FFFFFFF) + 2))
            t += step
        out.append((cstr(data, rva_to_off(sections, name_rva)), funcs))
        off += 20
    return out


def patch(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())
    info = parse(data)
    if isinstance(info, str):
        return info
    _is64, dd, sections, off = info
    changed = []
    seen_shim = False
    while off is not None:
        oft, _ts, _fc, name_rva, _ft = struct.unpack_from("<IIIII", data, off)
        if not name_rva and not oft:
            break
        noff = rva_to_off(sections, name_rva)
        end = data.index(b"\0", noff)
        name = bytes(data[noff:end]).decode("ascii", "replace")
        if name.lower() in SHIMS:
            seen_shim = True
        shim = shim_for(name)
        if shim:
            if len(shim) > end - noff:
                return "import name too short to patch: " + name
            data[noff:end] = shim.encode("ascii") + b"\0" * (end - noff - len(shim))
            changed.append("%s -> %s" % (name, shim))
        off += 20
    delay_rva, _ = struct.unpack_from("<II", data, dd + 13 * 8)      # delay-load imports (names only)
    doff = rva_to_off(sections, delay_rva) if delay_rva else None
    while doff is not None:
        attrs, name_rva = struct.unpack_from("<II", data, doff)[:2]
        if not name_rva or not attrs & 1:
            break
        noff = rva_to_off(sections, name_rva)
        end = data.index(b"\0", noff)
        name = bytes(data[noff:end]).decode("ascii", "replace")
        if name.lower() in SHIMS:
            seen_shim = True
        shim = shim_for(name)
        if shim and len(shim) <= end - noff:
            data[noff:end] = shim.encode("ascii") + b"\0" * (end - noff - len(shim))
            changed.append("%s -> %s (delay-load)" % (name, shim))
        doff += 32
    if not changed:
        return "already patched" if seen_shim else "nothing to patch"
    bound_rva, _ = struct.unpack_from("<II", data, dd + 11 * 8)
    if bound_rva:                                        # bound imports would hide the new names
        struct.pack_into("<II", data, dd + 11 * 8, 0, 0)
    bak = path + ".orig"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return "patched (" + ", ".join(changed) + ")"


def check_imports(path):
    """Windows only: every imported DLL must load and export every imported function."""
    import ctypes
    folder = os.path.dirname(os.path.abspath(path))
    with open(path, "rb") as f:
        imports = read_imports(f.read())
    problems = 0
    for dll, funcs in imports:
        if dll.lower().startswith("python3"):
            continue                                     # provided by the interpreter that loads the module
        local = os.path.join(folder, dll)
        try:
            lib = ctypes.WinDLL(local if os.path.isfile(local) else dll)
        except OSError as e:
            print("[win7] WARNING: %s imports %s, which cannot be loaded here (%s)" % (os.path.basename(path), dll, e))
            problems += 1
            continue
        missing = [fn for fn in funcs if not fn.startswith("#") and not hasattr(lib, fn)]
        if missing:
            print("[win7] WARNING: %s imports %s from %s, which does not export it" %
                  (os.path.basename(path), ", ".join(missing), dll))
            problems += 1
    return problems


def static_check(path):
    """Windows only, no loading: warn about imported DLLs that are neither beside the file nor in System32."""
    folder = os.path.dirname(os.path.abspath(path))
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    with open(path, "rb") as f:
        imports = read_imports(f.read())
    for dll, _funcs in imports:
        if dll.lower().startswith("python3"):
            continue
        if not (os.path.isfile(os.path.join(folder, dll)) or os.path.isfile(os.path.join(sysdir, dll))):
            print("[win7] WARNING: %s imports %s, which was not found (send me this line)" % (os.path.basename(path), dll))


def scan_dir(top):
    for base, dirs, files in os.walk(top):
        for n in files:
            if n.lower().endswith((".exe", ".dll", ".pyd")) and n.lower() not in SHIMS:
                yield os.path.join(base, n)


def main(argv):
    absent = [s for s in SHIMS if not os.path.isfile(os.path.join(HERE, s))]
    if absent:
        print("[win7] missing next to patch_pyd.py: " + ", ".join(absent))
        return 1
    targets = []
    dir_targets = []
    for a in argv[1:]:
        if os.path.isdir(a):
            dir_targets += list(scan_dir(a))
        else:
            targets.append(a)
    if not targets and not dir_targets:
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d.lower() not in ("python", ".git", "win7")]
            targets += [os.path.join(base, n) for n in files if n.lower().endswith(".pyd")]
    folders = {os.path.join(ROOT, "python")}
    for t in targets:
        try:
            res = patch(t)
        except Exception as e:                           # never block the start because of this
            res = "error: %r" % (e,)
        print("[win7] %s: %s" % (os.path.relpath(t, ROOT), res))
        folders.add(os.path.dirname(os.path.abspath(t)))
    for d in folders:                                    # the stand-ins next to the Python and next to each module
        for s in SHIMS:
            src, dst = os.path.join(HERE, s), os.path.join(d, s)
            try:
                if os.path.isdir(d) and (not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src)):
                    shutil.copy2(src, dst)
            except OSError as e:
                print("[win7] could not copy %s to %s: %s" % (s, d, e))
    patched_in_dirs = 0
    for t in dir_targets:                                # bulk mode: only report what changed or looks wrong
        try:
            res = patch(t)
        except Exception as e:
            res = "error: %r" % (e,)
        if res.startswith("patched") or res.startswith("error") or res == "already patched":
            if res != "already patched":
                print("[win7] %s: %s" % (t, res))
                patched_in_dirs += 1
            d = os.path.dirname(os.path.abspath(t))
            for s in SHIMS:
                src, dst = os.path.join(HERE, s), os.path.join(d, s)
                try:
                    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                        shutil.copy2(src, dst)
                except OSError as e:
                    print("[win7] could not copy %s to %s: %s" % (s, d, e))
            if os.name == "nt":
                try:
                    static_check(t)
                except Exception:
                    pass
    if dir_targets:
        print("[win7] folders scanned: %d file(s) patched" % patched_in_dirs)
    if os.name == "nt":
        for t in targets:
            try:
                check_imports(t)
            except Exception as e:
                print("[win7] import check skipped for %s: %r" % (os.path.basename(t), e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
