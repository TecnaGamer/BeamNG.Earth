#!/usr/bin/env python3
"""Package official pinned Windows/Linux BeamMP binaries with Earth's plugin and source/notices.

The BeamMP binary is downloaded unchanged, not compiled by this project. No authentication key is included.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BEAMMP_VERSION = "3.9.3"
BEAMMP_COMMIT = "54f49d81e255d6021f6ebe04c85c6ecad4c7cef8"
EXE_SHA256 = "d090f7b0c55b86127766fd9ce15d9c66d37fbaef0f797d6a4cb237bde09eb87e"
EXE_SIZE = 8855040
EXE_URL = f"https://github.com/BeamMP/BeamMP-Server/releases/download/v{BEAMMP_VERSION}/BeamMP-Server.exe"
LINUX_SHA256 = "783ba928b20f9cc3c5794b4e2560c92b7528ef5063fc66439adadee96beb9af1"
LINUX_SIZE = 12468680
LINUX_URL = f"https://github.com/BeamMP/BeamMP-Server/releases/download/v{BEAMMP_VERSION}/BeamMP-Server.ubuntu.22.04.x86_64"
SOURCE_REPOS = (
    ("BeamMP-Server", "BeamMP/BeamMP-Server", BEAMMP_COMMIT, "."),
    ("commandline", "lionkor/commandline", "04952e4811e7d44708e64986bbc82fdc3485c800", "deps/commandline"),
    ("toml11", "ToruNiina/toml11", "f33ca743fb105bf54293cc822f56cf2794d15bf2", "deps/toml11"),
    ("vcpkg", "Microsoft/vcpkg", "5bf0c55239da398b8c6f450818c9e28d36bf9966", "vcpkg"),
)
MAX_DOWNLOAD = 128 * 1024 * 1024
CONFIG = '''[General]
AuthKey = ""
Debug = false
Description = "BeamNG.Earth community server"
Map = "/levels/beamng_earth/info.json"
MaxCars = 2
MaxPlayers = 8
Name = "BeamNG.Earth"
Port = 30814
Private = true
ResourceFolder = "Resources"
'''


FETCH_TRIES = 7         # GitHub's release downloads answered 504 for minutes during the 1.9.0 build (2026-11-09)
FETCH_WAIT_S = 15       # s before the second try, doubling (~16 min of tries in all)


def fetch(url, tries=FETCH_TRIES, wait=FETCH_WAIT_S, sleep=time.sleep):
    """url's body. A server error, a timeout or a dropped connection is tried again (tries in all, the wait doubling);
    a refused redirect, an oversized file or a 4xx answer is not."""
    for attempt in range(tries):
        try:
            return _fetch_once(url)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
            why = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise
            why = str(e)
        print(f"{url}: {why}; trying again in {wait * 2 ** attempt:.0f} s", file=sys.stderr, flush=True)
        sleep(wait * 2 ** attempt)
    raise RuntimeError("unreachable")


def _fetch_once(url):
    req = urllib.request.Request(url, headers={"User-Agent": "BeamNG.Earth-release-builder"})
    with urllib.request.urlopen(req, timeout=60) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("Refused insecure download redirect")
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Release download exceeds size limit")
    return data


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def member(z, name, data, mode=0o100644):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data)


def build(out_dir, downloader=fetch, platform="windows"):
    import package
    if platform not in ("windows", "linux"):
        raise ValueError("Unsupported host platform")
    linux = platform == "linux"
    binary_url, binary_size, binary_hash = (LINUX_URL, LINUX_SIZE, LINUX_SHA256) if linux else (EXE_URL, EXE_SIZE, EXE_SHA256)
    binary_name = "BeamMP-Server" if linux else "BeamMP-Server.exe"
    platform_name = "Linux-x86_64" if linux else "Windows"
    ver = package.version()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = downloader(binary_url)
    if len(exe) != binary_size or sha256(exe) != binary_hash:
        raise ValueError("Official BeamMP executable does not match pinned GitHub release digest")
    license_url = f"https://raw.githubusercontent.com/BeamMP/BeamMP-Server/{BEAMMP_COMMIT}/LICENSE"
    license_text = downloader(license_url)
    if b"GNU AFFERO GENERAL PUBLIC LICENSE" not in license_text:
        raise ValueError("BeamMP license unavailable or unexpected")
    sources = []
    source_path = out_dir / f"BeamMP-Server-{BEAMMP_VERSION}-corresponding-source.zip"
    source_tmp = source_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(source_tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name, repo, commit, install_at in SOURCE_REPOS:
                url = f"https://codeload.github.com/{repo}/zip/{commit}"
                data = downloader(url)
                # Preserve each exact upstream archive, including its license and build scripts; do not extract
                # remotely supplied paths. The instructions describe assembling its recorded submodules.
                with zipfile.ZipFile(io.BytesIO(data)) as upstream:
                    if upstream.testzip() is not None:
                        raise ValueError("Corrupt upstream source archive")
                filename = name + "-" + commit + ".zip"
                member(z, filename, data)
                sources.append({"repository": repo, "commit": commit, "archive": filename,
                                "install_at": install_at, "url": url, "sha256": sha256(data)})
            member(z, "sources.json", json.dumps(sources, indent=2))
            member(z, "LICENSE-BeamMP.txt", license_text)
            member(z, "BUILD-SOURCE.txt", "BeamMP-Server v" + BEAMMP_VERSION + " source and pinned submodules.\n"
                       "Extract the main archive, then place each submodule archive's contents at install_at in sources.json.\n"
                       "Follow the upstream README and CMake/vcpkg build instructions. vcpkg.json and the pinned vcpkg tree\n"
                       "describe third-party dependency sources and build recipes; building needs network access for them.\n"
                       "The bundled executable is the unmodified upstream release; this project did not compile it.\n")
        source_tmp.replace(source_path)
    finally:
        source_tmp.unlink(missing_ok=True)
    package.beammp_zip(str(out_dir), ver)
    plugin_path = out_dir / f"BeamNG.Earth-BeamMP-Server-{ver}.zip"
    host_path = out_dir / f"BeamNG.Earth-Host-{ver}-{platform_name}.zip"
    notices = (f"BeamNG.Earth {platform_name} host bundle {ver}\n\n"
               f"{binary_name} is the unmodified official BeamMP v{BEAMMP_VERSION} release.\n"
               f"Binary source: {binary_url}\nSHA-256: {binary_hash}\n"
               f"Corresponding source and pinned submodules: {source_path.name} (inside this bundle and alongside it).\n"
               f"Online source: https://github.com/BeamMP/BeamMP-Server/tree/{BEAMMP_COMMIT}\n"
               "BeamMP is AGPL-3.0; retain LICENSE-BeamMP.txt and source access when redistributing.\n"
               "Earth is derived from TecnaGamer's BeamNG.Earth (MIT); LICENSE-Earth.txt and CREDITS-Earth.md are included.\n"
               "This bundle does not contain BeamNG.drive or grant a game license. Every player still needs the Earth app.\n\n"
               "SETUP: unpack into a new folder, obtain your own BeamMP AuthKey from https://keymaster.beammp.com/,\n"
               f"put it in ServerConfig.toml, and run {'./run_host.sh' if linux else binary_name}. Private=true is the default.\n"
               + ("Linux target: Ubuntu 22.04 x86_64; other distributions need compatible system libraries.\n"
                  "If extraction lost executable permissions: chmod +x BeamMP-Server run_host.sh\n" if linux else "") +
               "Configure TCP/UDP 30814 forwarding only when hosting outside your LAN. Do not publish your AuthKey.\n"
               "Windows prerequisite: Microsoft Visual C++ x64 Redistributable, https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
               "Platform dependencies and server setup: https://docs.beammp.com/server/create-a-server/\n"
               "See BeamNG.Earth-README.md for Earth-specific setup. No Redis or voice service is required.\n")
    host_tmp = host_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(host_tmp, "w", zipfile.ZIP_DEFLATED) as z:
            with zipfile.ZipFile(plugin_path) as plugin:
                for info in plugin.infolist():
                    z.writestr(info, plugin.read(info.filename))
            member(z, binary_name, exe, 0o100755)
            if linux:
                member(z, "run_host.sh", '#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")"\nexec ./BeamMP-Server "$@"\n', 0o100755)
            z.writestr("ServerConfig.toml", CONFIG)
            z.writestr("LICENSE-BeamMP.txt", license_text)
            # Earth's license and credits already arrived with the plugin ZIP.
            z.writestr("HOSTING.txt", notices)
            z.write(source_path, source_path.name)
            z.writestr("UPSTREAM-PROVENANCE.json", json.dumps({"beammp_version": BEAMMP_VERSION,
                        "binary_url": binary_url, "binary_sha256": binary_hash, "platform": platform_name, "locally_compiled": False,
                        "source_bundle": source_path.name, "source_sha256": sha256(source_path.read_bytes()),
                        "sources": sources}, indent=2))
        host_tmp.replace(host_path)
    finally:
        host_tmp.unlink(missing_ok=True)
    print(host_path)
    print(source_path)
    return host_path, source_path


def smoke(host_path, platform):
    """Only --help, in an isolated temporary directory: no server or configuration startup."""
    expected = "windows" if os.name == "nt" else "linux"
    if platform != expected:
        raise ValueError("Cannot execute a foreign-platform host binary")
    name = "BeamMP-Server.exe" if platform == "windows" else "BeamMP-Server"
    with tempfile.TemporaryDirectory() as d, zipfile.ZipFile(host_path) as z:
        path = Path(d) / name
        path.write_bytes(z.read(name))
        path.chmod(0o755)
        result = subprocess.run([str(path), "--help"], cwd=d, capture_output=True, timeout=20)
        if result.returncode != 0 or b"USAGE" not in result.stdout:
            raise RuntimeError(f"Host --help smoke test failed: exit {result.returncode}")
        print(f"{platform} host --help passed")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--platform", choices=("windows", "linux", "all"), default="all")
    ap.add_argument("--smoke", action="store_true", help="Test --help for this machine's platform only")
    args = ap.parse_args()
    platforms = ("windows", "linux") if args.platform == "all" else (args.platform,)
    native = "windows" if os.name == "nt" else "linux"
    for platform in platforms:
        host, _source = build(args.out, platform=platform)
        if args.smoke and platform == native:
            smoke(host, platform)


if __name__ == "__main__":
    main()
