"""One-click update (GitHub #140, 2026-10-22: "an automatic updater or button that makes it easier to update the mod").

The in-game update notice (core.lua) has an "Update now" button for the packaged app (the Windows .exe, the Linux
AppImage). The server then downloads that platform's file of GitHub's latest release next to the running one, under
its own release name (BeamNG.Earth-<version>.exe / -x86_64.AppImage; a development build: the newest release or
pre-release, updates.newest_release), checks it against the SHA-256 digest GitHub
publishes for the release asset, and notes it in STAGED beside it. The running copy is left alone (Windows cannot
replace a running .exe): the next time the old copy starts, the launcher hands over to the new file (staged()).

Only this project's own releases are fetched (api.github.com/repos/<REPO>, assets under github.com/<REPO>/releases),
and a file whose digest is missing or wrong is deleted, never run. A source copy (git) is told to pull instead.
"""
import hashlib
import json
import os
import sys
import urllib.request

import netfetch
import updates

API_URL = f"https://api.github.com/repos/{updates.REPO}/releases/latest"
ASSET_PREFIX = f"https://github.com/{updates.REPO}/releases/download/"
STAGED = "BeamNG.Earth-update.json"     # beside the downloaded file: {"version", "path", "sha256"}
CHUNK = 1 << 16


class UpdateError(RuntimeError):
    """The update could not be downloaded or checked (the message says why, for the player)."""


def packaged_path(environ=None, frozen=None, executable=None):
    """The running packaged app's file (the .exe, the .AppImage), or None for a source copy."""
    environ = os.environ if environ is None else environ
    if environ.get("APPIMAGE"):
        return environ["APPIMAGE"]
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return executable or sys.executable
    return None


def asset_suffix(path):
    """The release asset kind that replaces the packaged file at path, or None."""
    p = str(path or "")
    if p.lower().endswith(".exe"):
        return ".exe"
    if p.endswith(".AppImage"):
        return ".AppImage"
    return None


def pick(release, suffix):
    """(version, name, url, size, sha256) of the release's asset for suffix, or None. Only this project's assets with
    a SHA-256 digest count."""
    if not isinstance(release, dict) or not suffix:
        return None
    version = str(release.get("tag_name") or "").lstrip("v")
    if updates.parse(version) is None:
        return None
    for a in release.get("assets") or ():
        name, url, digest = str(a.get("name") or ""), str(a.get("browser_download_url") or ""), str(a.get("digest") or "")
        if not name.endswith(suffix) or not url.startswith(ASSET_PREFIX) or "/" in name or "\\" in name:
            continue
        if not digest.startswith("sha256:") or len(digest) != 71:
            continue
        return version, name, url, int(a.get("size") or 0), digest[7:].lower()
    return None


def _get_json(net, url):
    with net.session(url, kind="demand") as s:
        req = urllib.request.Request(url, headers={**s.headers, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            s.status = resp.status
            body = resp.read(4 << 20)
            s.nbytes = len(body)
    return json.loads(body.decode("utf-8", "replace"))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(net, current, path=None, progress=None):
    """Download the latest release's file for the packaged app at path (default: this one) beside it and stage it.
    Returns (version, the new file's path). progress(done bytes, total bytes) is called as it goes. Raises
    UpdateError (no newer release, a source copy, no matching asset, a bad download)."""
    path = path or packaged_path()
    if not path:
        raise UpdateError("this copy runs from source: update it with git pull")
    suffix = asset_suffix(path)
    if not suffix:
        raise UpdateError(f"not a packaged BeamNG.Earth file: {os.path.basename(path)}")
    if updates.is_dev(current):         # a development build: the newest release or pre-release (updates.py)
        got = updates.newest_release(net, fresh=True)
        if got is None:
            raise UpdateError("GitHub did not answer")
        release = got[2]
    else:
        try:
            release = _get_json(net, API_URL)
        except (netfetch.FetchError, OSError, ValueError) as e:
            raise UpdateError(f"GitHub did not answer ({e})") from e
    got = pick(release, suffix)
    if got is None:
        raise UpdateError("the latest release has no download for this system")
    version, name, url, size, digest = got
    if not updates.newer(version, current):
        raise UpdateError(f"already up to date ({current})")
    folder = os.path.dirname(os.path.abspath(path))
    dest = os.path.join(folder, name)
    if os.path.abspath(dest) == os.path.abspath(path):
        raise UpdateError("the new file has this file's name")
    if not (os.path.exists(dest) and sha256_of(dest) == digest):
        part = dest + ".part"
        try:
            with net.session(url, kind="demand") as s:
                req = urllib.request.Request(url, headers=s.headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    final = resp.geturl()
                    if not final.startswith("https://"):
                        raise UpdateError("the download was redirected off https")
                    s.status = resp.status
                    done = 0
                    with open(part, "wb") as f:
                        while True:
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            s.nbytes = done
                            if progress:
                                progress(done, size)
        except (netfetch.FetchError, OSError) as e:
            _remove(part)
            raise UpdateError(f"the download failed ({e})") from e
        if (size and os.path.getsize(part) != size) or sha256_of(part) != digest:
            _remove(part)
            raise UpdateError("the downloaded file did not match GitHub's checksum (deleted)")
        os.replace(part, dest)
    if suffix == ".AppImage":
        os.chmod(dest, 0o755)
    tmp = os.path.join(folder, STAGED + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": version, "path": name, "sha256": digest}, f)
    os.replace(tmp, os.path.join(folder, STAGED))
    return version, dest


def staged(current, path=None):
    """The downloaded newer copy to start instead of this one (its path), or None. Checked again by its digest; the
    note is removed when that file is gone or changed (kept when it is not newer: the new copy itself reads it, and the
    old one, started again from the player's shortcut, still hands over)."""
    path = path or packaged_path()
    if not path:
        return None
    folder = os.path.dirname(os.path.abspath(path))
    note = os.path.join(folder, STAGED)
    try:
        with open(note, encoding="utf-8") as f:
            d = json.load(f)
        version, name, digest = str(d["version"]), str(d["path"]), str(d["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    new = os.path.join(folder, os.path.basename(name))
    if not updates.newer(version, current) or os.path.abspath(new) == os.path.abspath(path):
        return None
    if not (os.path.isfile(new) and sha256_of(new) == digest):
        _remove(note)
        return None
    return new


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass
