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
import ioprofiler
import re
import sys
import urllib.request

import netfetch
import updates

API_URL = updates.LATEST_API
ASSET_PREFIX = f"https://github.com/{updates.REPO}/releases/download/"
STAGED = "BeamNG.Earth-update.json"     # beside the downloaded file: {"version", "path", "sha256"}
# beside it too: a downloaded version Windows would not start (Smart App Control: an unsigned .exe, no "run anyway";
# GitHub #823): {"version", "by"}, not offered again (earthd.check_update)
BLOCKED = "BeamNG.Earth-update-blocked.json"
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
        with ioprofiler.urlopen(req, timeout=30, context=netfetch.windows_ssl_context()) as resp:
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
                with ioprofiler.urlopen(req, timeout=60, context=netfetch.windows_ssl_context()) as resp:
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
        note_downloaded(dest)
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


def drop_staged(path=None, blocked_by=None):
    """Forget the downloaded copy (its note): it would not start (launcher.hand_over), so this one runs from now on.
    blocked_by (e.g. "smart_app_control"): its version is remembered (BLOCKED) and not offered again (blocked)."""
    path = path or packaged_path()
    if not path:
        return
    folder = os.path.dirname(os.path.abspath(path))
    note = os.path.join(folder, STAGED)
    if blocked_by:
        try:
            with open(note, encoding="utf-8") as f:
                version = str(json.load(f)["version"])
            with open(os.path.join(folder, BLOCKED), "w", encoding="utf-8") as f:
                json.dump({"version": version, "by": str(blocked_by)}, f)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    _remove(note)


def blocked(path=None):
    """{"version", "by"} of the newest downloaded version Windows would not start (drop_staged), or None."""
    path = path or packaged_path()
    if not path:
        return None
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(path)), BLOCKED), encoding="utf-8") as f:
            d = json.load(f)
        return {"version": str(d["version"]), "by": str(d.get("by") or "")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


# Old copies (GitHub #900: "~300 MB of old copies after a few days of dev builds"): every update leaves the previous
# file beside the new one, and a shortcut to an old file still works (it hands over, staged()), so they cannot all go.
# Kept: this copy, the newest older one (to go back to: the SAC case of #823), every copy a hand-over started from
# (the player's shortcut points at it: HANDED_OVER_FROM, noted in KEEP), and, while no such copy is known, the oldest
# (the one first downloaded, most likely the shortcut's). The rest of BeamNG.Earth-<version> files of this kind that
# are older than this copy are deleted (one still running stays: Windows refuses, _remove ignores it), but only files
# this updater downloaded itself (DOWNLOADED, noted by download()): a copy the player downloaded by hand, which a
# shortcut may point at before any hand-over has noted it, and every copy from before this list existed, stay.
KEEP = "BeamNG.Earth-keep.json"         # beside the copies: {"started_from": [file names]}
DOWNLOADED = "BeamNG.Earth-downloaded.json"     # beside them too: {"downloaded": [file names download() fetched]}
HANDED_OVER_FROM = "BNE_HANDED_OVER_FROM"   # (the environment of a copy a hand-over started: the old copy's path)
COPY_NAME = re.compile(r"^BeamNG\.Earth-(?P<v>.+?)(?:-x86_64)?(?P<s>\.exe|\.AppImage)$", re.IGNORECASE)


def _kept(folder):
    try:
        with open(os.path.join(folder, KEEP), encoding="utf-8") as f:
            got = json.load(f).get("started_from") or []
        return [os.path.basename(str(n)) for n in got if n]
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def _downloaded(folder):
    try:
        with open(os.path.join(folder, DOWNLOADED), encoding="utf-8") as f:
            got = json.load(f).get("downloaded") or []
        return [os.path.basename(str(n)) for n in got if n]
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def note_downloaded(dest):
    """Note the file dest (a path) as one download() fetched: prune_old may delete it once it is old. Names whose file
    is gone are dropped from the note."""
    folder = os.path.dirname(os.path.abspath(dest))
    name = os.path.basename(dest)
    names = [n for n in _downloaded(folder) if n != name and os.path.isfile(os.path.join(folder, n))]
    tmp = os.path.join(folder, DOWNLOADED + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"downloaded": names + [name]}, f)
        os.replace(tmp, os.path.join(folder, DOWNLOADED))
    except OSError:
        _remove(tmp)


def note_started_from(old, path=None):
    """Keep the copy old (a path) from now on: a hand-over started this one from it (the player's shortcut)."""
    path = path or packaged_path()
    if not (path and old):
        return
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.dirname(os.path.abspath(old)) != folder:
        return
    names = _kept(folder)
    name = os.path.basename(old)
    if name in names:
        return
    tmp = os.path.join(folder, KEEP + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"started_from": names + [name]}, f)
        os.replace(tmp, os.path.join(folder, KEEP))
    except OSError:
        _remove(tmp)


def prune_old(current, path=None, remove=None):
    """Delete the old copies beside this one that this updater downloaded and nothing needs (KEEP's comment). Returns
    the deleted files' names."""
    path = path or packaged_path()
    suffix = asset_suffix(path)
    if not (path and suffix and updates.parse(current)):
        return []
    folder = os.path.dirname(os.path.abspath(path))
    me = os.path.basename(path).lower()
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    fetched = {n.lower() for n in _downloaded(folder)}
    old = []
    for n in names:
        m = COPY_NAME.match(n)
        if (not m or n.lower() not in fetched or n.lower() == me or m.group("s").lower() != suffix.lower()
                or not os.path.isfile(os.path.join(folder, n)) or not updates.newer(current, m.group("v"))):
            continue
        old.append((updates.parse(m.group("v")), n))
    if len(old) < 2:
        return []
    old.sort()
    kept = {n.lower() for n in _kept(folder)}
    keep = {old[-1][1].lower()} | kept
    if not kept:
        keep.add(old[0][1].lower())
    out = []
    for _v, n in old:
        if n.lower() in keep:
            continue
        p = os.path.join(folder, n)
        (remove or _remove)(p)
        if not os.path.exists(p):
            out.append(n)
    return out


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass
