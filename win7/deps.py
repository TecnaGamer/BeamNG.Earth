"""Static dependency walker (Windows 7): finds which file needs a DLL that this PC does not have.

    python win7/deps.py "C:\\path\\to\\rustc.exe"

Starts at the given .exe/.dll/.pyd, follows every DLL that sits beside it or on PATH (the Windows system folder is
trusted, not followed), and prints each import that cannot be found, with the file that asks for it. Run it on
the file that shows the "missing DLL" dialog; paste the output back if win7/patch_pyd.py does not fix it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patch_pyd as p


def delay_imports(data):
    import struct
    info = p.parse(data)
    if isinstance(info, str):
        return []
    _is64, dd, sections, _off = info
    rva, _ = struct.unpack_from("<II", data, dd + 13 * 8)
    off = p.rva_to_off(sections, rva) if rva else None
    out = []
    while off is not None:
        attrs, name_rva = struct.unpack_from("<II", data, off)[:2]
        if not name_rva or not attrs & 1:
            break
        out.append(p.cstr(data, p.rva_to_off(sections, name_rva)))
        off += 32
    return out


def main(argv):
    if len(argv) != 2 or not os.path.isfile(argv[1]):
        print(__doc__)
        return 1
    root = os.path.abspath(argv[1])
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    folders = [os.path.dirname(root)] + [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    folders.append(sysdir)
    seen, missing, todo = set(), [], [root]
    while todo:
        f = todo.pop()
        if f.lower() in seen:
            continue
        seen.add(f.lower())
        with open(f, "rb") as fh:
            data = fh.read()
        names = [(n, "") for n, _ in p.read_imports(data)] + [(n, " (delay-load)") for n in delay_imports(data)]
        for name, kind in names:
            if name.lower().startswith("python3"):
                continue
            found = next((os.path.join(d, name) for d in folders if os.path.isfile(os.path.join(d, name))), None)
            if not found:
                missing.append((os.path.basename(f), name + kind, p.shim_for(name)))
            elif os.path.dirname(found).lower() != sysdir:
                todo.append(found)
    print("[win7] files examined: %d" % len(seen))
    if not missing:
        print("[win7] every imported DLL was found.")
    for who, name, shim in missing:
        print("[win7] %s needs %s -> %s" % (who, name, ("patchable with " + shim) if shim else "NOT covered by the patcher"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
