#!/usr/bin/env python3
"""Build the standalone BeamNG.Earth-Sidekick executable for companion compute offloading:

    Linux:   dist/BeamNG.Earth-Sidekick-<VERSION>-x86_64
    Windows: dist/BeamNG.Earth-Sidekick-<VERSION>.exe

    python tools/build_sidekick.py
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "BeamNG.Earth-Sidekick"
IS_WIN = os.name == "nt"
SEP = ";" if IS_WIN else ":"


def _overture_release():
    """overture.RELEASE, read from the source (the build script does not import the server)."""
    import re
    with open(os.path.join(ROOT, "server", "overture.py"), encoding="utf-8") as f:
        return re.search(r'^RELEASE = "([^"]+)"', f.read(), re.M).group(1)


def version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def make_icons(work):
    """icon.png (256 px, the level's preview cropped square) and icon.ico."""
    from PIL import Image
    im = Image.open(os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "beamng_earth_preview.jpg"))
    s = min(im.size)
    im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2))
    im = im.convert("RGBA").resize((256, 256), Image.LANCZOS)
    png, ico = os.path.join(work, "icon.png"), os.path.join(work, "icon.ico")
    im.save(png)
    im.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return png, ico


def win_version(ver):
    """'1.7.0-experimental' -> (1, 7, 0, 0): the four numbers of a Windows version resource."""
    nums = []
    for part in ver.split("-")[0].split(".")[:4]:
        digits = "".join(c for c in part if c.isdigit())
        nums.append(min(int(digits or 0), 65535))
    return tuple(nums + [0] * (4 - len(nums)))


def version_files(work, ver):
    """PyInstaller's --version-file and --manifest for Sidekick."""
    v4 = win_version(ver)
    dotted = ".".join(map(str, v4))
    strings = {
        "CompanyName": "TecnaGamer",
        "FileDescription": "BeamNG.Earth Companion Offload Sidekick Server",
        "FileVersion": ver,
        "InternalName": NAME,
        "LegalCopyright": "Copyright (c) 2026 TecnaGamer, MIT License",
        "OriginalFilename": f"{NAME}-{ver}.exe",
        "ProductName": "BeamNG.Earth Sidekick",
        "ProductVersion": ver,
        "Comments": "Companion compute offloading server for BeamNG.Earth handhelds. https://github.com/TecnaGamer/BeamNG.Earth",
    }
    vf = os.path.join(work, "version_info.txt")
    with open(vf, "w", encoding="utf-8") as f:
        f.write("VSVersionInfo(\n"
                f"  ffi=FixedFileInfo(filevers={v4}, prodvers={v4}, mask=0x3f, flags=0x0, OS=0x40004,\n"
                "                    fileType=0x1, subtype=0x0, date=(0, 0)),\n"
                "  kids=[\n"
                "    StringFileInfo([StringTable('040904B0', [\n")
        f.write(",\n".join(f"      StringStruct({k!r}, {v!r})" for k, v in strings.items()))
        f.write("])]),\n"
                "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
                "  ]\n)\n")
    mf = os.path.join(work, "app.manifest")
    with open(mf, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="BeamNG.Earth.Sidekick" version="{dotted}" processorArchitecture="amd64"/>
  <description>{strings["FileDescription"]}</description>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{{e2011457-1546-43c5-a5fe-008deee3d3f0}}"/>
      <supportedOS Id="{{35138b9a-5d96-4fbd-8e2d-a2440225f93a}}"/>
      <supportedOS Id="{{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}}"/>
      <supportedOS Id="{{1f676c76-80e1-4239-95bb-83d0f6d0da78}}"/>
      <supportedOS Id="{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}"/>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
</assembly>
""")
    return vf, mf


def own_bootloader():
    """build/bootloader/<this PyInstaller>/run.exe (tools/build_bootloader.py), or None."""
    import PyInstaller
    p = os.path.join(ROOT, "build", "bootloader", PyInstaller.__version__, "run.exe")
    return p if os.path.isfile(p) else None


BOOT_SHIM = ("import sys\n"
             "from PyInstaller.building import api\n"
             "boot = sys.argv.pop(1)\n"
             "api.EXE._bootloader_file = lambda self, exe, extension=None: boot\n"
             "from PyInstaller.__main__ import run\n"
             "run(sys.argv[1:])\n")


def pyinstaller(work, ico, ver=None, boot=None):
    server = os.path.join(ROOT, "server")
    mods = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(server, "*.py")))
    cmd = [sys.executable] + (["-c", BOOT_SHIM, boot] if boot else ["-m", "PyInstaller"])
    cmd += ["--noconfirm", "--clean", "--console", "--noupx", "--name", NAME,
            "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "build"),
            "--specpath", work, "--paths", server, "--paths", ROOT, "--icon", ico,
            "--collect-submodules", "fsspec",
            "--hidden-import", "certifi", "--collect-data", "certifi",
            "--hidden-import", "truststore",
            "--add-data", f"{os.path.join(ROOT, 'mod', 'beamng_earth')}{SEP}mod/beamng_earth",
            "--add-data", f"{os.path.join(ROOT, 'VERSION')}{SEP}.",
            "--add-data", f"{os.path.join(server, 'bridges_us.npz')}{SEP}.",
            "--add-data", f"{os.path.join(server, 'bridges_wd.npz')}{SEP}.",
            "--add-data", f"{os.path.join(server, 'koppen_1991_2020_0p1.png')}{SEP}.",
            "--add-data", f"{os.path.join(server, 'oil_wells.npz')}{SEP}.",
            "--add-data", f"{os.path.join(server, 'refineries.npz')}{SEP}.",
            "--add-data", f"{os.path.join(server, 'overture_stac_' + _overture_release() + '.json.gz')}{SEP}."]

    # Exact source fingerprint matching main app
    sys.path.insert(0, server)
    import peerwork
    with open(os.path.join(work, "CODE_ID"), "w", encoding="ascii") as f:
        f.write(peerwork.source_id(server))
    cmd += ["--add-data", f"{os.path.join(work, 'CODE_ID')}{SEP}."]
    cmd += ["--onefile"]

    if IS_WIN:
        vf, mf = version_files(work, ver)
        cmd += ["--version-file", vf, "--manifest", mf]

    for m in mods:
        cmd += ["--hidden-import", m]

    for lib in ("earthfast.pyd", "earthfast.abi3.so"):
        p = os.path.join(server, lib)
        if os.path.isfile(p) and (lib.endswith(".pyd") == IS_WIN):
            cmd += ["--add-binary", f"{p}{SEP}."]

    entry = os.path.join(ROOT, "tools", "sidekick.py")
    cmd.append(entry)
    subprocess.run(cmd, check=True, cwd=ROOT)
    return os.path.join(work, "dist")


def main():
    ap = argparse.ArgumentParser(description="Build BeamNG.Earth-Sidekick standalone executable")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="Output directory")
    ap.add_argument("--stock-bootloader", action="store_true", help="Windows: use PyInstaller's prebuilt bootloader")
    ap.add_argument("--require-own-bootloader", action="store_true", help="Windows: stop if own bootloader missing")
    a = ap.parse_args()

    work = os.path.join(ROOT, "build", "sidekick")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    os.makedirs(a.out, exist_ok=True)

    png, ico = make_icons(work)
    ver = version()

    if IS_WIN:
        boot = None if a.stock_bootloader else own_bootloader()
        if boot:
            print(f"bootloader: {boot} (built from source)")
        elif a.require_own_bootloader:
            sys.exit("no bootloader built for this PyInstaller: run tools/build_bootloader.py")
        else:
            print("WARNING: using stock bootloader (or run tools/build_bootloader.py first)")
        dist = pyinstaller(work, ico, ver=ver, boot=boot)
        out = os.path.join(a.out, f"{NAME}-{ver}.exe")
        shutil.copyfile(os.path.join(dist, NAME + ".exe"), out)
    else:
        dist = pyinstaller(work, ico, ver=ver)
        out = os.path.join(a.out, f"{NAME}-{ver}-x86_64")
        shutil.copyfile(os.path.join(dist, NAME), out)
        os.chmod(out, 0o755)

    size_mb = os.path.getsize(out) / 1e6
    print(f"Successfully built {out} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()

