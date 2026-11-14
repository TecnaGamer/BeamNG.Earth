"""The in-game reports, the world server's part (mod report.lua; the user, 2026-11-12: "Could we update the in game
reporter to send reports directly to our mini pc server?" and "update the reporter with any info and data that would be
useful to us"). Reports are private: sent to the BeamNG.Earth developers only (the collector on the project's own
server, native/serverlist reports.rs), never published.

report_context {folder, camera, aim, player}   the moment of a report (Ctrl+Shift+E / the profiler): the server's own
        state written into the report folder: server.json (build and config, env overrides, memory, the connection's
        backlog, the data sources and the map's ids at the camera and the aim point, the roads / buildings / structures
        within NEAR_M of the aim point) and logs.json (the server log's end and the game log's recent warnings and
        errors). Everything goes through earthfast's report_scrub (home folders, user names, addresses) before it is
        written.
report_saved {folder}     the mod wrote report.json / hardware.json: scrubbed the same way in place.
report_send {folder}      the player clicked "Send to the developers" (and nothing is sent without it; telemetry's
        switches do not apply: each send is the player's own choice). The choices are the folder's send.json
        (screenshot, exactLocation, logs); the state is sent.json ({state: queued / sending / sent / failed, id, text})
        and report_status messages to the game. Retried with backoff (BACKOFF_S); a report still queued when the server
        stops is sent at its next start (resume, RESUME_DAYS).

The upload body is built by earthfast.report_body (the JSON parts scrubbed again, an exact location rounded to 0.01 deg
when the player left it out, every part under the collector's caps). BNGE_REPORT_URL points the sends elsewhere (tests).
"""
import asyncio
import getpass
import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request

import fast

log = logging.getLogger("earthd.reports")

NEED = 267
URL = os.environ.get("BNGE_REPORT_URL") or "https://earth.tgforever.net/report"
NAME_RE = re.compile(r"[0-9]{8}_[0-9]{6}_(?:report|profile)_[0-9]{1,6}")
SUBDIRS = ("beamngearth", "beamngEarth")     # (the game lowercases new folders)
BACKOFF_S = (30, 120, 600, 1800, 3600)       # between tries; after the last one: failed until the next start
RESUME_DAYS = 14                             # a queued report older than this is not sent at a start any more
TIMEOUT_S = 90
NEAR_M = 30.0
LOG_TAIL_BYTES = 4 << 20                     # of a log file read for its end
SERVER_LINES = 300
GAME_LOG_S = 600.0
SECRET_RE = re.compile(r"TOKEN|KEY|SECRET|PASS|AUTH|COOKIE|CRED", re.I)
NEAR_LAYERS = ("roads", "buildings", "infrastructure")
NEAR_ATTRS = 24                              # attributes kept per nearby feature


def _ef():
    fast.require(NEED, "the in-game reports")
    return fast._ef


def folder(user_dir, name):
    """The report folder <user>/beamngEarth/reports/<name>, or None: only the reporter's own folder names
    (NAME_RE), never a path from the message."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    for sub in SUBDIRS:
        p = os.path.join(user_dir, sub, "reports", name)
        if os.path.isdir(p):
            return p
    return None


def roots(user_dir):
    return [os.path.join(user_dir, sub, "reports") for sub in SUBDIRS
            if os.path.isdir(os.path.join(user_dir, sub, "reports"))]


def user_names():
    """This computer's user and machine names (scrubbed from everything a report holds)."""
    out = set()
    for f in (getpass.getuser, socket.gethostname):
        try:
            out.add(f())
        except Exception:
            pass
    for k in ("USER", "USERNAME", "LOGNAME", "COMPUTERNAME", "HOSTNAME"):
        if os.environ.get(k):
            out.add(os.environ[k])
    out.add(os.path.basename(os.path.expanduser("~").rstrip("/\\")))
    return sorted(n for n in out if n)


def _read(path, limit=None):
    with open(path, "rb") as fh:
        if limit is not None:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
        return fh.read()


def _write_json(path, obj):
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(path + ".tmp", path)


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def git_commit(root):
    """The checkout's commit (a source run; a packaged build has none)."""
    try:
        g = os.path.join(root, ".git")
        if os.path.isfile(g):           # (a worktree: "gitdir: <path>")
            with open(g, encoding="utf-8") as fh:
                g = fh.read().split("gitdir:", 1)[1].strip()
        with open(os.path.join(g, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            return head[:12]
        ref = head[4:].strip()
        common = g
        if os.path.isfile(os.path.join(g, "commondir")):
            with open(os.path.join(g, "commondir"), encoding="utf-8") as fh:
                common = os.path.normpath(os.path.join(g, fh.read().strip()))
        for base in (g, common):
            p = os.path.join(base, ref)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    return fh.read().strip()[:12]
        with open(os.path.join(common, "packed-refs"), encoding="utf-8") as fh:
            for line in fh:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0][:12]
    except (OSError, IndexError):
        pass
    return None


def env_overrides():
    """The BNE_* / BNGE_* settings in effect (a secret-looking name: only that it is set)."""
    return {k: ("<set>" if SECRET_RE.search(k) else os.environ[k][:200])
            for k in sorted(os.environ) if k.startswith(("BNE_", "BNGE_"))}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _latlon(d):
    if not isinstance(d, dict):
        return None
    lat, lon = _num(d.get("lat")), _num(d.get("lon"))
    if lat is None or lon is None or not (-85 <= lat <= 85) or not (-180 <= lon <= 180):
        return None
    return lat, lon


def spot_keys(lat, lon):
    """The map's ids at a point: the map cell, the geotile."""
    import geotiles
    import mapcells
    out = {"lat": lat, "lon": lon}
    try:
        out["mapCell"] = str(mapcells.cell_of(lat, lon))
    except Exception as e:
        out["mapCell"] = f"error: {type(e).__name__}"
    try:
        out["geotile"] = geotiles.key(*geotiles.tile_of(lat, lon))
    except Exception as e:
        out["geotile"] = f"error: {type(e).__name__}"
    return out


def _scalar(v):
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v if not isinstance(v, str) else v[:120]
    try:
        x = v.item()               # (a numpy scalar)
        return x if not isinstance(x, str) else x[:120]
    except Exception:
        s = str(v)
        return s[:120] if s not in ("", "None", "nan") else None


def nearby(md, lat, lon, max_m=NEAR_M):
    """The features of NEAR_LAYERS within max_m of (lat, lon) in the cached map cells (never fetched): their ids and
    attributes, nearest first (earthfast report_nearby measures)."""
    import mapcells

    import numpy as np
    out = {}
    pad = max_m / 111000.0 * 2
    keys = mapcells.cells_for_points(np.asarray([lat]), np.asarray([lon]), pad_lat=pad, pad_lon=pad * 2)
    for layer in NEAR_LAYERS:
        hits = []
        for key in keys:
            if not md.has(key, layer):
                continue
            try:
                f = md.cell(key, layers=(layer,), kind="background").layers.get(layer)
            except Exception as e:
                log.debug("report nearby %s %s: %s", key, layer, e)
                continue
            if f is None or not len(f.kind):
                continue
            rings = []
            for i in range(len(f.kind)):
                for p in range(int(f.part_off[i]), int(f.part_off[i + 1])):
                    for r in range(int(f.ring_off[p]), int(f.ring_off[p + 1])):
                        rings.append((i, int(f.coord_off[r]), int(f.coord_off[r + 1])))
            ll = np.ascontiguousarray(np.asarray(f.lonlat, np.float64).reshape(-1, 2))
            for i, d in _ef().report_nearby(ll, rings, lat, lon, max_m, 12):
                e = {"m": round(d, 1), "cell": str(key)}
                ids = getattr(f, "ids", None)
                if ids is not None:
                    e["id"] = _scalar(ids[i])
                for k in sorted(f.attrs)[:NEAR_ATTRS]:
                    v = _scalar(f.attrs[k][i])
                    if v is not None:
                        e[k] = v
                hits.append(e)
        seen, uniq = set(), []
        for e in sorted(hits, key=lambda e: e["m"]):
            if e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            uniq.append(e)
        out[layer] = uniq[:12]
    return out


def sources_at(server, lat, lon):
    """The data sources that cover the point: the photo chain's (its sharpest zoom there), the elevation's coarse
    reading, the regional sets."""
    out = {}
    im = getattr(server, "imagery", None)
    if im is not None:
        try:
            names = []
            for src in getattr(im, "chain", []):
                b = getattr(src, "bounds", None)
                if not b or (b[0] <= lat <= b[2] and b[1] <= lon <= b[3]):
                    names.append(getattr(src, "name", str(src)))
            out["imageryChain"] = names      # (the photo chain; a source without a box may still not cover the point)
        except Exception as e:
            out["imageryChain"] = f"error: {type(e).__name__}"
        try:
            out["imageryStats"] = im.stats_snapshot()
        except Exception:
            pass
    dem = getattr(server, "dem", None)
    if dem is not None:
        try:
            out["demStats"] = dem.stats_snapshot()
        except Exception:
            pass
    return out


def build_info(version, server_version):
    import updates
    info = {"app": version, "server": server_version, "commit": git_commit(updates.ROOT),
            "earthfast": getattr(fast._ef, "VERSION", None) if fast._ef is not None else None}
    for mod, attr, key in (("plancache", "PLANS_FORMAT", "plansFormat"), ("roads", "FACTS_FORMAT", "factsFormat"),
                           ("pmtiles", "FORMAT", "pmtilesFormat")):
        try:
            info[key] = getattr(__import__(mod), attr)
        except Exception:
            pass
    try:
        import budget
        info["tier"] = budget.TIER
    except Exception:
        pass
    return info


def server_state(server, conn, msg, version, server_version):
    """server.json of a report (see the module's notes): each part on its own, a failing one is an "error: ..." line."""
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "build": build_info(version, server_version), "env": env_overrides()}
    parts = {
        "memory": lambda: __import__("memwatch").status(),
        "connection": lambda: {"backlog": conn.backlog(), "lastStatus": getattr(conn, "last_status", None),
                               "anchor": getattr(conn, "active", None), "games": len(server.connections),
                               "net": server.net_note()},
    }
    for k, f in parts.items():
        try:
            out[k] = f()
        except Exception as e:
            out[k] = f"error: {type(e).__name__}: {e}"
    spot = {}
    for what in ("camera", "aim", "player"):
        ll = _latlon(msg.get(what))
        if ll:
            spot[what] = spot_keys(*ll)
    aim = _latlon(msg.get("aim")) or _latlon(msg.get("camera")) or _latlon(msg.get("player"))
    if aim:
        md = getattr(getattr(server, "surface", None), "md", None) or getattr(server, "mapdata", None)
        if md is not None:
            try:
                spot["nearby"] = nearby(md, *aim)
            except Exception as e:
                spot["nearby"] = f"error: {type(e).__name__}: {e}"
        try:
            out["sources"] = sources_at(server, *aim)
        except Exception as e:
            out["sources"] = f"error: {type(e).__name__}: {e}"
    out["spot"] = spot
    return out


def server_log_path(cache_dir):
    """The running server's log (the launcher's, start.py's), or None."""
    cands = [os.environ.get("BNGE_SERVER_LOG")]
    try:
        import apppaths
        cands.append(os.path.join(apppaths.data_dir(), "logs", "server.log"))
    except Exception:
        pass
    cands.append(os.path.join(cache_dir, "logs", "server.log"))
    for p in cands:
        if p and os.path.isfile(p) and time.time() - os.path.getmtime(p) < 3600:
            return p
    return None


def logs_json(user_dir, cache_dir):
    """{"server.log": its end, "beamng.log": the game's recent warnings and errors} (earthfast report_log_tail)."""
    ef = _ef()
    out = {}
    p = server_log_path(cache_dir)
    if p:
        try:
            out["server.log"] = ef.report_log_tail(_read(p, LOG_TAIL_BYTES).decode("utf-8", "replace"), "server",
                                                   SERVER_LINES, 0.0)
        except OSError as e:
            out["server.log"] = f"(could not read: {e.strerror})"
    g = os.path.join(user_dir, "beamng.log")
    if os.path.isfile(g):
        try:
            out["beamng.log"] = ef.report_log_tail(_read(g, LOG_TAIL_BYTES).decode("utf-8", "replace"), "beamng",
                                                   400, GAME_LOG_S)
        except OSError as e:
            out["beamng.log"] = f"(could not read: {e.strerror})"
    return out


def write_context(path, state, logs, names):
    """server.json and logs.json into the report folder, scrubbed."""
    ef = _ef()
    for name, obj in (("server.json", state), ("logs.json", logs)):    # (logs plain on disk: the send window shows them)
        if not obj:
            continue
        # (NaN / inf, which Python writes and JSON has not, as null; anything else unknown as its text)
        text = json.dumps(json.loads(json.dumps(obj, default=str), parse_constant=lambda _c: None))
        clean = ef.report_scrub_json(text, names, False)
        with open(os.path.join(path, name + ".tmp"), "w", encoding="utf-8") as fh:
            fh.write(clean)
        os.replace(os.path.join(path, name + ".tmp"), os.path.join(path, name))


def scrub_saved(path, names):
    """report.json / hardware.json the mod wrote, scrubbed in place."""
    ef = _ef()
    for name in ("report.json", "hardware.json"):
        p = os.path.join(path, name)
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            clean = ef.report_scrub_json(text, names, False)
        except (OSError, ValueError) as e:
            log.debug("report scrub %s: %s", p, e)
            continue
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(clean)
        os.replace(p + ".tmp", p)


def build_body(path, install, version, names):
    """(body bytes, notes) of a report folder under its send.json choices."""
    ef = _ef()
    choice = _load_json(os.path.join(path, "send.json")) or {}
    coarse = choice.get("exactLocation") is False

    def text(name):
        p = os.path.join(path, name)
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    report = text("report.json")
    if report is None:
        raise ValueError("report.json is missing")
    shot = None
    if choice.get("screenshot") is True and os.path.isfile(os.path.join(path, "screenshot.jpg")):
        shot = _read(os.path.join(path, "screenshot.jpg"))
    logs = None
    if choice.get("logs") is True and os.path.isfile(os.path.join(path, "logs.json")):
        logs = ef.report_logs_pack(text("logs.json"), names, coarse)
    return ef.report_body(install, version, report, text("hardware.json"), text("server.json"), shot, logs, names,
                          coarse)


def post(url, body, timeout=TIMEOUT_S):
    """(status, answer dict) of one POST; status 0: no answer (network)."""
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "BeamNG.Earth"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(4096) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            ans = json.loads(e.read(4096) or b"{}")
        except ValueError:
            ans = {}
        return e.code, ans if isinstance(ans, dict) else {}
    except (OSError, ValueError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def retryable(status):
    return status == 0 or status in (408, 429, 500, 502, 503, 504, 507)


class Sender:
    """The sends (see the module's notes). notify(msg) tells the games (report_status)."""

    def __init__(self, user_dir, cache_dir, version, install, notify, url=URL, offline=False, post_fn=post,
                 backoff=BACKOFF_S):
        self.user_dir, self.cache_dir, self.version, self.install = user_dir, cache_dir, str(version or "0"), install
        self.notify, self.url, self.offline, self.post = notify, url, offline, post_fn
        self.backoff = backoff
        self.names = user_names()
        self.busy = set()

    def _state(self, path, name, state, **kw):
        d = dict(_load_json(os.path.join(path, "sent.json")) or {})
        d.update(kw, state=state, at=time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            _write_json(os.path.join(path, "sent.json"), d)
        except OSError as e:
            log.warning("report %s: %s", name, e)
        msg = {"type": "report_status", "folder": name, "state": state}
        for k in ("id", "text", "tries", "next"):
            if k in d:
                msg[k] = d[k]
        self.notify(msg)
        return d

    async def send(self, name):
        """One report's send with its retries (a second click while it is under way is ignored)."""
        path = folder(self.user_dir, name)
        if path is None or name in self.busy:
            return
        done = _load_json(os.path.join(path, "sent.json")) or {}
        if done.get("state") == "sent":
            self.notify({"type": "report_status", "folder": name, "state": "sent", "id": done.get("id"),
                         "text": done.get("text", "")})
            return
        self.busy.add(name)
        try:
            await self._send(path, name)
        finally:
            self.busy.discard(name)

    async def _send(self, path, name):
        if self.offline:
            self._state(path, name, "queued", text="offline mode: sent at a start without --offline")
            return
        loop = asyncio.get_running_loop()
        try:
            body, notes = await loop.run_in_executor(None, build_body, path, self.install, self.version, self.names)
        except Exception as e:
            log.warning("report %s: could not be packed: %s: %s", name, type(e).__name__, e)
            self._state(path, name, "failed", text=f"could not be packed: {e}")
            return
        for tries in range(1, len(self.backoff) + 2):
            self._state(path, name, "sending", tries=tries)
            status, ans = await loop.run_in_executor(None, self.post, self.url, body)
            if status == 200 and isinstance(ans.get("id"), str):
                rid = ans["id"][:16]
                log.info("report %s sent: %s (%d bytes%s)", name, rid, len(body), "; " + "; ".join(notes) if notes else "")
                self._state(path, name, "sent", id=rid, text="; ".join(notes))
                return
            why = str(ans.get("error") or f"HTTP {status}")[:200]
            if not retryable(status) or tries > len(self.backoff):
                log.warning("report %s: not sent (%s)", name, why)
                self._state(path, name, "failed" if not retryable(status) else "queued",
                            text=why + ("" if not retryable(status) else ": tried again at the next start"))
                return
            wait = self.backoff[tries - 1]
            log.info("report %s: not sent yet (%s), again in %d s", name, why, wait)
            self._state(path, name, "retry", text=why, next=wait)
            await asyncio.sleep(wait)

    def pending(self, now=None):
        """The folders whose send was clicked and is not done (queued, retry, sending: a server that stopped), newer
        than RESUME_DAYS."""
        now = now or time.time()
        out = []
        for root in roots(self.user_dir):
            for name in sorted(os.listdir(root)):
                if not NAME_RE.fullmatch(name):
                    continue
                d = _load_json(os.path.join(root, name, "sent.json"))
                if d and d.get("state") in ("queued", "retry", "sending"):
                    try:
                        age = now - os.path.getmtime(os.path.join(root, name, "send.json"))
                    except OSError:
                        continue
                    if age < RESUME_DAYS * 86400:
                        out.append(name)
        return out

    async def resume(self, delay=20.0):
        """At a start: the reports a stopped server left unsent, one after another."""
        await asyncio.sleep(delay)
        for name in self.pending():
            log.info("report %s: sending the report queued last time", name)
            await self.send(name)


def context(server, conn, msg, version, server_version):
    """report_context (see the module's notes): runs in a thread."""
    path = folder(server.user_dir, msg.get("folder"))
    if path is None:
        return False
    names = user_names()
    state = server_state(server, conn, msg, version, server_version)
    try:
        logs = logs_json(server.user_dir, server.cache_dir)
    except Exception as e:
        logs = {"error": f"{type(e).__name__}: {e}"}
    write_context(path, state, logs, names)
    return True


def install_id(cache_dir, keep=True):
    """The install's random id (telemetry.install_id; a session's own when the region keeps none)."""
    if not keep:
        return secrets.token_hex(16)
    import telemetry
    return telemetry.install_id(cache_dir)
