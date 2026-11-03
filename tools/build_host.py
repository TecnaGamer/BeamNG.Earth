#!/usr/bin/env python3
"""Package the official pinned Windows BeamMP binary with Earth's plugin and source/notices.

The BeamMP binary is downloaded unchanged, not compiled by this project. No authentication key is included.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BEAMMP_VERSION = "3.9.3"
BEAMMP_COMMIT = "54f49d81e255d6021f6ebe04c85c6ecad4c7cef8"
EXE_SHA256 = "d090f7b0c55b86127766fd9ce15d9c66d37fbaef0f797d6a4cb237bde09eb87e"
EXE_SIZE = 8855040
EXE_URL = f"https://github.com/BeamMP/BeamMP-Server/releases/download/v{BEAMMP_VERSION}/BeamMP-Server.exe"
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
Description = "Beam-Earth community server"
Map = "/levels/beamng_earth/info.json"
MaxCars = 2
MaxPlayers = 8
Name = "Beam-Earth"
Port = 30814
Private = true
ResourceFolder = "Resources"
'''


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Beam-Earth-release-builder"})
    with urllib.request.urlopen(req, timeout=60) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("Refused insecure download redirect")
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Release download exceeds size limit")
    return data


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(out_dir, downloader=fetch):
    import package
    ver = package.version()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = downloader(EXE_URL)
    if len(exe) != EXE_SIZE or sha256(exe) != EXE_SHA256:
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
                z.writestr(filename, data)
                sources.append({"repository": repo, "commit": commit, "archive": filename,
                                "install_at": install_at, "url": url, "sha256": sha256(data)})
            z.writestr("sources.json", json.dumps(sources, indent=2))
            z.writestr("LICENSE-BeamMP.txt", license_text)
            z.writestr("BUILD-SOURCE.txt", "BeamMP-Server v" + BEAMMP_VERSION + " source and pinned submodules.\n"
                       "Extract the main archive, then place each submodule archive's contents at install_at in sources.json.\n"
                       "Follow the upstream README and CMake/vcpkg build instructions. vcpkg.json and the pinned vcpkg tree\n"
                       "describe third-party dependency sources and build recipes; building needs network access for them.\n"
                       "The bundled executable is the unmodified upstream release; this project did not compile it.\n")
        source_tmp.replace(source_path)
    finally:
        source_tmp.unlink(missing_ok=True)
    package.beammp_zip(str(out_dir), ver)
    plugin_path = out_dir / f"BeamNG.Earth-BeamMP-Server-{ver}.zip"
    host_path = out_dir / f"BeamNG.Earth-Host-{ver}-Windows.zip"
    notices = (f"Beam-Earth Windows host bundle {ver}\n\n"
               f"BeamMP-Server.exe is the unmodified official BeamMP v{BEAMMP_VERSION} Windows release.\n"
               f"Binary source: {EXE_URL}\nSHA-256: {EXE_SHA256}\n"
               f"Corresponding source and pinned submodules: {source_path.name} (inside this bundle and alongside it).\n"
               f"Online source: https://github.com/BeamMP/BeamMP-Server/tree/{BEAMMP_COMMIT}\n"
               "BeamMP is AGPL-3.0; retain LICENSE-BeamMP.txt and source access when redistributing.\n"
               "Earth is derived from TecnaGamer's BeamNG.Earth (MIT); LICENSE-Earth.txt and CREDITS-Earth.md are included.\n"
               "This bundle does not contain BeamNG.drive or grant a game license. Every player still needs the Earth app.\n\n"
               "SETUP: unpack into a new folder, obtain your own BeamMP AuthKey from https://keymaster.beammp.com/,\n"
               "put it in ServerConfig.toml, and run BeamMP-Server.exe. Private=true is the default.\n"
               "Configure TCP/UDP 30814 forwarding only when hosting outside your LAN. Do not publish your AuthKey.\n"
               "See BeamNG.Earth-README.md for Earth-specific setup. No Redis or voice service is required.\n")
    host_tmp = host_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(host_tmp, "w", zipfile.ZIP_DEFLATED) as z:
            with zipfile.ZipFile(plugin_path) as plugin:
                for info in plugin.infolist():
                    z.writestr(info, plugin.read(info.filename))
            z.writestr("BeamMP-Server.exe", exe)
            z.writestr("ServerConfig.toml", CONFIG)
            z.writestr("LICENSE-BeamMP.txt", license_text)
            z.write(ROOT / "LICENSE", "LICENSE-Earth.txt")
            z.write(ROOT / "CREDITS.md", "CREDITS-Earth.md")
            z.writestr("HOSTING.txt", notices)
            z.write(source_path, source_path.name)
            z.writestr("UPSTREAM-PROVENANCE.json", json.dumps({"beammp_version": BEAMMP_VERSION,
                        "binary_url": EXE_URL, "binary_sha256": EXE_SHA256, "locally_compiled": False,
                        "source_bundle": source_path.name, "source_sha256": sha256(source_path.read_bytes()),
                        "sources": sources}, indent=2))
        host_tmp.replace(host_path)
    finally:
        host_tmp.unlink(missing_ok=True)
    print(host_path)
    print(source_path)
    return host_path, source_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    build(args.out)


if __name__ == "__main__":
    main()
